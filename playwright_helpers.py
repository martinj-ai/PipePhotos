"""Helpers Playwright — flags container-friendly partagés par tous les scrapers.

Sans ces flags, Chromium crash systématiquement sur Railway / Docker / Heroku :
- `--no-sandbox` : sandbox kernel Linux refusée par le container
- `--disable-setuid-sandbox` : idem
- `--disable-dev-shm-usage` : `/dev/shm` trop petit (~64MB sur Railway) → out-of-memory
- `--disable-gpu` : pas de GPU dispo
- `--disable-blink-features=AutomationControlled` : anti-detection DataDome (Booking, Expedia)

Usage :
    from playwright_helpers import chromium_launch_args
    browser = p.chromium.launch(headless=True, args=chromium_launch_args())
"""

from __future__ import annotations

import os


def _is_container() -> bool:
    """Détecte si on tourne dans un container (Railway/Heroku/Docker)."""
    return bool(
        os.getenv("RAILWAY_ENVIRONMENT")
        or os.getenv("DYNO")  # Heroku
        or os.path.exists("/.dockerenv")
        or os.getenv("FORCE_CONTAINER_FLAGS") == "1"
    )


# Flags toujours utiles (anti-detect + perf)
_BASE_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

# Flags container-only (peuvent dégrader sandbox sécurité en local, donc opt-in)
_CONTAINER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


def chromium_launch_args(extra: list[str] | None = None) -> list[str]:
    """Retourne les args appropriés pour `p.chromium.launch(args=...)`.

    Args:
        extra : flags additionnels à ajouter (cas spécifiques par scraper).

    En local : juste les flags anti-detect.
    En container (Railway, etc.) : ajoute --no-sandbox + autres flags critiques.
    """
    args = list(_BASE_ARGS)
    if _is_container():
        args.extend(_CONTAINER_ARGS)
    if extra:
        args.extend(extra)
    return args
