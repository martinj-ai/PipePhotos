# Multi-format Crop & Outpainting — Spécification

**Version** : 0.2
**Auteurs** : Martin × Claude
**Statut** : ✅ Cadrage complet — prêt pour Phase 1 (paramètres Runwayml extraits du HAR)

---

## 🎯 Problème / besoin

Les photos finales du pack DayAccess (12-18 photos déjà sélectionnées + retouchées) doivent être **publiées** sur plusieurs surfaces qui ont chacune leur format imposé :
- Site Dayuse : home cards, fiche hôtel principale/secondaire, rate plan cards (mobile + desktop)
- Réseaux sociaux : YouTube thumbnail, Instagram feed/story/reels, LinkedIn banner, Facebook OG
- Probablement d'autres surfaces à venir

Aujourd'hui, ces variantes sont produites manuellement → temps perdu, risque humain (couper la tête d'un mannequin, masquer la piscine, etc.).

**On veut** : générer automatiquement les variantes nécessaires pour chaque photo finale, avec un crop intelligent qui ne coupe jamais le sujet, et un outpainting génératif quand le ratio cible est incompatible avec le ratio source.

---

## 👤 User story

> En tant que CRM/Marketing manager, après avoir lancé le pipeline et obtenu mon pack de 15 photos retouchées, je veux **cocher les surfaces de publication** dont j'ai besoin (site Dayuse / Insta / etc.), lancer la génération multi-format, et récupérer un **ZIP organisé en dossiers par format** prêt à uploader sans retouche manuelle.

---

## 📐 Catalogue de formats

Configurable via `config/output_formats.json`. **Liste initiale validée** :

### Surfaces Dayuse (site)
| ID | Surface | Largeur | Hauteur | Ratio | Notes |
|---|---|---|---|---|---|
| `home_card_mobile` | Home — card mobile | 715 | 230 | 3.11 : 1 | Banner large |
| `home_card_desktop` | Home — card desktop | 410 | 230 | 1.78 : 1 | ≈ 16:9 |
| `hotel_main_desktop` | Fiche hôtel — image principale desktop | 845 | 420 | 2.01 : 1 | Banner |
| `hotel_secondary_desktop` | Fiche hôtel — image secondaire desktop | 410 | 198 | 2.07 : 1 | Banner |
| `hotel_main_mobile` | Fiche hôtel — image principale mobile | 766 | 510 | 1.50 : 1 (3:2) | Standard |
| `rate_plan_desktop` | Rate plan — card desktop | 410 | 194 | 2.11 : 1 | Banner |
| `rate_plan_mobile` | Rate plan — card mobile | 726 | 194 | 3.74 : 1 | **Banner ultra-large** ← outpainting probable |

### Surfaces Réseaux sociaux
| ID | Surface | Largeur | Hauteur | Ratio | Notes |
|---|---|---|---|---|---|
| `youtube_thumbnail` | YouTube — thumbnail | 1280 | 720 | 1.78 : 1 (16:9) | |
| `insta_feed` | Instagram — feed (carré) | 1080 | 1080 | 1 : 1 | Crop centré |
| `insta_story` | Instagram — story / reels | 1080 | 1920 | 0.56 : 1 (9:16) | **Vertical** ← outpainting |
| `linkedin_banner` | LinkedIn — banner | 1584 | 396 | 4 : 1 | **Banner ultra-large** |
| `facebook_og` | Facebook — Open Graph | 1200 | 630 | 1.91 : 1 | |
| `twitter_card` | Twitter — large card | 1200 | 628 | 1.91 : 1 | |

> Le user pourra ajouter / retirer des formats dans `output_formats.json`.

---

## 🧠 Stratégies de crop / outpainting

### Logique en 3 niveaux

Pour chaque (photo source × format cible) :

#### Niveau 1 — Resize simple
Si **|ratio_source − ratio_target| ≤ 5 %** → simple resize sans crop.

#### Niveau 2 — Crop intelligent (Pillow + Gemini Vision)
Si le ratio cible est plus étroit que le source (en largeur ou en hauteur) :
1. **Détection des zones à préserver** via un nouveau champ Gemini Vision `crop_safe_zones` :
   - Bbox de chaque humain visible (entier — tête, torse, membres)
   - Bbox de l'amenity principale (piscine, cabana, rooftop, lit spa, etc.)
   - Bbox de tout élément critique signalé (logo hôtel discret, vue significative)
