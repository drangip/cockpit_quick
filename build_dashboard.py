"""Construit le cockpit Quick (HTML autonome) à partir de l'export journalier.

Usage : python build_dashboard.py [chemin_csv]
        python build_dashboard.py --template   (réutilise les agrégats en cache)
Sortie : cockpit_quick.html et site/index.html (données, logos et carte embarqués)
"""
import base64
import io
import json
import os
import re
import sys
import urllib.request

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
CSV = ARGS[0] if ARGS else os.path.join(ROOT, "data", "quick.csv")
GEO_CACHE = os.path.join(ROOT, "data", "geocode_magasins.csv")
DEP_CACHE = os.path.join(ROOT, "data", "departements.geojson")
TEMPLATE = os.path.join(ROOT, "dashboard_template.html")
OUT = os.path.join(ROOT, "cockpit_quick.html")
SITE_OUT = os.path.join(ROOT, "site", "index.html")  # version servie par Vercel
PAYLOAD_CACHE = os.path.join(ROOT, "data", "agregats_hebdo.json")

BAN_URL = "https://api-adresse.data.gouv.fr/search/csv/"
DEP_URL = "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/departements-version-simplifiee.geojson"

METRICS = {
    "imp": "Couverture locale (impressions)",
    "vis": "Visites en magasin",
    "call": "Clics d'appel",
    "itin": "Itinéraire",
    "web": "Visites sur le site Web",
}
# Libellés de fiche sans nom de restaurant exploitable : on nomme alors par la ville
GENERIC_LABELS = {"quick", "quickos", "fermé", "fiche fermée", "épicerie", "macdo"}


def dep_code(cp: str) -> str:
    if cp.startswith("20"):
        return "2A" if int(cp) < 20200 else "2B"
    if cp.startswith("97"):
        return cp[:3]
    return cp[:2]


def to_int(s: pd.Series) -> pd.Series:
    # Séparateurs de milliers de l'export : espaces fines insécables ( ), insécables, espaces
    return pd.to_numeric(s.fillna("0").astype(str).str.replace(r"[\s \xa0]", "", regex=True), errors="coerce").fillna(0).astype(int)


def store_name(label: str, city: str) -> str:
    lab = re.sub(r"\s+", " ", label).strip()
    low = lab.lower()
    if low.startswith("burger king"):
        return "Burger King " + city
    if low.startswith("quick") and low not in GENERIC_LABELS:
        return re.sub(r"^Quick\s*-\s*", "Quick ", lab, flags=re.I)
    return "Quick " + city


