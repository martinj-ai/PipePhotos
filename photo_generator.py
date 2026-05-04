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
    "spa": """A premium hotel spa treatment room — UNAMBIGUOUSLY a spa, NOT a bedroom.

REQUIRED VISUAL ELEMENTS (must be clearly identifiable as spa) :
- A professional MASSAGE TABLE with a face cradle / face hole at one end (NOT a bed). Crisp white linens, folded white towels stacked at the foot.
- Spa-specific objects on a side table or shelf : rolled white towels, a small bowl with stones / orchid / rose petals, candles (lit), essential oil bottles, a lit incense stick, hot stones in a wooden bowl.
- Spa ambiance details : soft warm dim spa lighting (NOT bright daylight), wooden Asian-inspired or zen elements, lush green plants, dark wood floor or stone tiles, a small water feature OR a folded yoga mat in the background.
- Atmosphere : quiet, dim, intimate, NOT a hotel bedroom. NO bed with pillows. NO bedside lamp. NO dressing area.

🚫 FORBIDDEN visual elements (these would make it look like a bedroom — DO NOT include) :
- A real bed with bed linens / pillows / headboard
- Bedside table with reading lamp
- Wardrobe, mirror over a dressing table, closet door
- Window with city view (spa rooms typically have NO windows or only frosted/closed ones)
- TV screen, dresser, suitcase
- Hotel-room-style decor (bedsheets, throw pillows on bed, etc.)

Composition : massage table positioned diagonally or rule-of-thirds, soft warm low-key lighting (golden bias), shallow depth of field. The photo must read as "spa treatment room" within 1 second of looking at it.
Photorealistic editorial lifestyle photography. Kodak Vision3 500T look : warm highlights, neutral tones, subtle film grain, soft contrast. 35mm, f/4, ISO 100. Premium-accessible Dayuse mood — chaleureuse parenthèse wellness.

NEGATIVE PROMPT: bedroom, hotel bedroom, real bed, pillows on bed, headboard, bedside lamp, window with view, wardrobe, sterile clinical look, harsh fluorescent lighting, hospital, CGI, plastic shine, watermarks, logos, text, brand elements.""",

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