2. **Algorithme de placement de la window** :
   - Calculer toutes les positions possibles de la window cible (même dimensions que ratio cible)
   - Filtrer celles qui ne coupent AUCUN humain (priorité 1) et ne coupent PAS l'amenity (priorité 2)
   - Parmi les valides, choisir celle qui **centre le sujet d'intérêt** (centre de masse des bbox)
3. **Validation post-crop** : Gemini vérifie que la photo cropée reste cohérente

#### Niveau 3 — Outpainting génératif (Nano Banana 2)
Si le ratio cible **étend** l'image (ex: photo 16:9 → format 9:16 vertical, ou 1:1 → 4:1 banner) :
1. Calculer les zones à générer (gauche/droite/haut/bas)
2. Construire un prompt outpainting qui matche le contexte de l'image (ex: "extend the sky and palm trees naturally to the right, matching the existing pool scene")
3. Appel **Nano Banana 2** avec image originale + prompt + ratio cible
4. Validation post-outpainting : pas d'invention de mobilier, pas d'humain ajouté

**Coût** : $0.067 par expansion. Sur 15 photos × 6 formats outpaintés = ~$6/hôtel max.

---

## 🏗 Architecture technique

### Modules

#### `multi_format_cropper.py` (nouveau)
- `load_formats_config(path)` → dict des formats actifs
- `compute_crop_strategy(source_size, target_size)` → `"resize" | "crop" | "outpaint"`
- `crop_intelligently(image, target_dims, safe_zones)` → image (Pillow only)
- `outpaint_via_nano_banana(image, target_dims, context_prompt)` → image (Nano Banana)
- `generate_variants(photo_path, formats_list, safe_zones)` → dict `{format_id: bytes}`

#### `analyze.py` (extension)
Nouveau champ Gemini dans le prompt :
```json
"crop_safe_zones": {
  "humans_bboxes": [{"x": 0.45, "y": 0.30, "w": 0.20, "h": 0.55}, ...],
  "main_amenity_bbox": {"x": 0.10, "y": 0.40, "w": 0.70, "h": 0.45},
  "critical_zones": [{"x": ..., "y": ..., "w": ..., "h": ..., "label": "logo discret hotel"}]
}
```
Coordonnées normalisées 0-1.

#### `app.py` (extension)
Nouvelle étape 5 dans la pipeline (après le pack final) :
```
Step 4 (existant) → enhance_one sur 15 photos → pack final ZIP "single format"
Step 5 (nouveau) → multi_format_cropper sur 15 photos × N formats cochés → ZIP multi-format
```

Endpoint `/api/run` : accepte un nouveau param `output_formats: list[str]` dans le body.

#### `templates/index.html` (extension)
Nouvelle section dans Step 4 : **"📐 Formats de sortie"**
- Préset "Pack Dayuse complet" (toutes les surfaces site)
- Préset "Pack Social media" (Insta + LinkedIn + Facebook + Twitter + YouTube)
- Préset "Tout"
- Sélection unitaire par checkbox (toutes les checkboxes décochées par défaut sauf 1 format minimum)
- Affichage dimensions + ratio à côté de chaque format
- Indicateur de coût estimé Nano Banana en bas

---

## 📦 Output

### Structure ZIP (organisation par format)
```
{slug}_multiformat_pack.zip
├── home_card_mobile/
│   ├── photo_01.jpg  (715×230)
│   ├── photo_02.jpg
│   └── ...
├── home_card_desktop/
│   ├── photo_01.jpg  (410×230)
│   └── ...
├── hotel_main_desktop/
│   └── ...
├── insta_feed/
│   └── ...
├── insta_story/
│   └── ...
└── manifest.json   # liste des formats × photos avec stratégie utilisée
```

