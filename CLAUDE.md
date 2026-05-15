# Notes pour Claude (et autres agents) qui bossent sur ce projet

## 🎯 Contexte

POC PipePhotos pour le déploiement Dayuse "Day Access" (50-200 hôtels, deadline serrée).
Pipeline photo : **scrap multi-source HD** → analyse Gemini Vision → sélection → retouches IA
(Nano Banana 2) → LUT brand → **upscale Lanczos** → multi-format crop → slowmo Higgsfield (optionnel) → PDF avant/après (optionnel).

**Owner** : Martin (CRM/email marketing lead chez Dayuse, parle français).
**Statut actuel** : POC local sur MacBook, pas encore déployé.

## 📂 Layout du projet

```
poc/
├── booking_scraper.py            # Playwright stealth, upgrade auto max1024→max3000
├── booking_amenities_extractor.py # Mapping facilities Booking → taxonomie interne
├── expedia_scraper.py            # Patchright + Chrome réel + headless=False (bypass DataDome) → 3840×2560
├── rp_scraper.py                 # JSON __NEXT_DATA__ depuis fiche RP
├── hotel_site_finder.py          # DDG primary + Gemini fallback, whitelist chaînes
├── hotel_gallery_extractor.py    # Cascade Playwright stealth → fallback patchright si Access Denied
├── hotel_site_finder.py          # DDG + Gemini cascade pour URL site officiel
├── expedia_finder.py             # DDG + Gemini cascade pour URL Expedia + détecteur hallucination ID
├── rp_finder.py                  # DDG + Gemini cascade pour URL ResortPass
├── analyze.py                    # Gemini Vision → JSON par photo (2 passes : light + rich)
├── ai_validator.py               # Validation post-IA (invented_furniture, architecture_changed, etc.)
├── amenity_verifier.py           # 2e passe Gemini sur top candidats par bucket
├── dedup_angles.py + dedup_vlm.py # Dédup pHash + VLM Gemini sur zone grise
├── coverage.py                   # Scoring + sélection par buckets brand
├── ordering.py                   # Ordre du pack final (slot 1, alternance, bonus lifestyle)
├── enhance.py                    # Retouches Nano Banana 2 + LUT brand + Lanczos x2 final
├── brand_lut.py                  # LUT brand Dayuse (profils soft/medium/strong)
├── multi_format_cropper.py       # Step 5 : crop + outpaint multi-format (13 formats)
├── slowmo_higgsfield.py          # Step 6 : slow-motion via Kling 2.1 Pro + ffmpeg ping-pong
├── pdf_export.py                 # PDF avant/après branded Dayuse (Playwright PDF)
├── photo_journey.py              # Tracking workflow (16 nœuds) pour viz front
├── photo_generator.py            # Génération photos IA (full IA, pour amenities manquantes)
├── booking_amenities_extractor.py # Scrape amenities depuis page Booking
├── laws.py / laws_matrix.py      # Audit matriciel des 20 lois métier
├── model_comparison.py           # Test AB GPT Image vs Nano Banana
├── progress.py                   # Progress tracker file-based (data/progress/)
├── spend.py                      # Cumul cost/tokens cross-pipeline
├── app.py                        # Flask backend
├── templates/index.html          # UI principale (~4500 lignes)
├── templates/pdf_export.html     # Template Jinja2 du PDF
├── config/
│   ├── output_formats.json       # 13 formats multi-format
│   ├── vibes_personas.json       # Mapping vibe → personas autorisés
│   ├── brand_lut.json            # Paramètres LUT brand par profil
│   └── pool_floats.json          # Catalogue bouées (flamingo, unicorn, etc.)
├── docs/
│   ├── MULTI_FORMAT_CROP_SPEC.md
│   ├── SLOWMO_SPEC.md
│   ├── PROD_MIGRATION.md         # ⚠️ checklist Railway — à maintenir à chaque feature
│   ├── FAILURE_MODES_MATRIX.md   # (13/05/2026) Matrix des bugs récurrents IA + mitigations
│   └── PROMPT_ANATOMY.md         # 🆕 (14/05/2026) Dissection des prompts ai_add_character par type (probabiliste/dérivé/hardcoded) + pistes de réduction de la dilution
├── _legacy_prompts.py            # Snapshot V1 long prompt — rollback via USE_COMPACT_PROMPT_V2 env
├── scenario_writer.py            # 🆕 (13/05/2026 V5) Gemini Vision génère le scenario adapté à chaque photo (résout bugs catalogue aveugle)
└── data/                         # Local only, gitignored
    ├── uploads/<slug>/           # photos sources scrapées
    ├── analyses/<slug>/          # JSON Gemini Vision (1 par photo)
    ├── output/<slug>/             # enhanced/, multiformat/, slowmo/, comparison/, pdf/
    ├── rp/<slug>.json            # cache scrape (booking ou RP)
    ├── seo_names.json            # mapping filename → nom SEO (dayuse_{slug}_{amenity}_{seq}.jpg)
    └── progress/                 # progress.json par run
```

