# PipePhotos — Day Access photo pipeline

Outil interne Dayuse pour générer en ~5 min un pack photo complet (12-18 visuels HD prêts à publier) pour une fiche **Day Access** à partir d'une simple URL Booking.

> POC local sur MacBook Martin · pipeline 16 nœuds · à industrialiser sur Railway (cf. [`docs/PROD_MIGRATION.md`](docs/PROD_MIGRATION.md))

## 🎯 Ce que fait l'outil

1. **Tu colles une URL Booking** d'un hôtel
2. L'outil identifie l'hôtel (nom + ville + amenities) et trouve en parallèle les URLs **Site officiel** + **Expedia** + **ResortPass** via DuckDuckGo + Gemini fallback
3. Scraping HD multi-source (Booking 3000×2000, Expedia 3840×2560, RP natif, site officiel via Playwright stealth + patchright pour les chaînes Cloudflare)
4. Dédup pHash inter-sources → 30-100 photos uniques
5. Analyse Gemini Vision (catégorie, dominance amenity, hero quality, ambiance, safe zones humains, clutter)
6. Sélection & scoring → top 12-18 par buckets brand (Freedom / Wellness / Experience)
7. Retouches Nano Banana 2 (clutter removal, nuit→jour, ajout persona selon vibe, bouée occasionnelle)
8. Validation post-IA → retry prompt durci → fallback original si raté
9. LUT brand Dayuse (cohérence inter-photos)
10. **Upscale Lanczos x2** (1264×843 → 2528×1686 — évite la pixelisation plein écran)
11. (optionnel) Multi-format crop (13 formats : Insta feed/story, LinkedIn, FB OG, Dayuse banners…)
12. (optionnel) Cinemagraph slow-motion d'1 photo finale via Higgsfield Kling 2.1 Pro

## 🚀 Setup

```bash
cd "/Users/martinj/Desktop/Noeuds/poc"

# 1. Secrets (clés gratuites Gemini + Higgsfield)
cp .env.example .env
# édite .env :
#   GEMINI_API_KEY=AIza... (https://aistudio.google.com/apikey)
#   HF_API_KEY=...         (optionnel, slowmo)
#   HF_API_SECRET=...
#   FINAL_UPSCALE_FACTOR=2.0  (optionnel, default 2.0 — mettre 1.0 pour désactiver upscale)

# 2. Python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Playwright + patchright (bot bypass Expedia/Marriott/Hilton)
playwright install chromium
patchright install chromium

# 4. Lance l'app
python app.py
# → http://localhost:5050
```

## 🌳 Pipeline (16 nœuds)

```
sources → user_select → gemini_analyze → dedup_phash_strict → dedup_vlm
  → amenity_verifier → coverage_select → bonus_lifestyle → fully_generated_inject
  → ordering_slot → retouche → validation_postIA → lut_brand
  → upscale_lanczos → final_pack → slowmo_higgsfield (optionnel)
```

Détail visuel par run : onglet **🌳 Workflow** dans le front.
Doc tech par nœud : onglet **📚 Documentation → 📊 Workflow** (diagramme Mermaid + règles métier).

## 💰 Coût par hôtel

| Poste | Coût (USD) |
|---|---|
| Gemini Vision (analyse light + rich) | ~$0.04 |
| Nano Banana 2 retouche (~15 photos) | ~$1.00 |
| Validation post-IA | ~$0.05 |
| Multi-format outpaint (optionnel, 0-30 variantes) | $0-$2 |
| Slowmo Higgsfield (optionnel) | $0.35 |
| **Upscale Lanczos** | $0 (local PIL) |
| **TOTAL** typique | **~$1-3 / hôtel** |

## 📚 Documentation

| Fichier | Contenu |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Notes pour agents Claude (méthodo, layout, décisions) |
| [`docs/MULTI_FORMAT_CROP_SPEC.md`](docs/MULTI_FORMAT_CROP_SPEC.md) | Spec multi-format crop (13 formats, outpaint Nano Banana) |
| [`docs/SLOWMO_SPEC.md`](docs/SLOWMO_SPEC.md) | Spec slow-motion via Higgsfield Kling 2.1 Pro |
| [`docs/PROD_MIGRATION.md`](docs/PROD_MIGRATION.md) | **Checklist d'adaptation prod Railway** — à maintenir à chaque feature |

La doc complète orientée business (règles métier, scoring, vibes, personas, roadmap)
est dans le front, onglet **📚 Documentation**.

## ⚠️ Local-only à industrialiser

L'outil tourne en **POC local** :
- Filesystem (`data/uploads/`, `data/output/`, `data/analyses/`)
- Pas de DB (tout en JSON sur disque)
- Pas de queue async (pipeline synchrone dans la requête HTTP)
- Flask `debug=True` sur `localhost:5050`

Pour passer en prod Railway → [`docs/PROD_MIGRATION.md`](docs/PROD_MIGRATION.md) liste tout ce qu'il faut migrer (S3/R2 pour les médias, Postgres pour les analyses, Redis + RQ pour les jobs, etc.).

## 🔧 Mode replay

Pour itérer rapidement sans tout refaire (un pipeline complet = ~5 min) :

| Mode | Skip | Gain |
|---|---|---|
| `scrape` (default) | rien | — |
| `selection` | skip dedup_vlm + amenity_verifier (mais re-fait l'enhance + multi-format) | ~1-2 min |
| `postprocess` | skip dedup_vlm + amenity_verifier + photo_generator (réutilise les `enhanced/` du run précédent → re-fait juste multi-format / slowmo / PDF) | ~3-4 min |

Bandeau cyan en Step 3 si un run précédent est détecté.
