# Déploiement Railway — PipePhotos

## Prérequis

- Compte Railway (https://railway.app)
- Repo GitHub `Dayuse-Labs/PipePhotos` connecté à Railway
- Variables d'env API (Gemini, OpenAI, Higgsfield)

## Étapes

### 1. Créer le projet Railway

```bash
# Option CLI (recommandé)
railway login
railway init
railway link  # → choisir Dayuse-Labs/PipePhotos

# Option UI : New Project → Deploy from GitHub repo → Dayuse-Labs/PipePhotos
```

### 2. Ajouter l'addon Postgres

```bash
railway add --plugin postgresql
```

Railway injecte automatiquement la variable d'env `DATABASE_URL` au format
`postgres://user:password@host:port/db`. Le code `audit_db.py` la détecte
et normalise vers `postgresql://` (SQLAlchemy 2.x compatible).

### 3. Variables d'env à configurer

Dans Railway UI → Variables :

| Variable | Valeur | Critique ? |
|---|---|---|
| `DATABASE_URL` | (auto-injecté par addon Postgres) | ✅ Oui (sinon fallback SQLite éphémère) |
| `GEMINI_API_KEY` | (depuis Google Cloud Console) | ✅ Oui |
| `OPENAI_API_KEY` | (depuis OpenAI dashboard) | ✅ Oui (pour A/B test GPT Image) |
| `HF_API_KEY` | (depuis Higgsfield dashboard) | ✅ Oui (slowmo Kling) |
| `HF_API_SECRET` | (depuis Higgsfield dashboard) | ✅ Oui (slowmo Kling) |
| `USE_LEGACY_SCENARIO_CATALOG` | `0` (default, V5 Vision) ou `1` (legacy) | Optionnel |
| `KEEP_AI_INTERMEDIATES` | `1` (default) ou `0` (cleanup) | Optionnel |

### 4. Volume de stockage pour les photos

Railway filesystem est **éphémère** : `data/uploads/`, `data/output/`, `data/analyses/`
seront perdus à chaque redéploy.

Options :
- **Option A — Volume Railway** : `railway volume add --mount-path /app/data --size 5GB`
  → Persistance simple, mais limité à 1 instance.
- **Option B — S3/R2** : refacto upload/download photos pour passer par bucket cloud.
  → Plus robuste, multi-instance possible.

Pour le POC, **option A** suffit.

### 5. Build & déploy

```bash
railway up
# OU push sur main → auto-deploy via webhook GitHub
```

Railway build :
1. Lit `runtime.txt` → Python 3.11
2. `pip install -r requirements.txt`
3. `playwright install --with-deps chromium` (pour Booking scraper)
4. Boot via `Procfile` : `gunicorn app:app --bind 0.0.0.0:$PORT`

### 6. Vérifier le déploiement

```bash
railway status
railway logs --tail 100
railway open  # → ouvre l'URL générée
```

Test rapide :
```bash
curl https://<your-app>.up.railway.app/api/audit-dashboard
# → doit retourner {"kpis": ..., "backend": "postgresql"}
```

→ Si `"backend": "sqlite"` → la variable `DATABASE_URL` n'est pas injectée
correctement. Vérifier l'addon Postgres attaché.

## Migration des données existantes (optionnel)

Si tu veux importer ta DB locale `data/audit.db` dans la Postgres Railway :

```bash
# 1. Export local
sqlite3 data/audit.db .dump > audit_dump.sql

# 2. Import sur Railway Postgres (CLI psql)
railway run psql < audit_dump.sql
# OU via Railway connect : railway connect postgresql
```

Note : le dump SQLite n'est pas 100% compatible Postgres (BOOL vs INT). Pour
une migration propre, écrire un script `migrate_sqlite_to_postgres.py` avec
SQLAlchemy qui lit depuis SQLite via `DATABASE_URL=sqlite://...` et écrit
sur Postgres via une 2e connexion.

## Coûts estimés Railway

| Composant | Estimation |
|---|---|
| Compute (1 service, 0.5 vCPU, 512 MB RAM) | ~$5-10/mois |
| Postgres addon (256 MB) | ~$5/mois |
| Volume 5 GB | ~$1/mois |
| Bandwidth (estim. 10 GB/mois) | ~$1/mois |
| **Total** | **~$12-17/mois** |

Free tier Railway : $5/mois gratuits → gratuit jusqu'à modérément utilisé.

## Rollback

```bash
railway redeploy <previous-deployment-id>
```

Ou rollback via Git : `git push` une version antérieure → Railway redeploy.
