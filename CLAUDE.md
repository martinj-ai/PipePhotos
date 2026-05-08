# Notes pour Claude (et autres agents) qui bossent sur ce projet

## 🎯 Contexte

POC PipePhotos pour le déploiement Dayuse "Day Access" (50-200 hôtels, deadline serrée).
Pipeline photo : scrap multi-source → analyse Gemini Vision → sélection → retouches IA
(Nano Banana 2) → multi-format crop → slowmo Higgsfield (optionnel).

**Owner** : Martin (CRM/email marketing lead chez Dayuse, parle français).
**Statut actuel** : POC local sur MacBook, pas encore déployé.

## 📂 Layout du projet

```
poc/
├── analyze.py             # Gemini Vision → JSON par photo
├── enhance.py             # Retouches IA (Nano Banana 2 / GPT Image)
├── coverage.py            # Scoring + sélection
├── ordering.py            # Ordre du pack final (slot 1, alternance, bonus)
├── multi_format_cropper.py # Step 5 : crop + outpainting multi-format
├── slowmo_higgsfield.py   # Step 6 : slow-motion via Kling 2.1 Pro
├── photo_journey.py       # Tracking workflow (14 nœuds)
├── laws.py / laws_matrix.py # Audit matriciel des 20 lois métier
├── model_comparison.py    # Test AB GPT Image vs Nano Banana
├── app.py                 # Flask backend
├── templates/index.html   # UI principale
├── docs/
│   ├── MULTI_FORMAT_CROP_SPEC.md
│   ├── SLOWMO_SPEC.md
│   └── PROD_MIGRATION.md  # ⚠️ checklist Railway — à maintenir à chaque feature
└── data/                  # Local only, gitignored
    ├── uploads/<slug>/    # photos sources
    ├── analyses/<slug>/   # JSON Gemini Vision
    ├── output/<slug>/     # enhanced/, multiformat/, slowmo/, comparison/
    └── progress/          # progress.json par run
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
décoché par défaut).

### 4. Toujours commit + push après une feature complète

Format de commit : message clair en anglais, signé `Co-Authored-By: Claude...`. Push
vers `feat/initial-import` ou la feature branch en cours.

### 5. Tester sur des données existantes avant de relancer un pipeline complet

Le pipeline complet prend ~18min. Pour tester une feature, utiliser **le mode replay**
(`resume_from=postprocess` ou `=selection`) qui skip les étapes coûteuses. Voir le
bandeau "Run précédent détecté" en Step 3.

## 🛠 Stack technique actuelle

- Python 3.9 (warning EOL — bump à 3.11 prévu pour la prod)
- Flask en `debug=True` sur `localhost:5050`
- Gemini Vision (analyse) + Gemini Image (Nano Banana 2 retouche/outpaint)
- OpenAI `gpt-image-2` (uniquement pour benchmark, non-prod)
- Higgsfield Kling 2.1 Pro (slowmo)
- Pillow (crop local)
- Playwright (scrap Booking)
- ffmpeg (slowmo loops ping-pong)

## 🔑 Décisions adoptées

| Sujet | Décision | Date | Source |
|---|---|---|---|
| Modèle de retouche IA | **Nano Banana 2** par défaut, fallback Pro | 06/05/2026 | AB test 36 versions, voir doc UI |
| Outpainting | Nano Banana Flash, fallback Pro auto | 05/05/2026 | Spec multi-format |
| GPT Image 2 | Non adopté (lettrebox systématique) | 05/05/2026 | AB test |
| Persona alternance | 1 sur 2 strict, slot 1 obligatoire | itération | doc UI règles |
| Slot 1 hard reqs | dominance ≥ 50%, shot ≠ close_up, hero ≥ 50 | itération | lois L2 + F5 |

## 📋 État du replay/cache (06/05/2026)

3 modes de reprise dispo via le bandeau cyan en Step 3 :
- `scrape` (default) : tout refaire
- `selection` : skip Gemini analyse → -10min
- `postprocess` : skip Gemini + skip enhance → -15min

Cache lookup transparent dans `analyze.analyze_batch(use_cache=True)`.

## 🎨 Charte UI Dayuse

Cf. plugin `frontend-design-dayuse`. Couleurs principales :
- Primary gradient : `linear-gradient(62deg, #FFAF36 0%, #FFC536 100%)`
- Text primary : `#292935`
- Text secondary : `#54545D`
- Border-radius : 100px (pill) pour boutons, 12px pour cards
- Police : Manrope (400-800)