## 🚨 Règles MUST FOLLOW

### 1. Toute feature → mettre à jour `docs/PROD_MIGRATION.md`

Chaque ajout de feature doit être noté dans la section "Features actuelles" ou
"Features à venir" du doc. On note : où ça stocke des fichiers, si ça utilise un secret,
si c'est sync/async. C'est une checklist pour le jour du déploiement Railway.

**Pourquoi** : on ne veut pas découvrir 50 incompatibilités le jour du push prod.

### 2. Répondre en français à Martin

Martin préfère le français même si le système est en anglais. Les commentaires de code
peuvent rester en français aussi.

### 3. Ne pas casser le pipeline existant

Quand on ajoute des features (crop, slowmo, replay), le comportement par défaut doit
rester identique. Toute nouvelle option doit être **opt-in** (toggle / radio / checkbox
décoché par défaut). Pour les ajouts globaux (ex: Lanczos x2 sur toutes les photos),
exposer une env var pour désactiver (`FINAL_UPSCALE_FACTOR=1.0`).

### 4. Toujours commit + push après une feature complète

Format de commit : message clair en anglais, signé `Co-Authored-By: Claude...`. Push
vers `feat/photo-optimization` ou la feature branch en cours.

### 5. Tester sur des données existantes avant de relancer un pipeline complet

Le pipeline complet prend ~5 min. Pour tester une feature, utiliser **le mode replay**
(`resume_from=postprocess` ou `=selection`) qui skip les étapes coûteuses. Voir le
bandeau "Run précédent détecté" en Step 3.

## 🛠 Stack technique actuelle

- **Python 3.9** (warning EOL — bump à 3.11 prévu pour la prod)
- **Flask** en `debug=True` sur `localhost:5050`
- **Gemini Vision** (analyse) + **Gemini Image** (Nano Banana 2 retouche/outpaint)
- **OpenAI `gpt-image-2`** (uniquement pour benchmark `model_comparison.py`, non-prod)
- **Higgsfield Kling 2.1 Pro** (slowmo)
- **Playwright** (scrap Booking) + **playwright-stealth**
- **Patchright** (Playwright undetected fork) — bypass DataDome/Akamai sur Expedia, Marriott, Hilton, Hyatt, IHG
- **Pillow** (crop local, LUT, Lanczos upscale)
- **ImageHash** (pHash dedup inter-sources)
- **ffmpeg** (slowmo loops ping-pong)

## 🔑 Décisions adoptées

