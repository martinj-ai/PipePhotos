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
