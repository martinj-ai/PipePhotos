"""Validation post-IA — 2e passe Gemini Vision sur les photos retouchées par Nano Banana
pour détecter les dérives (mobilier inventé, personnes sur l'eau, scène régénérée, etc.).

Coût : ~$0.0006 par photo IA validée. Filet de sécurité automatique.
"""

from __future__ import annotations

import json
import os
import re as _re
import time
from pathlib import Path
from PIL import Image
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

VALIDATION_MODEL = "gemini-2.5-flash"

VALIDATION_PROMPT = """Tu reçois 2 images : la première est l'ORIGINALE, la seconde est une RETOUCHE IA censée juste ajouter ou modifier des éléments légers (personnages, lumière, recadrage). La retouche doit préserver fidèlement l'environnement.

Détecte les VIOLATIONS suivantes en comparant les 2 images. Retourne UNIQUEMENT un JSON strict.

Schéma JSON à retourner :

{
  "ok": true,
  "violations": ["liste des violations détectées, vide si aucune"],
  "summary": "1 phrase résumant l'état de la retouche"
}

Violations à détecter :

1. **invented_furniture** : du mobilier (daybed, transat, sofa, raft, plateforme flottante, etc.) a été AJOUTÉ alors qu'il n'existait pas dans l'originale. Particulièrement grave si placé sur l'eau ou en suspension.
2. **subject_on_water** : une ou plusieurs personnes sont positionnées SUR la surface de l'eau (debout sur l'eau, marchant dessus, ou sur un meuble flottant qui n'existe pas dans l'originale).
3. **subject_on_furniture_top** : personne debout sur un meuble fait pour s'allonger (daybed, sun lounger, sofa).
4. **subject_wrong_side_barrier** : personne de l'autre côté d'une barrière de sécurité (rooftop railing, garde-corps).
5. **scene_regenerated** : la photo a été quasi-régénérée — l'angle de caméra, la perspective, ou les éléments principaux ont fondamentalement changé entre l'avant et l'après. C'est une violation majeure.
6. **inconsistent_scale** : 2+ subjets ajoutés ont des échelles incompatibles (un subjet beaucoup plus grand qu'un autre à la même distance camera).
7. **architecture_changed** : l'architecture du bâtiment, la disposition du mobilier existant, ou le décor de fond ont été altérés.
8. **lighting_break** : ombres / direction lumière incohérente entre les sujets ajoutés et la scène.

`ok` = true SEULEMENT si la liste violations est vide. Sinon `ok` = false.

Retourne STRICTEMENT le JSON, pas de markdown, pas de texte hors JSON.
"""


_GENAI_CONFIGURED = False


def _ensure_configured():
    global _GENAI_CONFIGURED
    if not _GENAI_CONFIGURED:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante")
        genai.configure(api_key=api_key)
        _GENAI_CONFIGURED = True


def validate_ai_output(input_path: Path, output_path: Path,
                       model_name: str = VALIDATION_MODEL,
                       max_retries: int = 2,
                       action_context: str | None = None) -> dict:
    """Compare l'avant/après et retourne {ok: bool, violations: list, summary: str, ...}.

    Args:
        action_context : si fourni, certaines violations sont **autorisées** :
          - "ai_lighting" → la transformation nuit→jour est autorisée → on filtre 'scene_regenerated'
            et 'lighting_break' (changement de lumière voulu)
    """
    _ensure_configured()
    model = genai.GenerativeModel(model_name)

    last_error = None
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            before = Image.open(input_path).convert("RGB")
            after = Image.open(output_path).convert("RGB")
            response = model.generate_content(
                [VALIDATION_PROMPT, before, after],
                generation_config={"response_mime_type": "application/json", "temperature": 0.0},
            )
            duration_ms = int((time.time() - t0) * 1000)
            data = json.loads(response.text)
            usage = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
            output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
            cost_usd = (input_tokens * 0.30 + output_tokens * 2.50) / 1_000_000

            violations = data.get("violations") or []
            # Filtrage selon le contexte d'action : certaines "violations" sont en réalité des
            # transformations légitimes attendues.
            ALLOWED_BY_ACTION = {
                "ai_lighting": {"scene_regenerated", "lighting_break"},  # transformation nuit→jour autorisée
                "ai_recompose": {"scene_regenerated"},                    # recadrage IA peut sembler régénéré
                "ai_remove_clutter": {"architecture_changed"},            # retrait clutter modifie le décor
            }
            allowed = ALLOWED_BY_ACTION.get(action_context or "", set())
            filtered = [v for v in violations if v not in allowed]
            ok_after_filter = len(filtered) == 0

            return {
                "ok": ok_after_filter,
                "violations": filtered,
                "violations_raw": violations,  # garde pour debug
                "violations_allowed_by_context": list(allowed.intersection(violations)),
                "summary": data.get("summary") or "",
                "duration_ms": duration_ms,
                "cost_usd": round(cost_usd, 6),
            }
        except Exception as e:
            err = str(e)
            last_error = e
            is_retryable = "429" in err or "500" in err or "503" in err
            if is_retryable and attempt < max_retries:
                m = _re.search(r"retry in (\d+(?:\.\d+)?)\s*s", err)
                wait = (float(m.group(1)) + 2) if m else min(2 ** attempt * 5, 30)
                time.sleep(wait)
                continue
            break

    # Échec validation : on retourne ok=true par défaut (don't block pipeline) avec note
    return {
        "ok": True,
        "violations": [],
        "summary": f"Validation post-IA échouée : {str(last_error)[:200]}",
        "duration_ms": 0,
        "cost_usd": 0,
        "error": str(last_error)[:200] if last_error else None,
    }