| Sujet | Décision | Date | Source |
|---|---|---|---|
| Modèle de retouche IA | **Nano Banana 2** par défaut (`gemini-3.1-flash-image-preview`), fallback Pro outpaint | 06/05/2026 | AB test 36 versions |
| Outpainting | Nano Banana Flash, fallback Pro auto | 05/05/2026 | Spec multi-format |
| GPT Image 2 | Non adopté (lettrebox systématique) | 05/05/2026 | AB test |
| Persona alternance | Opportuniste, slot 1 obligatoire human-ready | 12/05/2026 | refonte règle métier |
| Slot 1 hard reqs | dominance ≥ 50%, shot ≠ close_up, hero ≥ 50, `_human_can_be_prominent` | itération | lois L2 + F5 |
| Workflow Étape 1 | **Booking comme clé d'entrée unique** + auto-discovery DDG/Gemini pour Site officiel + Expedia + RP | 12/05/2026 | refactor Martin |
| Instagram | **Retiré** (bot wall systématique, scraping anonyme bloqué) | 12/05/2026 | retour Martin |
| Bypass Expedia / chaînes Akamai | Patchright + Chrome réel + `headless=False` (cascade auto si stealth headless bloqué) | 12/05/2026 | empirique |
| Résolution sources | Booking 3000×2000 (max3000 path) + Expedia 3840×2560 (URL nue) + RP natif + officiel via srcset | 12/05/2026 | tests CDN |
| Upscale photos finales | **Lanczos x2** sur TOUTES les photos finales (1264→2528) avant pack | 12/05/2026 | retour Martin |
| Real-ESRGAN | En backlog (Lanczos x2 jugé suffisant pour le besoin "pas de pixelisation plein écran") | 12/05/2026 | retour Martin |

## 📋 État du replay/cache

3 modes de reprise dispo via le bandeau cyan en Step 3 :
- `scrape` (default) : tout refaire
- `selection` : skip Gemini analyse → -2-3 min
- `postprocess` : skip Gemini + skip enhance → -3-4 min

Cache lookup transparent dans `analyze.analyze_batch(use_cache=True)`.

## 🎨 Charte UI Dayuse

Cf. plugin `frontend-design-dayuse`. Couleurs principales :
- Primary gradient : `linear-gradient(62deg, #FFAF36 0%, #FFC536 100%)`
- Text primary : `#292935`
- Text secondary : `#54545D`
- Border-radius : 100px (pill) pour boutons, 12px pour cards
- Police : Manrope (400-800)

---

## 🐛 Bugs récurrents IA — voir `docs/FAILURE_MODES_MATRIX.md`

Matrix maintenue avec tous les failure modes connus du pipeline + mitigations + statut. À CONSULTER avant de modifier `enhance.py` / `ai_validator.py` / `_legacy_prompts.py` pour éviter de réintroduire un bug fixé.

## 🚩 Flags d'environnement actifs

| Variable | Défaut | Effet |
|---|---|---|
| `USE_LEGACY_SCENARIO_CATALOG` | non défini | **(V5, 13/05/2026)** Si `=1`, rollback vers le catalogue Python hardcoded au lieu du scenario généré dynamiquement par Gemini Vision. V5 actif par défaut = +$0.002/photo retouchée mais +30-50% cohérence scenario/photo. |
| `USE_COMPACT_PROMPT_V2` | non défini | Si `=1`, utilise V2 compacte (~2000 tokens) au lieu de V1 longue (~5000). V2 = expérimentale, peut dégrader les garde-fous. |
| `FINAL_UPSCALE_FACTOR` | `2.0` | Facteur Lanczos final sur photos pack (1264→2528). Mettre `1.0` pour skip. |
| `GEMINI_API_KEY` | requis | Clé API Gemini Vision + Image |
| `HIGGSFIELD_API_KEY` | requis pour slowmo | Clé API cloud.higgsfield.ai (≠ wallet consumer higgsfield.ai !) |

## 🔄 Pipeline de retry post-IA

Validator Gemini Vision passe sur chaque photo retouchée. Si violation ∈ `ACTIONABLE_VIOLATIONS` (cf. `enhance.py:2513`), retry avec :
1. **Reinforcement message** ajouté en tête du prompt (cf. `_REINFORCEMENT_BY_VIOLATION` dans `enhance.py`)
2. **Scenario swap** vers une alternative safe (cf. `_SCENARIO_SWAP_TARGETS`) — typiquement `pool_edge` ou `outdoor_deck` qui ne nécessitent aucun mobilier inventable

Si retry échoue : **fallback au dernier step IA réussi** (= preserve ai_lighting si ai_add_character échoue) plutôt qu'au pixel input brut.
