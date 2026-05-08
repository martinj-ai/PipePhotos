# POC Arbre DayPass — v0.1

Pipeline minimal 3 nœuds : ingestion → analyse Gemini Flash → sortie JSON.

## Setup

```bash
cd "/Users/martinj/Desktop/Noeuds/poc"

# 1. Clé Gemini (gratuite sur https://aistudio.google.com/apikey)
cp .env.example .env
# ouvre .env et colle : GEMINI_API_KEY=AIza...

# 2. Deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Run sur toutes les photos de data/input/
python analyze.py

# ou une seule
python analyze.py data/input/04_hilton-cabana-miami-beach-resort_196a0dff.webp
```

## Ce qui sort

Pour chaque photo, un JSON dans `data/output/{name}.json` contenant :
- `trace` : log des nœuds traversés (N1 ingestion, N2 analyse) avec durée
- `analysis.factual` : catégorie, sujets, humains, heure du jour, décor
- `analysis.emotional` : sensations, brand_keywords, pillar_scores (Freedom/Wellness/Experience)
- `analysis.technical_hints` : ambiance, palette, compatibilité IA
- `analysis.recommended_placement` : hero_home / product_amenities / product_incarnated / rejected
- `analysis.issues` : problèmes brand éventuels

## À itérer ensuite

- Raffiner le prompt sur les cas mal classés
- Ajouter nœud qualité technique locale (blur, expo — sans appel API)
- Ajouter nœud outil Lumière (transformation)
- Brancher fal.ai pour l'ajout personnage
- Interface canvas React Flow (voir `../canvas_tech_reco.md`)

## 📚 Documentation

| Fichier | Contenu |
|---|---|
| [`docs/MULTI_FORMAT_CROP_SPEC.md`](docs/MULTI_FORMAT_CROP_SPEC.md) | Spec multi-format crop (13 formats, outpainting Nano Banana 2) |
| [`docs/SLOWMO_SPEC.md`](docs/SLOWMO_SPEC.md) | Spec slow-motion via Higgsfield Kling 2.1 Pro |
| [`docs/PROD_MIGRATION.md`](docs/PROD_MIGRATION.md) | **Checklist d'adaptation prod Railway** — à maintenir à chaque feature |
| [`CLAUDE.md`](CLAUDE.md) | Méthodologie de travail pour les itérations Claude |

## ⚠️ Ce qui est local-only à industrialiser

L'outil tourne actuellement en **POC local** (filesystem, pas de DB, pas de queue async).
Avant de déployer sur Railway, voir [`docs/PROD_MIGRATION.md`](docs/PROD_MIGRATION.md)
qui répertorie tous les points à adapter (CDN pour les médias, Postgres pour les
analyses, Redis pour le cache de progress, RQ/Celery pour les pipelines longs).
