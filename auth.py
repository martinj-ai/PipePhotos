"""SSO Google OAuth pour PipePhotos — restriction au domaine @dayuse.com.

Architecture :
- Authlib gère le flow OAuth 2.0 / OpenID Connect avec Google
- Flask session (signed cookie) stocke {email, name, picture} après login
- Décorateur `@login_required` + middleware `before_request` qui forcent l'auth
  sur TOUTES les routes sauf les publiques (/login, /auth/*, /healthcheck, /static)
- Filtre domaine : seul un email se terminant par `@{ALLOWED_EMAIL_DOMAIN}` peut
  obtenir une session (sinon 403 Forbidden)

Variables d'env nécessaires :
- `GOOGLE_OAUTH_CLIENT_ID` : Client ID du projet Google Cloud
- `GOOGLE_OAUTH_CLIENT_SECRET` : Client Secret correspondant
- `FLASK_SECRET_KEY` : clé pour signer les cookies session (32+ bytes hex random)
- `ALLOWED_EMAIL_DOMAIN` : domaine autorisé (default 'dayuse.com')
- `AUTH_DISABLED` : si "1", désactive complètement l'auth (debug local uniquement)

Setup Google Cloud Console :
1. APIs & Services > OAuth consent screen → Internal (limite à l'organisation)
2. Credentials > Create OAuth Client ID > Web application
3. Authorized redirect URIs :
   - http://localhost:5050/auth/callback (dev)
   - https://<railway-url>/auth/callback (prod)
"""

from __future__ import annotations

import os
import secrets
from functools import wraps

from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint, abort, redirect, render_template_string, request, session, url_for, jsonify,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ALLOWED_DOMAIN = (os.getenv("ALLOWED_EMAIL_DOMAIN") or "dayuse.com").lower()
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "0") == "1"

# Routes accessibles sans login (paths exacts ou préfixes)
PUBLIC_EXACT_PATHS = {"/login", "/healthcheck", "/favicon.ico"}
PUBLIC_PREFIXES = ("/auth/", "/static/")

oauth = OAuth()
auth_bp = Blueprint("auth", __name__)


def init_oauth(app):
    """Initialise OAuth client + secret_key Flask.

    À appeler une fois au boot, avant register_blueprint(auth_bp).
    """
    # Secret key pour signer les session cookies (sans ça, sessions désactivées)
    app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
    # Cookie config sécurisé en prod (HTTPS) — adaptatif selon DEBUG
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # En prod (Railway), HTTPS forcé → SESSION_COOKIE_SECURE=True
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("RAILWAY_ENVIRONMENT") is not None

    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")

    if AUTH_DISABLED:
        print("[auth] ⚠️  AUTH_DISABLED=1 — SSO désactivé (debug only). NE PAS utiliser en prod !")
        return

    if not client_id or not client_secret:
        print("[auth] ⚠️  GOOGLE_OAUTH_CLIENT_ID/SECRET manquants — SSO désactivé. "
              "Définir les vars d'env pour activer le login.")
        return

    oauth.init_app(app)
    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    print(f"[auth] ✓ OAuth Google configuré · domaine autorisé : @{ALLOWED_DOMAIN}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Middleware : exige login sur TOUTES les routes sauf publiques
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def require_login_globally(app):
    """Hook before_request qui redirige vers /login si pas de session active.

    Exceptions : routes publiques (login page, auth callbacks, static, healthcheck).
    """
    @app.before_request
    def _check_auth():
        if AUTH_DISABLED:
            return None
        if not _has_oauth_configured():
            # Si l'OAuth n'est pas configuré (vars d'env absentes) → pas d'auth
            # (fail open en dev, mais loggué au boot pour alerter)
            return None

        path = request.path or "/"
        # Static & assets : toujours libre
        if path in PUBLIC_EXACT_PATHS:
            return None
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return None

        if not session.get("user"):
            # Pour les requêtes XHR/API, retourne 401 JSON plutôt qu'un redirect HTML
            if path.startswith("/api/") or request.headers.get("Accept", "").startswith("application/json"):
                return jsonify({"error": "Authentication required", "login_url": "/login"}), 401
            return redirect(url_for("auth.login"))
        return None


def _has_oauth_configured() -> bool:
    """True si client_id + client_secret sont définis (et donc OAuth utilisable)."""
    return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID")) and bool(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"))