### `manifest.json` (audit)
```json
{
  "slug": "yotel-miami",
  "generated_at": "2026-05-05T12:00:00Z",
  "source_pack": "yotel-miami_dayaccess_final_pack.zip",
  "formats_requested": ["home_card_mobile", "insta_feed", "insta_story"],
  "variants": [
    {
      "source_photo": "official_010_unknown_10.jpg",
      "format_id": "home_card_mobile",
      "output_path": "home_card_mobile/photo_01.jpg",
      "strategy": "crop",
      "duration_ms": 12,
      "cost_usd": 0
    },
    {
      "source_photo": "official_010_unknown_10.jpg",
      "format_id": "insta_story",
      "output_path": "insta_story/photo_01.jpg",
      "strategy": "outpaint",
      "duration_ms": 4500,
      "cost_usd": 0.067,
      "outpaint_prompt": "extend the sky upward and pool deck downward..."
    }
  ]
}
```

---

## 🎨 Outpainting via Nano Banana 2

> 💡 Calibré après analyse du HAR Runwayml : Runwayml utilise Gemini 3 Pro Image **sans prompt textuel**. On reproduit cette approche minimaliste.

### Prompt template (minimaliste — calibré Runwayml)
```
Extend this image to fill the empty alpha-zero areas of the canvas, completing it as a single coherent {target_aspect_ratio} photo.

🚨 STRICT RULES:
- Pixels of the original image MUST remain identical (no recoloring, no resizing, no shift)
- The generated extensions MUST match seamlessly: same lighting, same color palette, same depth, same textures, same time of day
- DO NOT add new objects, furniture, people, signs, decorations, or text
- DO NOT change the weather, mood, or composition

Negative: new furniture, new people, signs, logos, watermarks, color shifts, lighting break, ghost outlines, CGI artifacts.
```

> Le modèle Gemini Image (Flash ou Pro) infère le contenu naturellement depuis l'image source. Notre rôle est juste de cadrer l'image dans le canvas cible et de protéger les pixels originaux.

### Paramètres Nano Banana spécifiques
- **Modèle par défaut** : `gemini-3.1-flash-image-preview` (Nano Banana 2) — $0.039/image
- **Modèle fallback** : `gemini-3-pro-image-preview` (= modèle Runwayml) — $0.067/image, déclenché si `ai_validator` rejette le résultat Flash
- **Temperature** : 0 (déterministe)
- **Input** : image RGBA avec alpha=0 dans les zones à générer + alpha=255 dans la zone à préserver
- **Validation post-IA** : `ai_validator` réutilisé pour détecter invention d'éléments / dérive lighting

---

## 📊 Paramètres Runwayml — ✅ Capturés

Capturés via HAR Chrome DevTools (`requetes.txt`, session du 2026-05-05, app=expand-image).

