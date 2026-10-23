# Cockpit Quick

Tableau de bord de suivi des performances des restaurants Quick (fiches établissement) : impressions, visites en magasin, clics d'appel, itinéraires et visites web, agrégés par semaine.

## Mettre à jour les données

1. Déposer le nouvel export dans `data/quick.csv` (export TSV UTF-16 tel que sorti de l'outil).
2. Lancer :

```bash
python build_dashboard.py
```

Le script génère `site/index.html` (déployé par Vercel) et `cockpit_quick.html` (copie locale).
`python build_dashboard.py --template` regénère la page sans relire le CSV (après une modification de `dashboard_template.html`).

3. Commit + push : Vercel redéploie automatiquement.

L'export brut n'est pas versionné (`.gitignore`).
