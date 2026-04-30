"""Génération photo full IA quand une amenity est manquante dans le pack final.

Règles métier strictes :
- SPA : OK à générer (massage table générique, pas un endroit identifiable)
- BAR / cocktail : OK si l'hôtel a un bar selon RP
- FOOD / plat : INTERDIT (risque d'inventer un menu inexistant)
- Toute autre amenity : INTERDIT (architecture spécifique = trop risqué)

Coût : ~$0.067 par photo générée (Nano Banana 2 text-to-image).
Toutes les photos générées sont marquées is_fully_generated=true et taggées clairement.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

NANO_BANANA_MODEL = os.getenv("NANO_BANANA_MODEL", "gemini-3.1-flash-image-preview")
NANO_BANANA_PRICE_USD = 0.067

# Prompts par amenity générique. Tous calibrés "ton brand Dayuse / Kodak Vision3 500T".
GENERIC_PROMPTS = {
    "spa": """A serene spa massage room, premium-accessible feel.
Single empty massage table with crisp white linens and folded towels, soft warm natural light from a window or sheer curtain, lush green plants in the corner, neutral wood tones, minimalist design, calm atmosphere.
Photorealistic editorial lifestyle photography. Kodak Vision3 500T look: warm highlights, neutral tones, subtle film grain, soft contrast. Sony A7R IV / Canon R5 aesthetic, 35mm, f/4, ISO 100.
Composition: rule of thirds, table positioned slightly off-center, soft natural light coming from the side. No people. No identifying brand elements. No logos. Premium-accessible Dayuse mood — chaleureuse parenthèse, never sterile.

NEGATIVE PROMPT: clinical/hospital look, harsh fluorescent lighting, plastic shine, CGI, over-saturated, generic stock photo feel, watermarks, logos, text in image, identifying brand elements.""",

    "bar": """A vibrant outdoor bar scene with one signature cocktail in the foreground.
A single tall glass with a tropical-style cocktail (orange/yellow tones, ice, mint or fruit garnish, simple glass), placed on a rustic wood or marble bar counter. Soft tropical greenery and warm golden-hour ambient light in the background. No specific bar architecture visible — just bokeh / blurred background suggesting an outdoor pool or rooftop atmosphere.
Photorealistic editorial lifestyle photography. Kodak Vision3 500T look: warm highlights, golden hour vibe, soft film grain. 50mm, f/2.8, shallow depth of field. Premium-accessible feel.

NEGATIVE PROMPT: full bar in focus, identifiable architecture, multiple cocktails, food on plate, people, hands, watermarks, brand logos, text in image, sterile commercial product shot.""",
}


_GENAI_CLIENT = None


def _get_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante")
        _GENAI_CLIENT = genai.Client(api_key=api_key)
    return _GENAI_CLIENT


def can_generate(category: str) -> bool:
    """True si on est autorisé à générer une photo full IA pour cette catégorie."""
    return category in GENERIC_PROMPTS


def generate_photo_for_amenity(category: str, output_path: Path,
                               model: str = NANO_BANANA_MODEL) -> dict:
    """Génère une photo full IA générique pour une catégorie autorisée.

    Returns:
        {output_path, cost_usd, duration_ms, prompt_used, error?}
    """
    if not can_generate(category):
        return {
            "output_path": None,
            "error": f"Catégorie '{category}' non autorisée à la génération full IA (règle métier)",
            "cost_usd": 0,
            "duration_ms": 0,
        }

    prompt = GENERIC_PROMPTS[category]
    client = _get_client()
    t0 = time.time()
    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt],
        )
    except Exception as e:
        return {
            "output_path": None,
            "error": f"Génération échouée : {type(e).__name__}: {str(e)[:200]}",
            "cost_usd": 0,
            "duration_ms": int((time.time() - t0) * 1000),
        }

    # Extraction de l'image
    image_data = None
    for part in response.candidates[0].content.parts:
        if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
            image_data = part.inline_data.data
            break
    if not image_data:
        return {
            "output_path": None,
            "error": "Nano Banana n'a pas retourné d'image",
            "cost_usd": 0,
            "duration_ms": int((time.time() - t0) * 1000),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(image_data)

    return {
        "output_path": str(output_path),
        "category": category,
        "is_fully_generated": True,
        "prompt_used": prompt[:100] + "...",
        "cost_usd": NANO_BANANA_PRICE_USD,
        "duration_ms": int((time.time() - t0) * 1000),
    }