### Endpoint observé
- **URL** : `https://api.runwayml.com/v1/tasks`
- **Méthode** : `POST`
- **Polling** : `GET /v1/tasks/{task_id}?asTeamId={team_id}` (toutes les ~1-2s jusqu'à status `SUCCEEDED`)
- **Récupération asset** : `GET /v1/assets/{asset_id}` (URL S3 signée)

### Headers envoyés
```http
Content-Type: application/json
Accept: application/json
Origin: https://app.runwayml.com
X-Runway-Workspace: 58026031        # team / workspace ID
X-Runway-Source-Application: web    # (sur GET tasks)
Authorization: <Bearer ...>         # géré côté Runwayml session, pas exposé dans le HAR
```

### Payload (POST /v1/tasks)
```json
{
  "sessionId": "797f0639-df23-472b-b5bc-a06c08e9601c",
  "taskType": "expand_image",
  "internal": false,
  "options": {
    "image_asset_id": "93963a6e-2769-4484-86f9-7e3afe93597b",
    "target_aspect_ratio": "9:16",
    "exploreMode": false,
    "name": "Expand Image",
    "model": "gemini-3-pro-image-preview",
    "image_size": "2K",
    "creationSource": "apps",
    "creationSourceAppId": "expand-image",
    "assetGroupId": "dd46b947-8599-4014-9209-9c5f6786f3af"
  },
  "asTeamId": 58026031
}
```

> ⚠️ **Pas de `prompt`, `negative_prompt`, `guidance_scale`, `num_inference_steps`, ni `seed`** dans le payload. Le modèle Gemini gère tout en autonomie depuis l'image source.

### Workflow complet observé
1. **Upload image** → `POST /v1/uploads/{upload_id}/complete` → renvoie un `asset_id`
2. **Création tâche** → `POST /v1/tasks` (payload ci-dessus)
3. **Polling status** → `GET /v1/tasks/{task_id}` (toutes ~1-2s, jusqu'à 30+ requêtes vues dans le HAR)
4. **Récupération artefact** → `GET /v1/assets/{artifact_id}` → URL S3 signée pour télécharger l'image étendue

### Valeurs observées dans la session
- `target_aspect_ratio` : `"9:16"` (format ratio en string, **pas en pixels**)
- `image_size` : `"2K"` (résolution de sortie — probablement aussi `"1K"`, `"4K"` selon plan)
- `model` : `"gemini-3-pro-image-preview"` (Gemini 3 Pro Image — version Pro de Nano Banana)

### 🎯 Insights MAJEURS pour notre implémentation

#### 1. Runwayml utilise déjà Gemini → on peut répliquer fidèlement
Runwayml ne fait rien de plus magique que ce qu'on peut faire en direct via l'API Gemini. Le modèle `gemini-3-pro-image-preview` est la même famille que Nano Banana 2 (`gemini-3.1-flash-image-preview`) — la version Pro est plus chère mais plus précise sur le compositing. **Pas de FLUX, pas de SDXL, pas de pipeline propriétaire.**

#### 2. Pas de prompt textuel → simplification massive
Toute la magie se fait côté serveur Gemini. Notre prompt template (section "🎨 Outpainting via Nano Banana 2" plus haut) peut être **drastiquement raccourci**. Il suffit que le modèle "voie" l'image et le ratio cible.

#### 3. Le ratio est passé en string, pas en pixels
Cohérent avec notre design : `target_aspect_ratio: "9:16"` → on calcule les pixels finaux côté code (selon `image_size`).

#### 4. Notre stratégie ajustée
Au lieu d'envoyer un prompt élaboré à Nano Banana, on va :
- Préparer une image-canvas avec l'original placé dans la zone correspondante (centered/left/right)
- Marquer les zones à remplir avec un bord doux (alpha 0)
- Envoyer à Nano Banana 2 avec un **prompt minimaliste** :
  ```
  Fill the empty areas of this image to complete it naturally.
  Match the existing lighting, colors, depth, and style exactly.
  Do not add any new objects, people, furniture, or signs.
  Preserve the original pixels untouched.
  ```
- Si la qualité est insuffisante en Flash, fallback vers `gemini-3-pro-image-preview` (même modèle que Runwayml) → coût plus élevé mais qualité garantie

#### 5. Architecture du polling
On va répliquer le pattern Runwayml (POST → polling → GET asset) **localement** :
- Pas besoin de polling en réalité (l'API Gemini est synchrone)
- Mais on garde la structure async côté UI : "génération en cours…" avec spinner par variante

#### 6. Choix Flash vs Pro
| Modèle | Coût/image | Qualité outpaint | Recommandation |
|---|---|---|---|
| `gemini-3.1-flash-image-preview` (Nano Banana 2) | $0.039 | Bonne | **Default** : 90 % des cas |
| `gemini-3-pro-image-preview` (Pro) | $0.067+ | Excellente (= Runwayml) | Fallback si validation post-IA fail |

→ On commence en Flash, on bascule en Pro si `ai_validator` détecte une dérive.

### ❓ Points non capturés (impact = nul)
- Auth : la session Runwayml est gérée par cookies httpOnly invisibles dans le HAR. **Impact nul** : on n'appelle pas leur API, on a notre propre clé Gemini.
- Réponses (body des `tasks/{id}` et `assets/{id}`) : tronquées dans ce HAR. **Impact nul** : on connaît la structure attendue (status + artifact URL).

---

## 🚦 Phases de livraison

### Phase 1 — MVP crop simple (1-2h)
- Module `multi_format_cropper.py` avec `compute_crop_strategy` + `crop_intelligently` (Pillow only)
- Champ Gemini `crop_safe_zones`
- UI checkbox formats Dayuse uniquement
- Export ZIP avec dossiers par format
- **Pas d'outpainting** : si ratio cible incompatible, on warning + skip ce format
- Crit. d'acceptation : sur 5 photos test, aucune n'a humain coupé / amenity manquante

### Phase 2 — Outpainting Nano Banana (2-3h)
- Implémenter `outpaint_via_nano_banana` avec prompt template optimisé
- Validation post-outpainting via `ai_validator`
- Activer les formats outpainting (Insta story, banners ultra-larges)
- Crit. d'acceptation : sur 5 photos test outpaintées, aucune n'a d'invention manifeste

### Phase 3 — Présets + UI riche (1h)
- Boutons préset "Pack Dayuse complet" / "Pack Social media" / "Tout"
- Compteur coût estimé live selon checkboxes
- Indicateur de stratégie attendue par format (resize / crop / outpaint)

### Phase 4 — Robustesse & fallback Pro (variable)
- Retry stricter sur outpainting raté (max 2 tentatives)
- Fallback automatique Flash → Pro (`gemini-3-pro-image-preview`, le modèle utilisé par Runwayml) si validation échoue
- Toggle UI "Qualité premium" qui force le Pro pour 100 % des outpaints (coût ~×1.7)
- Skip définitif si même le Pro échoue → laisser le format en blanc dans le ZIP avec note dans le manifest

---

## ⚠️ Risques / open questions

1. **Qualité Nano Banana sur outpainting** : pas son point fort, risque de patterns artificiels. → Validation visuelle nécessaire sur 10 cas test avant prod.
2. **Coût** : 6 formats × 15 photos × outpaint_ratio (~50% des cas) × $0.067 = ~$3/hôtel. Acceptable.
3. **Performance** : 15 × 6 = 90 outputs par hôtel. Avec ~2-5s par variante (crop) ou ~5-10s (outpaint), ça donne ~5 minutes. Faut un progress bar dédié.
4. **Déduplication** : si 2 formats ont les mêmes dimensions, on génère 2 fois ou on alias ? → générer 1 fois, alias dans le ZIP.
5. **Watermark / logo Dayuse** : faut-il l'ajouter sur les formats sociaux ? → out of scope V1, ticket suivant.

---

## ✅ Critères d'acceptation V1

- [ ] L'utilisateur peut cocher 1+ formats dans l'UI Step 4
- [ ] Après le pack final, un ZIP multi-format est généré
- [ ] Les fichiers sont dans des dossiers nommés par format
- [ ] Aucune photo cropée n'a un humain coupé ou la piscine masquée (vérification visuelle sur 5 cas)
- [ ] Le `manifest.json` documente chaque variante avec sa stratégie et son coût
- [ ] Le bouton "📦 Télécharger ZIP multi-format" apparait à la fin du run
- [ ] L'UI affiche le coût estimé Nano Banana avant lancement

---

## 📚 Annexes

### Annexe A — Calcul de ratio
```python
ratio = width / height
ratio_diff = abs(source_ratio - target_ratio) / max(source_ratio, target_ratio)
strategy = (
    "resize" if ratio_diff <= 0.05 else
    "crop"   if (source_ratio > target_ratio and target_aspect == "narrower") or
                (source_ratio < target_ratio and target_aspect == "shorter") else
    "outpaint"
)
```

### Annexe B — Bbox normalisées Gemini
Toutes les coordonnées Gemini Vision sont en `[0, 1]` relatif à la dimension de l'image source :
- `x` : position gauche (0 = bord gauche, 1 = bord droit)
- `y` : position haut (0 = haut, 1 = bas)
- `w`, `h` : largeur / hauteur normalisées

### Annexe C — Mapping format-id → préset UI
- `Pack Dayuse complet` = `home_card_mobile`, `home_card_desktop`, `hotel_main_desktop`, `hotel_secondary_desktop`, `hotel_main_mobile`, `rate_plan_desktop`, `rate_plan_mobile`
- `Pack Social media` = `insta_feed`, `insta_story`, `linkedin_banner`, `facebook_og`, `twitter_card`, `youtube_thumbnail`
- `Tout` = union des deux

---

**Prochaine étape** : valider la spec V0.2 → démarrer Phase 1 (MVP crop simple Pillow + champ Gemini `crop_safe_zones`).