def login_required(f):
    """Décorateur pour des routes spécifiques (alternative au global before_request)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_DISABLED or not _has_oauth_configured():
            return f(*args, **kwargs)
        if not session.get("user"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def current_user():
    """Helper : retourne le dict user de la session, ou None."""
    return session.get("user")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Routes /login, /auth/google, /auth/callback, /logout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <title>Sign in — PipePhotos</title>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: 'Manrope', -apple-system, BlinkMacSystemFont, sans-serif;
      background: #F4F4F6;
      background-image:
        radial-gradient(circle at 15% 20%, rgba(255, 175, 54, 0.06) 0%, transparent 45%),
        radial-gradient(circle at 85% 80%, rgba(110, 105, 172, 0.05) 0%, transparent 45%);
      min-height: 100vh;
      margin: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
      color: #292935;
    }
    .card {
      background: white;
      border-radius: 20px;
      padding: 44px 40px;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04), 0 12px 36px rgba(0, 0, 0, 0.08);
      max-width: 420px;
      width: 100%;
      text-align: center;
      border: 1px solid #EAEAEB;
    }
    .logo-mark {
      width: 56px;
      height: 56px;
      border-radius: 16px;
      background: linear-gradient(135deg, #FFAF36 0%, #FFC536 50%, #FF9F26 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 0 auto 20px;
      box-shadow: 0 8px 24px rgba(255, 175, 54, 0.35);
      font-size: 24px;
      font-weight: 800;
      color: white;
      letter-spacing: -0.5px;
    }
    h1 {
      font-size: 26px;
      font-weight: 800;
      margin: 0 0 6px;
      color: #292935;
      letter-spacing: -0.8px;
    }
    .subtitle { color: #54545d; font-size: 14px; margin-bottom: 32px; line-height: 1.5; }
    .google-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 12px;
      width: 100%;
      padding: 14px 24px;
      border: 1px solid #EAEAEB;
      border-radius: 100px;
      background: white;
      color: #292935;
      font-family: inherit;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      text-decoration: none;
    }
    .google-btn:hover {
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.10);
      transform: translateY(-1px);
    }
    .google-btn svg { width: 20px; height: 20px; }
    .footer {
      margin-top: 24px;
      font-size: 12px;
      color: #94a3b8;
    }
    .footer strong { color: #54545d; }
    .error {
      background: #fee2e2;
      color: #991b1b;
      padding: 12px 16px;
      border-radius: 12px;
      font-size: 13px;
      margin-bottom: 20px;
      border: 1px solid #fecaca;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo-mark">D</div>
    <h1>Day Access Photo Tool</h1>
    <p class="subtitle">Pipeline IA photos Dayuse<br>Connecte-toi pour accéder à l'outil.</p>
    {% if error %}
      <div class="error">⚠️ {{ error }}</div>
    {% endif %}
    {% if oauth_ready %}
    <a href="/auth/google" class="google-btn">
      <svg viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
      Sign in with Google
    </a>
    {% else %}
    <div class="error">⚠️ OAuth Google non configuré. Variables <code>GOOGLE_OAUTH_CLIENT_ID</code> et <code>GOOGLE_OAUTH_CLIENT_SECRET</code> manquantes côté serveur.</div>
    {% endif %}
    <div class="footer">
      Accès restreint au domaine <strong>@{{ domain }}</strong>
    </div>
  </div>
</body>
</html>"""


@auth_bp.route("/login")
def login():
    """Page de login avec bouton Google."""
    if session.get("user"):
        return redirect("/")
    error = request.args.get("error")
    return render_template_string(
        _LOGIN_HTML,
        error=error,
        domain=ALLOWED_DOMAIN,
        oauth_ready=_has_oauth_configured(),
    )


@auth_bp.route("/auth/google")
def google_login():
    """Déclenche le flow OAuth : redirige vers Google consent screen."""
    if not _has_oauth_configured():
        return redirect("/login?error=OAuth+non+configur%C3%A9+c%C3%B4t%C3%A9+serveur")
    redirect_uri = url_for("auth.callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/auth/callback")
def callback():
    """Callback Google : valide le token, check domaine, ouvre session."""
    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        return redirect(f"/login?error=Erreur+OAuth+%3A+{str(e)[:80]}")

    userinfo = token.get("userinfo") if isinstance(token, dict) else None
    if not userinfo:
        # Fallback : appel manuel à userinfo endpoint
        try:
            userinfo = oauth.google.parse_id_token(token, nonce=None) if token else {}
        except Exception:
            userinfo = {}

    email = (userinfo.get("email") or "").lower().strip()
    email_verified = userinfo.get("email_verified", True)  # default true (Google verified)

    if not email:
        return redirect("/login?error=Email+absent+de+la+r%C3%A9ponse+Google")
    if not email_verified:
        return redirect("/login?error=Email+non+v%C3%A9rifi%C3%A9+par+Google")
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        return redirect(
            f"/login?error=Acc%C3%A8s+restreint+aux+emails+%40{ALLOWED_DOMAIN}+%28re%C3%A7u+%3A+{email}%29"
        )

    # Session OK
    session["user"] = {
        "email": email,
        "name": userinfo.get("name") or email.split("@")[0],
        "picture": userinfo.get("picture", ""),
    }
    # Redirige vers la page d'origine si on en avait sauvé une
    next_url = session.pop("next_url", None) or "/"
    return redirect(next_url)


@auth_bp.route("/logout")
def logout():
    """Détruit la session côté serveur + redirige vers login."""
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/api/me")
def me():
    """Endpoint utilitaire pour le front : retourne l'user courant ou 401."""
    user = current_user()
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "user": user})


@auth_bp.route("/healthcheck")
def healthcheck():
    """Endpoint public pour le healthcheck Railway (pas de login requis)."""
    return jsonify({"ok": True, "service": "pipephotos"})
