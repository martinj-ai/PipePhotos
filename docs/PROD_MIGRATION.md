# Migration prod — Railway

**Statut** : 🚧 outil POC en local, à industrialiser pour Railway
**Dernière revue** : 2026-05-11

> 🎯 **Méthodologie** : à **chaque ajout de feature**, on revient sur ce document
> et on ajoute la ligne correspondante dans la section concernée. C'est un
> aide-mémoire collaboratif pour ne pas découvrir 50 incompatibilités le jour J.

## 🌍 Contexte

L'outil tourne aujourd'hui sur le **MacBook de Martin** :
- Filesystem local (`data/uploads/`, `data/output/`, `data/analyses/`, etc.)
- Flask en mode `debug=True` sur `localhost:5050`
- Pas de DB (tout en JSON sur disque)
- Secrets dans un fichier `.env` local

L'objectif est de le déployer sur **Railway** (infra cible) :
- Filesystem éphémère par défaut → tout ce qui est persistent doit aller ailleurs
- Variables d'environnement injectées par Railway
- Volume persistant Railway possible mais limité → CDN pour les médias
- Hébergement orchestré (build, deploy, scaling)

---

## 🗂 Filesystem → Stockage distant

### État actuel

Tout est dans `data/` à la racine du projet :

| Dossier | Contenu | Volume estimé / hôtel |
|---|---|---|
| `data/uploads/<slug>/` | Photos sources scrappées (jpg/png/webp) | ~50-100 MB |
| `data/analyses/<slug>/` | JSON Gemini Vision (1 par photo) | ~500 KB |
| `data/output/<slug>/enhanced/` | Photos retouchées Nano Banana | ~30 MB |
| `data/output/<slug>/multiformat/` | Variantes croppées (1 dossier par format) | ~20 MB |
| `data/output/<slug>/slowmo/` | Loops MP4 Higgsfield | ~50 MB |
| `data/output/<slug>/comparison/` | Test AB modèles GPT vs Gemini | ~30 MB |
| `data/output/laws_audit/` | Heatmaps lois (statique global) | ~1 MB |
| `data/rp/<slug>.json` | Scrap RP | ~10 KB |
| `data/progress/*.json` | État runs en cours | ~2 KB |

→ **~150 MB par hôtel**. Pour 200 hôtels = **30 GB**. Pas tenable sur le filesystem éphémère Railway.

### À faire en prod

| Catégorie | Migration | Priorité |
|---|---|---|
| **Photos sources** (`uploads/`) | **S3 / CloudFlare R2** (CDN-friendly + signed URLs pour upload) | 🔴 P1 |
| **Photos retouchées** (`enhanced/`) | **CDN** (CloudFlare / BunnyCDN) avec URLs signées 24h pour preview | 🔴 P1 |
| **Variantes multi-format** (`multiformat/`) | **CDN** + cache headers longs (1 an, immuable) | 🔴 P1 |
| **Slowmo MP4** | **CDN** vidéo (CloudFlare Stream ou similaire) | 🔴 P1 |
| **Analyses JSON** (`analyses/`) | **DB Postgres** (table `photo_analysis`, JSONB) — plus pratique pour requêtes | 🟡 P2 |
| **Scrap RP** (`rp/`) | DB Postgres (table `hotel_rp`) | 🟡 P2 |
| **Comparison HTML** | CDN ou Flask static path | 🟢 P3 |
| **Laws audit HTML** | Flask static | 🟢 P3 |
| **Progress JSON** | **Redis** (TTL 1h, partagé entre workers) | 🟡 P2 |

### Signal d'alerte ⚠️

> Toute fonction qui fait `Path(...).read_text()`, `open(path)`, `shutil.rmtree(...)`,
> `send_from_directory(...)` est candidate à la refactor.

---

## 🔐 Secrets

### État actuel

Fichier `.env` à la racine, lu via `python-dotenv` :
- `GEMINI_API_KEY` (analyse + retouche + outpaint)
- `OPENAI_API_KEY` (test AB GPT Image)
- `HF_API_KEY`, `HF_API_SECRET` (slowmo Higgsfield)

### À faire en prod