def ban_csv(df: pd.DataFrame, **fields) -> pd.DataFrame:
    """Géocodage par lot via la Base Adresse Nationale."""
    boundary = "----quickcockpit"
    body = io.BytesIO()
    payload = df.to_csv(index=False).encode("utf-8")

    def part(name, value, filename=None):
        body.write(f"--{boundary}\r\n".encode())
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        body.write(f"Content-Disposition: {disp}\r\n\r\n".encode())
        body.write(value if isinstance(value, bytes) else value.encode())
        body.write(b"\r\n")

    part("data", payload, "in.csv")
    for k, v in fields.items():
        part(k, v)
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BAN_URL, data=body.getvalue(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return pd.read_csv(io.BytesIO(r.read()), dtype=str)


def geocode(stores: pd.DataFrame) -> pd.DataFrame:
    if os.path.exists(GEO_CACHE):
        cache = pd.read_csv(GEO_CACHE, dtype={"key": str})
        if set(stores["key"]) <= set(cache["key"]):
            return cache
    q = stores[["key", "addr", "cp", "city"]].copy()
    res = ban_csv(q, columns="addr", postcode="cp", city="city")
    res["result_score"] = pd.to_numeric(res["result_score"], errors="coerce").fillna(0)
    ok = (res["result_score"] >= 0.5) & (res["result_postcode"].fillna("").str[:2] == res["cp"].str[:2])
    geo = res.loc[ok, ["key", "latitude", "longitude"]].assign(precision="adresse")
    # Repli : centre de la commune
    miss = q[~q["key"].isin(geo["key"])]
    if len(miss):
        res2 = ban_csv(miss[["key", "cp", "city"]], columns="city", postcode="cp")
        res2 = res2[pd.to_numeric(res2["latitude"], errors="coerce").notna()]
        geo = pd.concat([geo, res2[["key", "latitude", "longitude"]].assign(precision="commune")])
    # Dernier repli : codes postaux CEDEX inconnus de la BAN, commune seule dans le bon département
    miss = q[~q["key"].isin(geo["key"])]
    if len(miss):
        res3 = ban_csv(miss[["key", "cp", "city"]].assign(dep=miss["cp"].str[:2]), columns="city")
        res3 = res3[(res3["result_type"] == "municipality") & (res3["result_citycode"].fillna("").str[:2] == res3["dep"])]
        geo = pd.concat([geo, res3[["key", "latitude", "longitude"]].assign(precision="commune")])
    geo["latitude"] = pd.to_numeric(geo["latitude"], errors="coerce")
    geo["longitude"] = pd.to_numeric(geo["longitude"], errors="coerce")
    geo.to_csv(GEO_CACHE, index=False)
    return geo


def departements() -> dict:
    if not os.path.exists(DEP_CACHE):
        urllib.request.urlretrieve(DEP_URL, DEP_CACHE)
    with open(DEP_CACHE, encoding="utf-8") as f:
        gj = json.load(f)

    def rnd(c):
        return [rnd(x) for x in c] if isinstance(c[0], list) else [round(c[0], 3), round(c[1], 3)]

    for ft in gj["features"]:
        ft["geometry"]["coordinates"] = rnd(ft["geometry"]["coordinates"])
    return gj


def b64(path: str, mime: str) -> str:
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def main():
    # Cache des agrégats : évite de relire le CSV quand seul le template change
    if "--template" in sys.argv and os.path.exists(PAYLOAD_CACHE):
        with open(PAYLOAD_CACHE, encoding="utf-8") as f:
            payload = json.load(f)
        write_html(payload, departements())
        return
    print("Lecture", CSV)
    # Export Google Business Profile : TSV en UTF-16
    df = pd.read_csv(CSV, sep="\t", encoding="utf-16", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    # Colonnes à accents (parfois corrompus dans l'export) : on les retrouve par préfixe
    col = dict(METRICS)
    col["itin"] = next(c for c in df.columns if c.startswith("Itin") and "(" not in c)
    for k, c in col.items():
        df[c] = to_int(df[c])

    df["Jour"] = pd.to_datetime(df["Jour"], format="%d/%m/%Y")
    for c in ["address_line_1", "address_line_2", "city", "Adresse des magasins"]:
        df[c] = df[c].fillna("").astype(str).str.strip()
    df["cp"] = df["postal_code"].fillna("").astype(str).str.strip().str.zfill(5)
    df["key"] = df["address_line_1"] + "|" + df["address_line_2"] + "|" + df["city"] + "|" + df["cp"]
    df = df.sort_values("Jour").drop_duplicates(["key", "Jour"], keep="last")

    # Fin des données : dernier jour où les visites en magasin sont remontées, puis dernière semaine complète
    daily = df.groupby("Jour")[col["vis"]].sum()
    last_day = daily[daily > 0].index.max()
    last_sunday = last_day - pd.Timedelta(days=(last_day.weekday() + 1) % 7)
    first_day = df["Jour"].min()
    first_monday = first_day + pd.Timedelta(days=(7 - first_day.weekday()) % 7)
    df = df[(df["Jour"] >= first_monday) & (df["Jour"] <= last_sunday)].copy()
    df["week"] = df["Jour"] - pd.to_timedelta(df["Jour"].dt.weekday, unit="D")
    weeks = pd.date_range(first_monday, last_sunday - pd.Timedelta(days=6), freq="7D")
    print(f"Semaines : {weeks[0].date()} -> {weeks[-1].date()} ({len(weeks)})")

    # Référentiel magasins (libellé de fiche le plus récent)
    stores = df.groupby("key").agg(
        a1=("address_line_1", "first"), a2=("address_line_2", "first"), city=("city", "first"),
        cp=("cp", "first"), label=("Adresse des magasins", "last"),
    ).reset_index()
    stores["addr"] = stores["a2"].where(~stores["a2"].isin(["", "--"]), stores["a1"])
    stores["dep"] = stores["cp"].map(dep_code)
    stores["street"] = stores.apply(
        lambda r: ", ".join(x for x in [r.a1, r.a2] if x and x != "--"), axis=1
    )
    stores["name"] = stores.apply(lambda r: store_name(r.label, r.city), axis=1)
    dup = stores["name"].duplicated(keep=False)
    stores.loc[dup, "name"] = stores.loc[dup].apply(lambda r: f"{r['name']} · {r.a1[:32]}", axis=1)
    geo = geocode(stores).drop_duplicates("key")
    stores = stores.merge(geo, on="key", how="left")
    stores = stores.sort_values(["dep", "city", "name"]).reset_index(drop=True)

    # Agrégation hebdomadaire (jours présents conservés pour repérer les semaines incomplètes)
    agg = df.groupby(["key", "week"]).agg(
        **{k: (c, "sum") for k, c in col.items()}, days=("Jour", "count")
    )
    idx = pd.MultiIndex.from_product([stores["key"], weeks], names=["key", "week"])
    agg = agg.reindex(idx)

    out_stores = []
    for s in stores.itertuples():
        a = agg.loc[s.key]
        rec = {
            "n": s.name, "st": s.street, "cp": s.cp, "c": s.city, "d": s.dep,
            "lat": None if pd.isna(s.latitude) else round(float(s.latitude), 5),
            "lng": None if pd.isna(s.longitude) else round(float(s.longitude), 5),
            "days": [0 if pd.isna(v) else int(v) for v in a["days"]],
        }
        for k in METRICS:
            rec[k] = [0 if pd.isna(v) else int(v) for v in a[k]]
        out_stores.append(rec)

    gj = departements()
    payload = {
        "weeks": [w.strftime("%Y-%m-%d") for w in weeks],
        "lastDay": last_sunday.strftime("%Y-%m-%d"),
        "stores": out_stores,
        "deps": {f["properties"]["code"]: f["properties"]["nom"] for f in gj["features"]},
    }
    with open(PAYLOAD_CACHE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    write_html(payload, gj)


def write_html(payload, gj):
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    html = (
        html.replace("__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        .replace("__GEO__", json.dumps(gj, ensure_ascii=False, separators=(",", ":")))
        .replace("__LOGO_QUICK__", b64(os.path.join(ROOT, "logo", "quick.webp"), "image/webp"))
        .replace("__LOGO_WPP__", b64(os.path.join(ROOT, "logo", "wppmedia.png"), "image/png"))
    )
    os.makedirs(os.path.dirname(SITE_OUT), exist_ok=True)
    for path in (OUT, SITE_OUT):
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    print(f"OK -> {OUT} ({os.path.getsize(OUT) / 1e6:.1f} Mo, {len(payload['stores'])} magasins)")


if __name__ == "__main__":
    main()
