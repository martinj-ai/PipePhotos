# Slow-motion Loop (Cinemagraph) — Spécification

**Version** : 0.1
**Auteurs** : Martin × Claude
**Statut** : 🔧 V1 implémenté — Higgsfield Kling 2.1 Pro + post-process ping-pong ffmpeg

---

## 🎯 Problème / besoin

En plus du pack photo final, on veut produire **une animation courte et bouclée** pour une des photos du pack (typiquement la hero / slot 1 ou la photo avec le meilleur potentiel de mouvement ambiant). L'objectif est un **cinemagraph** : photo qui prend vie sur un détail (eau qui ondule, voilages qui flottent, feuillage qui bouge, flammes qui dansent), reste 100% fidèle au cadre original, boucle de manière invisible.

Use cases visés :
- Hero animée sur la fiche hôtel Day Pass (au lieu d'une image statique)
- Stories Instagram / Reels courtes
- Bannière web

---

## 👤 User story

> En tant que CRM/Marketing manager, après avoir lancé le pipeline et obtenu mon pack de 15 photos retouchées, je veux qu'**une photo finale soit automatiquement convertie en mp4 loop seamless** que je puisse intégrer en Hero animé sans faire de montage manuel.

---

## 🧬 Architecture

### Pipeline (étape 4.5, après retouches IA, avant multi-format)

```
photo finale enhanced (jpeg)
    │
    ▼
Higgsfield upload_file → URL CDN
    │
    ▼
POST kling-video/v2.1/pro/image-to-video
  prompt = ambient motion (water/curtains/foliage/...)
  duration = 5s
    │
    ▼
download mp4 (5s, motion ambiant)
    │
    ▼
ffmpeg ping-pong : clip + reverse(clip) → 10s loop
    │
    ▼
data/output/{slug}/slowmo/{filename}.mp4
```

### Sélection de la photo cible

Gemini analyse chaque photo et remplit `slowmo_potential` :

```json
{
  "has_motion_subject": true,
  "motion_subject": "water | curtains | foliage | fire | steam | fountain | none",
  "motion_strength": 0-100,
  "reason": "..."
}
```

Le module `slowmo_higgsfield.pick_slowmo_target()` :
1. Filtre les photos du pack final ordonné qui ont `has_motion_subject = true`
2. Trie par `motion_strength` desc, puis slot asc
3. Retourne la 1ère candidate
4. **Fallback** : slot 1 (hero) avec `motion_subject = "ambient"` si aucune candidate qualifiée

---

## 🎬 Pourquoi le ping-pong ffmpeg ?

### Ce qu'on voulait initialement : First-Last-Frame natif

Idéalement, on aurait passé `start_image = end_image = même photo` à un modèle vidéo, qui aurait généré un mouvement continu revenant à l'origine → loop parfait avec mouvement directionnel possible.

### Ce qui n'est pas disponible

L'**API officielle Higgsfield** (`platform.higgsfield.ai`, auth clé+secret) **ne supporte PAS** le mode FLF. Les modèles disponibles acceptent uniquement `prompt + image_url + duration` :

| Modèle | Endpoint | FLF supporté ? |
|---|---|---|
| Kling 2.1 Pro | `kling-video/v2.1/pro/image-to-video` | ❌ non |
| DoP standard | `higgsfield-ai/dop/standard` | ❌ non |
| DoP preview | `higgsfield-ai/dop/preview` | ❌ non |
| Seedance v1 Pro | `bytedance/seedance/v1/pro/image-to-video` | ❌ non |

Le FLF natif (`kling-o3-flf`, `kling3` avec `end_image_url`) n'existe que sur le **backend web non-officiel** (`cloud.higgsfield.ai`), accessible uniquement via scraping de cookie Clerk JWT — fragile (cookies rotés ~7 jours), instable (Cloudflare + Datadome bot protection), et probablement contre les ToS Higgsfield. **Écarté**.

### Solution retenue : ping-pong post-process

1. On génère 5s de mouvement **ambiant et non-directionnel** via Kling 2.1 Pro (`prompt: "subtle gentle ripples [...] no camera movement, locked-off shot"`)
2. On chaîne `clip + reverse(clip)` via ffmpeg → 10s de loop **mathématiquement parfait** (la dernière frame du clip = la première du reverse, et vice-versa)

**Pourquoi ça marche bien** : sur des sujets de mouvement non-directionnel (eau, voilages, feuillage, vapeur), aller "en arrière" est visuellement indistinguable d'aller "en avant". Le ping-pong est invisible.

**Limites** : ne marche PAS sur des sujets directionnels (fontaine qui éclabousse vers le haut, flammes qui montent — le reverse va se voir). Pour ces cas, on peut basculer en mode **crossfade** (option B ci-dessous) ou désactiver le slowmo.

---

## 🔬 Alternatives évaluées et écartées

### Option A — Ping-pong loop (✅ retenue)
- ✅ Loop mathématiquement parfait
- ✅ Marche sur eau / voilages / feuillage / vapeur (sujets non-directionnels)
- ✅ Pas de couture visible
- ❌ Crée un effet "stop-motion" sur sujets directionnels (flammes, fontaine)

### Option B — Crossfade loop (post-process ffmpeg)
- Génère 5s, crossfade 0.5s entre fin et début → 5s loop
- ✅ Marche sur sujets directionnels
- ❌ Léger blur visible au point de couture
- ❌ Durée = durée native du clip (pas de doublement)
- **Statut** : implémentable en option future via flag `SLOWMO_LOOP_MODE=crossfade`

### Option C — Prompt strict "return to start"
- Demander à Kling de générer un mouvement qui revient à la position initiale
- ❌ Inconsistant : Kling ignore souvent cette consigne
- **Statut** : abandonnée

### Option D — FLF natif via backend web Higgsfield (`kling-o3-flf`)
- ✅ Loop parfait avec mouvement directionnel possible (start = end → vraie boucle continue)
- ❌ Auth via cookies Clerk JWT rotés ~7 jours
- ❌ Cloudflare + Datadome bot protection → blocages random
- ❌ Schema undocumented, peut breaker sans préavis
- ❌ Probablement contre les ToS Higgsfield
- **Statut** : à reconsidérer si Higgsfield ouvre FLF sur l'API officielle

### Option E — Concurrents (Runway Gen-3, Luma Ray2, Pika)
- Luma Ray2 a un mode "loop" natif explicite
- Runway Gen-3 supporte first-last-frame
- **Statut** : à benchmarker contre la stack Higgsfield actuelle si la qualité ambiante de Kling 2.1 Pro déçoit

### Option F — Nano Banana / Gemini image generation frame-par-frame
- Générer N frames intermédiaires + assembler en mp4
- ❌ Aucune cohérence temporelle entre frames (modèle image, pas vidéo)
- ❌ Flicker, dérives d'identité, pas de motion blur
- **Statut** : abandonnée d'emblée

---

## ⚙️ Configuration

### Variables d'environnement

```bash
# Obligatoires
HF_API_KEY=...
HF_API_SECRET=...
# (alternative : HF_KEY="key:secret" en une seule var)

# Optionnels (défauts en code)
HIGGSFIELD_SLOWMO_MODEL=kling-video/v2.1/pro/image-to-video  # ou higgsfield-ai/dop/standard
HIGGSFIELD_SLOWMO_DURATION=5                                  # secondes (clip brut avant ping-pong)
HIGGSFIELD_SLOWMO_PRICE_USD=0.35                              # pour le tracking spend
SLOWMO_DRY_RUN=1                                              # skip appel API en dev
```

### Activation côté UI

Step 4 (avant lancement pipeline) → checkbox `🎬 Générer un slow-motion loop pour la hero`. Désactivé par défaut (coût + latence).

---

## 🎨 Prompts par sujet (`slowmo_higgsfield.PROMPTS_BY_SUBJECT`)

Tous les prompts forcent **"no camera movement, locked-off shot, static composition"** pour neutraliser la tendance de Kling/DoP à pousser des camera moves cinématiques par défaut. On veut UNIQUEMENT le mouvement du sujet.

| Sujet | Prompt résumé |
|---|---|
| `water` | Subtle gentle ripples, slow ambient surface motion, reflection shimmer |
| `curtains` | Soft gentle breeze, slow fabric sway |
| `foliage` | Gentle wind on leaves and plants, ambient natural sway |
| `fire` | Gentle dancing flames, soft flicker, ember glow |
| `steam` | Slow rising steam and mist, gentle drift |
| `fountain` | Gentle water flow, soft continuous splashing |
| `ambient` (fallback) | Very gentle natural motion, photorealistic |

---

## 💸 Coût estimé

| Modèle | Prix indicatif (5s clip) |
|---|---|
| `higgsfield-ai/dop/standard` | ~$0.10 |
| `higgsfield-ai/dop/preview` | ~$0.20 |
| `bytedance/seedance/v1/pro/image-to-video` | ~$0.20 |
| `kling-video/v2.1/pro/image-to-video` (✅ default) | ~$0.35 |

**1 slowmo par hôtel** → coût marginal de ~$0.35/hôtel. Tracké dans `cost.slowmo_usd`.

---

## 🚧 Limites connues

- **FLF non disponible** sur l'API officielle → contraint au ping-pong ou crossfade
- **Sujets directionnels mal gérés** (flammes hautes, fontaines, smoke directionnel) → le reverse du ping-pong se voit
- **Photos avec humains visibles** : le slowmo va animer cheveux/vêtements de manière weird → exclus dans le prompt Gemini (`has_motion_subject = false` si humains au premier plan)
- **Latence** : 30s à 2 min par génération selon la file Higgsfield
- **Quota Higgsfield** : pas de fallback si la quota est dépassée — l'erreur remonte dans `slowmo.error`

---

## 🗺 Roadmap

- **V1.1** — Mode crossfade en option (`SLOWMO_LOOP_MODE=crossfade`)
- **V1.2** — Génération de plusieurs candidates (top 3 motion_strength) + UI pour choisir
- **V1.3** — Variantes de format pour le slowmo (16:9 desktop / 9:16 story / 1:1 feed) via ffmpeg crop
- **V2** — Si Higgsfield ouvre FLF sur l'API officielle → bascule pour les sujets directionnels
- **V2.1** — Évaluation Luma Ray2 / Runway Gen-3 en alternative pour comparer la qualité