| Action | Détail |
|---|---|
| **Variables Railway** | `railway vars set GEMINI_API_KEY=...` (chaque secret du `.env`) |
| **Rotation** | Mettre en place une rotation 90j des clés (GitHub Actions ?) |
| **Quotas API** | Surveiller les quotas Gemini paid tier (1000 RPM aujourd'hui), prévoir alerting |
| **Pas de fallback** | Aucune valeur par défaut hardcodée → fail fast au boot si manquante |

---

## 🗄 Base de données (introduction)

### État actuel : ZÉRO DB

Tout est en JSON sur disque. Avantage : simplicité de POC. Inconvénient : pas requêtable, pas concurrent-safe.

### À faire en prod

**Schéma minimal Postgres** à introduire :

```sql
-- Hôtels processés
CREATE TABLE hotels (
  slug TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  city TEXT,
  vibe TEXT,
  rp_data JSONB,
  created_at TIMESTAMPTZ DEFAULT now(),
  last_run_at TIMESTAMPTZ
);

-- Photos sources
CREATE TABLE photos (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  hotel_slug TEXT REFERENCES hotels(slug),
  filename TEXT NOT NULL,
  source TEXT,  -- booking | rp | official_site | instagram
  cdn_url TEXT NOT NULL,
  uploaded_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(hotel_slug, filename)
);

-- Analyses Gemini Vision (cache)
CREATE TABLE photo_analyses (
  photo_id UUID PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
  analysis JSONB NOT NULL,
  cost_usd NUMERIC(10, 6),
  input_tokens INT,
  output_tokens INT,
  analyzed_at TIMESTAMPTZ DEFAULT now()
);

-- Runs du pipeline
CREATE TABLE pipeline_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  hotel_slug TEXT REFERENCES hotels(slug),
  resume_from TEXT,  -- scrape | selection | postprocess
  status TEXT,       -- pending | running | done | failed
  output_formats TEXT[],
  outpaint_enabled BOOLEAN,
  pipeline_started_at TIMESTAMPTZ,
  pipeline_duration_s NUMERIC,
  total_cost_usd NUMERIC(10, 6),
  total_input_tokens INT,
  total_output_tokens INT,
  manifest JSONB
);

-- Variantes multi-format générées
CREATE TABLE multiformat_variants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID REFERENCES pipeline_runs(id) ON DELETE CASCADE,
  source_filename TEXT,
  format_id TEXT,
  strategy TEXT,  -- resize | crop | outpaint
  cdn_url TEXT,
  width INT, height INT,
  cost_usd NUMERIC(10, 6),
  fallback_pro BOOLEAN
);
```

→ Migration script pour porter les `data/analyses/<slug>/*.json` actuels.

---

## ⏱ Background tasks

### État actuel

Le pipeline `/api/run` tourne **synchrone** dans la requête HTTP : un `POST /api/run` peut prendre 10-20min, le client doit attendre. C'est viable en local mais :
- Timeout proxy Railway (~5min par défaut)
- Pas de scaling horizontal possible
- Si le user ferme l'onglet, le run continue mais pas de retry / pas de notification

### À faire en prod

| Composant | Migration |
|---|---|
| **Queue** | Celery + Redis OU RQ (plus simple) |
| **Workers** | 1 service Railway dédié `worker.py` |
| **Endpoint API** | `/api/run` retourne immédiatement `{run_id, status: "queued"}` |
| **Polling** | `/api/runs/<run_id>` pour status (le front existant via `pollProgress` est déjà compatible) |
| **Webhook optionnel** | Notification Slack quand un run finit |

---

## 🌐 Static / Templates

### État actuel

- `templates/index.html` (3300+ lignes) servi par Flask render_template
- `templates/comparison_template.html`, `templates/laws_matrix_template.html` lus à la volée par les modules Python
- `static/` n'existe pas (tout est inline dans index.html)

### À faire en prod

| Action | Priorité |
|---|---|
| Pas de changement majeur — Flask gère bien les templates en prod | 🟢 P3 |
| Si le HTML grossit > 5000 lignes, splitter en partials Jinja2 | 🟡 P2 (refactor qualité de vie) |
| Servir le `static/` via CDN si on ajoute du JS/CSS lourd | 🟡 P2 |

---

## 📦 Dépendances système

### État actuel

- Python 3.9 (du système macOS) — **end-of-life** (warning à chaque run)
- ffmpeg local (présupposé installé pour slowmo)
- Playwright (chromium navigateur headless pour scraping Booking)

### À faire en prod

| Composant | Action |
|---|---|
| **Python** | Bump à 3.11 ou 3.12 dans le `Dockerfile` Railway |
| **ffmpeg** | Installé via apt-get dans le `Dockerfile` |
| **Playwright** | `playwright install chromium` dans le build step Railway |
| **`requirements.txt`** | Pinner toutes les versions (pas de `>=` flou) |

---

## 🔍 Observabilité

### État actuel

- Logs `print()` dans le terminal local
- Coûts/tokens cumulés dans la response API
- Aucune métrique persistée

### À faire en prod

| Outil | Usage |
|---|---|
| **Logs structurés** | `loguru` ou `structlog` → CloudWatch / Logtail |
| **Sentry** | Capture exceptions backend |
| **Prometheus / Datadog** | Métriques business : cost_per_hotel, success_rate, p95 duration |
| **Tracing** | Optionnel pour identifier les goulots (Gemini Vision = 90% du temps) |

---

## 📋 Checklist par feature

> Chaque fois qu'on ajoute une feature, on coche/note ici ce qui devra être adapté.

### ✅ Features actuelles

| Feature | Filesystem | DB | Secret | Async | Note prod |
|---|---|---|---|---|---|
| Scrap RP | `data/rp/` | Table `hotels.rp_data` | — | sync OK | OK |
| Scrap Booking (Playwright) | `data/uploads/` | Table `photos` | — | **doit passer en async** (long) | Stocker images sur CDN signé |
| Analyse Gemini Vision | `data/analyses/` | Table `photo_analyses` | `GEMINI_API_KEY` | async via worker | Cache → DB lookup |
| Sélection / scoring | en mémoire | sortie Run table | — | sync OK (rapide) | OK |
| Retouches IA Nano Banana | `data/output/.../enhanced/` | `multiformat_variants` ? | `GEMINI_API_KEY` | async | URLs CDN signées |
| Multi-format crop | `data/output/.../multiformat/` | `multiformat_variants` | — | async | **Cache local actuel doit aller en CDN** |
| Outpainting Nano Banana | `data/output/.../multiformat/` | idem | `GEMINI_API_KEY` | async | idem |
| Slowmo Higgsfield | `data/output/.../slowmo/` | nouvelle table `slowmo` | `HF_API_KEY/SECRET` | async (long) | CDN vidéo |
| Test AB modèles | `data/output/.../comparison/` | non persisté (audit one-shot) | `GEMINI_API_KEY`, `OPENAI_API_KEY` | sync OK | Garder en local pour audit |
| Audit matriciel lois | `data/output/laws_audit/` | non persisté (statique global) | — | sync OK | Garder en static |
| Replay / resume | lecture cache | reads `photo_analyses` | — | sync OK | **Cache lookup → DB** |
| Replay postprocess strict (skip dedup_vlm + amenity_verifier + photo_generator) | lecture cache `enhanced/` | reads `photo_analyses` | — | sync OK | **Important** : en prod aussi, le mode postprocess doit court-circuiter tous les appels Gemini de sélection (économie 1-2 min + ~$0.05 par run) |
| Multi-format lightbox grille comparative | lecture `multiformat/` | reads `multiformat_variants` | — | sync (UI only) | Render N variantes côte-à-côte (CSS grid) — pas d'impact backend ; côté CDN il faut juste assurer que toutes les URLs des variantes soient servies |
| Skip placeholders (variantes outpaint-required avec outpaint off) | — | flag `strategy='skip'` dans `multiformat_variants` | — | sync OK | OK |
| Source enhanced ancrée dans la grille compare lightbox | — | — | — | sync (UI only) | OK |
| Pool floats occasionnels dans prompts ai_add_character (déterministe par filename) | — | tracker `pool_float_used` dans table `enhanced_results` | — | sync OK | Le seed déterministe (hashlib sur filename) doit rester stable côté prod — c'est ce qui rend le résultat reproductible sur replay |
| Déterminisme Gemini Vision : temperature=0.0 sur analyze | — | — | `GEMINI_API_KEY` | async OK | Sélection reproductible run-to-run sur même hôtel |
| Gate strict Booking : amenities non déclarées Booking → bucket désactivé | — | — | — | sync OK | Plus de risque de classer en cabana une photo lookalike d'un autre hôtel |
| Veto étendu sur warnings critiques (éclairage hors-brand, pas de focus amenity) | — | rejected_low_score table | — | sync OK | Applique à TOUS les buckets, pas que non-amenity |
| Photo transformable (nuit/sombre) : pas de pénalité dominance | — | — | — | sync OK | Score +45 typique sur une photo piscine nuit, lui permet d'être rescue propre par ai_lighting |
| Outpainting checked par défaut (UI) | — | — | — | sync OK | Préférence Martin — il oubliait systématiquement |
| Wipe `enhanced/` au début d'un run complet (sauf en replay) | écrasement | — | — | sync OK | **Important prod** : en CDN, lors d'un run complet, il faudra invalider/supprimer les anciennes versions enhanced d'un slug (sinon URLs cachées 1 an pointent vers anciennes) |
| Parallélisation enhance loop (3 workers) + multi-format outpaint (3 workers) | — | — | `GEMINI_API_KEY` | async via ThreadPool | **Quota** : Gemini Image preview ~60 RPM. Sur 3 workers ça reste sous le plafond. En prod, sémaphore global si multi-tenant. |
| Parallélisation `analyze_batch` 3→8 + `amenity_verifier` 4→8 + `dedup_vlm` 3→8 | — | — | `GEMINI_API_KEY` | async via ThreadPool | Gemini Vision Flash tier paid 1 = ~2000 RPM, large marge |
| Outpaint prompt enrichi (direction explicite + anti-tile/anti-repeat) | — | — | — | sync OK | Réduit drastiquement les bugs "vues empilées" sur insta_story |
| Pool float standalone (action `ai_add_pool_float` séparée d'add_character) | — | — | `GEMINI_API_KEY` | async | Photo piscine sans humain à ajouter peut quand même recevoir une bouée — proba ~55% (Family-Friendly) déterministe par filename |
| Validator : nouvelle violation `invented_pool_float` distincte d'`invented_furniture` | — | — | `GEMINI_API_KEY` | sync OK | Whitelist conditionnelle (allowed pour `ai_add_pool_float`) ; nuit→jour aussi clarifié comme légitime via `scene_regenerated` whitelist sur `ai_lighting` |
| UI Progress multi-étapes (6 étapes pipelines, mini-barres) | — | — | — | sync (UI only) | Plus de "barre qui reset à 0" : vue globale + détail par étape avec statuts pending/active/done/skipped |
| ThreadPool enhance/multi-format robustes aux exceptions | — | error rows | — | sync OK | Avant : 1 photo crash → tout le pipeline coupé → multi-format jamais lancé. Maintenant : crash isolé, ligne d'erreur dans l'UI, pipeline continue |
| Transformable bonus bumped à +80 sur les amenities (vs +40 sur hero_ext/detail) | — | — | — | sync OK | Force les photos piscine/rooftop/spa/etc. nuit/sombre à battre des photos jour banales — sinon Gemini sous-estime trop le pillar |
| Validator violation `shallow_water_illusion` (piscine sans fond) | — | — | `GEMINI_API_KEY` | sync OK | Détecte les humains debout en eau aux genoux/cuisses sans marche visible — déclenche retry avec prompt durci |
| Progress UI v2 : steps barres pointillées (pending) + shimmer animé (active) + vibing dots + temps mm:ss | — | — | — | sync (UI only) | Vraiment plus lisible : pending ≠ done visuellement, message qui tourne sur l'étape active |
| Slot 1 : POOL en priorité absolue (Tier 0) si Booking le déclare | — | — | — | sync OK | Avant : cabana/rooftop/beach gagnaient parfois. Maintenant pool prend systématiquement le slot 1 si dispo |
| Scenarios déterministes (catalogue persona×zone_type) | — | tracker scenario_id sur step | — | sync OK | Avant : prompt persona listait 4-6 PLACEMENT OPTIONS et Gemini choisissait → dérives (transat flottant inventé). Maintenant Python sélectionne UN scenario unique via safe_zones, prompt envoyé = description précise unique sans alternative |
| Targeted clutter prompt (passe la liste `clutter_to_remove` explicite à Gemini Image) | — | — | `GEMINI_API_KEY` | async | Avant : prompt clutter générique → Gemini ne savait pas quoi retirer concrètement (ex bouée de sauvetage rouge identifiée par Vision mais pas retirée par Image). Maintenant : `build_remove_clutter_prompt([list])` met "REMOVE EXACTLY THESE" en tête |
| Expose prompts IA dans le front (debug/transparence) | — | — | — | sync (UI only) | Bloc `details` "Prompts IA envoyés" déplie le prompt exact envoyé à Gemini pour chaque step IA. Utile pour debug + audit + démo Martin |
| Multi-format skip diagnostic logs (Martin : "ne tourne plus") | — | — | — | sync OK | Côté serveur on log explicitement pourquoi multi-format est skip (output_formats vide / enhanced_dir absent / 0 jpg) pour debug |
| Désactivation local_smart_crop (redondant avec multi-format) | — | — | — | sync OK | `_maybe_crop_step` retourne toujours None. Le cadrage final est géré par la step 5 multi-format par format de sortie |
| Pool floats AUTORISÉS sur vues aériennes piscine | — | — | `GEMINI_API_KEY` | async | Référence Dayuse homepage : flamingo en vue aérienne. La bouée est visible en aerial (contrairement à un humain trop petit) |
| Validator `architecture_invented` (NON whitelistée pour ai_lighting) | — | rejected_low_score | `GEMINI_API_KEY` | sync OK | Détecte la fabrication de fenêtres/baies/openings inventées (cas tricky alcôves néon → fenêtres ensoleillées). Séparée de `architecture_changed`, déclenche retry + fallback original. PROMPT_ENSOLEILLEMENT durci en parallèle |
| Analyze Gemini Vision en 2 passes (light sur toutes / rich sur finalistes) | `data/analyses/` (merge in-place) | `photo_analyses.rich_payload` JSONB optionnel | `GEMINI_API_KEY` | async ThreadPool 8 workers | **Optimisation perf** : `SYSTEM_PROMPT` light (~3055 tokens) sans `safe_zones_for_humans` / `crop_safe_zones` / `recommended_crop` / `slowmo_potential` ; `SYSTEM_PROMPT_RICH` (~1550 tokens) génère ces 4 champs UNIQUEMENT sur les ~12-18 photos sélectionnées par ordering. Économise ~60% des tokens output. En prod : worker async distinct pour la pass2 (priorité après ordering, avant enhance). Skip pass2 si `use_enhance_cache=True` (mode replay strict). Merge sur disque dans le même JSON (trace `N2b_analyze_rich`). Front : étape dédiée "🎯 Détails IA finalistes" entre amenities et retouche. |
| Retry policy Gemini : fail-fast sur spend cap + MAX_RETRIES 6→3 + cap 90s→30s | — | — | `GEMINI_API_KEY` | async OK | **Critical bugfix** : avant, sur spend cap dépassé chaque photo bouffait 4+8+16+32+64+90+90 = 304s de backoff avant d'abandonner → wall-time multiplié par 10. Maintenant : (1) détection explicite des erreurs non-retryables (`spending cap`, `billing`, `permission`, `invalid api key`) qui raise direct, (2) MAX_RETRIES = 3 (worst case 28s), (3) wait max 30s. Côté `/api/run`, si > 30% des analyses échouent → abort HTTP 502 avec message UX clair pointant vers `ai.studio/spend`. En prod : monitoring du fail_rate par batch + alerte budget cap. |

### ⏳ Features à venir (à compléter)

> Quand tu ajoutes une feature, ajoute une ligne ici.

| Feature | Filesystem | DB | Secret | Async | Note prod |
|---|---|---|---|---|---|
| _(prochaine)_ | | | | | |

---

## 🚀 Roadmap migration

### M0 — Préparation (avant déploiement)
- [ ] Audit dépendances : pinner versions
- [ ] Bump Python 3.11
- [ ] Dockerfile + `railway.toml`
- [ ] Setup variables d'environnement Railway

### M1 — Stockage médias
- [ ] Créer bucket S3/R2 (régions : eu-west-3 ou us-east-1 selon Gemini latency)
- [ ] Refactor `data/uploads/`, `data/output/.../enhanced/`, `multiformat/`, `slowmo/` → upload S3 avec URLs signées
- [ ] Migration des fichiers existants depuis local vers S3

### M2 — DB
- [ ] Provision Postgres Railway
- [ ] Créer migrations (Alembic)
- [ ] Refactor lecture/écriture des analyses, runs, photos
- [ ] Script de migration des `data/analyses/<slug>/*.json` → table

### M3 — Async
- [ ] Provision Redis Railway
- [ ] Setup RQ worker
- [ ] Refactor `/api/run` en async + endpoint `/api/runs/<id>`
- [ ] Adapter le frontend (déjà compatible via `pollProgress`)

### M4 — Observabilité
- [ ] Sentry
- [ ] Logs structurés
- [ ] Dashboard coûts/jour (Datadog ou rolled own)

### M5 — Sécurité / Auth
- [ ] Auth utilisateur (Railway Auth ? Clerk ?)
- [ ] Rate limiting par user
- [ ] Audit log des runs

---

## 📝 Comment utiliser ce document

1. **À chaque feature ajoutée**, ouvrir ce fichier et mettre à jour la section "Features actuelles" ou "Features à venir"
2. **Avant chaque commit**, vérifier que les nouveaux paths/secrets/calls API sont notés
3. **Au moment du déploiement**, ce document devient la check-list officielle
4. **Garder à jour la "Dernière revue"** en haut du fichier

> Si tu lis ce fichier dans 6 mois et que tu trouves une catégorie qui manque → ajoute-la.
> Mieux vaut un warning précoce qu'un bug en prod.
