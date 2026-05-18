"""A/B test — Nano Banana (Gemini Image) vs GPT Image (gpt-image-1) sur le dernier pipe.

OBJECTIF : pour chaque photo SÉLECTIONNÉE par l'outil qui a eu un ai_add_character,
on lance le même prompt sur les 2 modèles et on génère un HTML comparatif lisible
avec validation structurée par champ critique.

USAGE :
    python scripts/compare_nb_vs_gpt.py [slug]
    (par défaut : booking-gates-hotel-south-beach)

OUTPUT :
    data/output/<slug>/ab_test_nb_vs_gpt/
        gpt_<filename>.jpg     ← outputs GPT Image
        nb_<filename>.jpg      ← outputs Nano Banana (fresh run, prompt identique)
        index.html             ← rapport comparatif
        results.json           ← raw data

COÛT ESTIMÉ :
    Pour 13 photos : ~$1.50 NB + ~$1.80 GPT (medium quality) + ~$0.05 validation = ~$3.35
"""

import os
import sys
import json
import base64
import time
import traceback
from pathlib import Path

# Add parent dir to path so we can import enhance, ai_validator, etc.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from openai import OpenAI
from enhance import pick_strategy, enhance_ai
import ai_validator

# ━━━ Config ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLUG = sys.argv[1] if len(sys.argv) > 1 else "booking-gates-hotel-south-beach"
# Quality GPT Image : "low" / "medium" / "high" — passé en arg 2 ou via env GPT_QUALITY.
GPT_QUALITY = sys.argv[2] if len(sys.argv) > 2 else os.getenv("GPT_QUALITY", "medium")
if GPT_QUALITY not in ("low", "medium", "high"):
    print(f"❌ quality '{GPT_QUALITY}' invalide. Utiliser low/medium/high.")
    sys.exit(1)

# Coût estimé GPT Image gpt-image-1 selon quality (1024×1024)
GPT_COST_PER_IMAGE = {"low": 0.011, "medium": 0.04, "high": 0.167}[GPT_QUALITY]

INPUT_DIR = ROOT / "data" / "uploads" / SLUG
ANALYSES_DIR = ROOT / "data" / "analyses" / SLUG
ENHANCED_DIR = ROOT / "data" / "output" / SLUG / "enhanced"
# Dossier dédié par quality pour ne pas écraser les runs précédents
COMPARE_DIR_NAME = "ab_test_nb_vs_gpt" if GPT_QUALITY == "medium" else f"ab_test_nb_vs_gpt_{GPT_QUALITY}"
COMPARE_DIR = ROOT / "data" / "output" / SLUG / COMPARE_DIR_NAME
COMPARE_DIR.mkdir(parents=True, exist_ok=True)

print(f"[A/B] Slug         : {SLUG}")
print(f"[A/B] GPT Quality  : {GPT_QUALITY} (~${GPT_COST_PER_IMAGE}/image)")
print(f"[A/B] Output dir   : {COMPARE_DIR}")

# ━━━ Clients ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
openai_client = OpenAI()


def run_gpt_image(input_path: Path, prompt: str, quality: str = "medium") -> tuple[bytes, int]:
    """Edit input image with GPT Image (gpt-image-1).

    Returns: (image_bytes, duration_ms)
    """
    t0 = time.time()
    # GPT Image accepte des prompts jusqu'à ~32000 chars
    truncated = prompt[:30000] if len(prompt) > 30000 else prompt
    with open(input_path, "rb") as f:
        result = openai_client.images.edit(
            model="gpt-image-1",
            image=f,
            prompt=truncated,
            size="1024x1024",
            quality=quality,
        )
    duration_ms = int((time.time() - t0) * 1000)
    b64 = result.data[0].b64_json
    img_bytes = base64.b64decode(b64)
    return img_bytes, duration_ms


def run_nb(input_path: Path, output_path: Path, prompt: str) -> dict:
    """Run Nano Banana via existing enhance_ai helper."""
    return enhance_ai(input_path, output_path, prompt)


# ━━━ Collecte les photos sélectionnées avec strategy ai_add_character ━━━
print(f"[A/B] Scanning enhanced dir...")

entries_to_test = []
for jpg in sorted(ENHANCED_DIR.iterdir()):
    if jpg.is_dir():
        continue
    if not jpg.suffix.lower() in (".jpg", ".jpeg", ".png"):
        continue
    if jpg.name.startswith("_"):
        continue

    # Trouve l'input source
    input_path = INPUT_DIR / jpg.name
    if not input_path.exists():
        # Tente variation .jpg/.png
        stem = jpg.stem
        for ext in (".jpg", ".jpeg", ".png"):
            alt = INPUT_DIR / f"{stem}{ext}"
            if alt.exists():
                input_path = alt
                break
        if not input_path.exists():
            print(f"  ⏭️  {jpg.name} : input source absent")
            continue

    # Trouve l'analyse Gemini
    analysis_path = ANALYSES_DIR / f"{jpg.stem}.json"
    if not analysis_path.exists():
        print(f"  ⏭️  {jpg.name} : analyse Gemini absente")
        continue

    try:
        with open(analysis_path) as f:
            analysis_full = json.load(f)
    except Exception as e:
        print(f"  ⏭️  {jpg.name} : analyse corrompue ({e})")
        continue

    # Les analyses sont wrappées : {"input": ..., "trace": ..., "analysis": {...}}
    # pick_strategy attend l'objet "analysis" directement (avec factual, etc.)
    analysis = analysis_full.get("analysis") if isinstance(analysis_full, dict) and "analysis" in analysis_full else analysis_full

    entries_to_test.append({
        "filename": jpg.name,
        "input_path": input_path,
        "analysis": analysis,
        "nb_cached_output": jpg,  # Référence visuelle de ce qui était en cache
    })

print(f"[A/B] {len(entries_to_test)} photos retouchées scannées.")

# ━━━ Pour chaque entry : reconstruct strategy + run NB + run GPT Image ━━━
results = []
for idx, entry in enumerate(entries_to_test, 1):
    fn = entry["filename"]
    print(f"\n[A/B] [{idx}/{len(entries_to_test)}] {fn}")

    # Reconstruct strategy via pick_strategy.
    # ⚠️ FORCE add_character=True + personas_allowed=["couples"] pour s'assurer qu'on génère
    # bien un prompt ai_add_character à comparer entre NB et GPT Image. Sans ces flags,
    # pick_strategy fait du local_warm_boost et on n'a rien à tester.
    try:
        strategy = pick_strategy(
            analysis=entry["analysis"],
            image_path=entry["input_path"],
            photo_filename=fn,
            add_character=True,
            personas_allowed=["couples"],
        )
    except Exception as e:
        print(f"  ❌ pick_strategy crash : {e}")
        results.append({**entry, "skip_reason": f"pick_strategy_error: {str(e)[:200]}"})
        continue

    # Cherche le step ai_add_character
    steps = strategy.get("steps") or [strategy]
    add_char_step = None
    for s in steps:
        if s.get("action") == "ai_add_character":
            add_char_step = s
            break

    if not add_char_step:
        action = strategy.get("action") or "?"
        print(f"  ⏭️  pas de ai_add_character (action={action})")
        results.append({
            "filename": fn,
            "input_path": str(entry["input_path"]),
            "skip_reason": f"no_ai_add_character (action={action})",
        })
        continue

    prompt = add_char_step.get("prompt") or ""
    if not prompt:
        print(f"  ⏭️  prompt vide")
        results.append({**entry, "skip_reason": "empty_prompt"})
        continue

    # Récup expected_n du prompt (regex EXACTLY N HUMAN)
    import re
    m = re.search(r"EXACTLY\s+(\d+)\s+HUMAN", prompt)
    expected_n = int(m.group(1)) if m else None

    print(f"  Prompt: {len(prompt)} chars, expected_n={expected_n}")

    # ━ Run NB (fresh, mêmes conditions que GPT Image) ━
    nb_output_path = COMPARE_DIR / f"nb_{fn}"
    nb_result = {"ok": False, "error": None, "duration_ms": 0, "cost_usd": 0}
    try:
        print(f"  🟢 NB run...", flush=True)
        nb_run = run_nb(entry["input_path"], nb_output_path, prompt)
        nb_result["ok"] = True
        nb_result["duration_ms"] = nb_run.get("duration_ms", 0)
        nb_result["cost_usd"] = nb_run.get("cost_usd", 0)
        print(f"     ok ({nb_result['duration_ms']}ms, ${nb_result['cost_usd']:.4f})")
    except Exception as e:
        nb_result["error"] = str(e)[:200]
        print(f"  ❌ NB failed: {e}")

    # ━ Run GPT Image ━
    gpt_output_path = COMPARE_DIR / f"gpt_{fn.replace('.jpg', '.png').replace('.jpeg', '.png')}"
    gpt_result = {"ok": False, "error": None, "duration_ms": 0, "cost_usd": 0}
    try:
        print(f"  🔵 GPT Image run (quality={GPT_QUALITY})...", flush=True)
        img_bytes, dur_ms = run_gpt_image(entry["input_path"], prompt, quality=GPT_QUALITY)
        with open(gpt_output_path, "wb") as f:
            f.write(img_bytes)
        gpt_result["ok"] = True
        gpt_result["duration_ms"] = dur_ms
        gpt_result["cost_usd"] = GPT_COST_PER_IMAGE
        gpt_result["quality"] = GPT_QUALITY
        print(f"     ok ({dur_ms}ms, ~${GPT_COST_PER_IMAGE})")
    except Exception as e:
        gpt_result["error"] = str(e)[:200]
        print(f"  ❌ GPT Image failed: {e}")

    # ━ Validation critical_fields sur les 2 outputs ━
    nb_validation = None
    gpt_validation = None
    if nb_result["ok"] and nb_output_path.exists():
        try:
            print(f"  🔬 Validation NB critical fields...", flush=True)
            nb_validation = ai_validator.validate_critical_fields(
                entry["input_path"], nb_output_path,
                expected_subject_count=expected_n,
            )
            print(f"     {len(nb_validation.get('violations_derived', []))} violations dérivées")
        except Exception as e:
            print(f"  ⚠️ NB validation crashed: {e}")
            nb_validation = {"error": str(e)[:200]}
    if gpt_result["ok"] and gpt_output_path.exists():
        try:
            print(f"  🔬 Validation GPT critical fields...", flush=True)
            gpt_validation = ai_validator.validate_critical_fields(
                entry["input_path"], gpt_output_path,
                expected_subject_count=expected_n,
            )
            print(f"     {len(gpt_validation.get('violations_derived', []))} violations dérivées")
        except Exception as e:
            print(f"  ⚠️ GPT validation crashed: {e}")
            gpt_validation = {"error": str(e)[:200]}

    results.append({
        "filename": fn,
        "input_path": str(entry["input_path"]),
        "prompt_chars": len(prompt),
        "expected_subject_count": expected_n,
        "scenario_writer_meta": strategy.get("scenario_writer"),
        "nb": {
            "output_path": str(nb_output_path) if nb_output_path.exists() else None,
            "result": nb_result,
            "validation": nb_validation,
        },
        "gpt": {
            "output_path": str(gpt_output_path) if gpt_output_path.exists() else None,
            "result": gpt_result,
            "validation": gpt_validation,
        },
    })

# ━━━ Sauve raw results ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
results_path = COMPARE_DIR / "results.json"
with open(results_path, "w") as f:
    json.dump({
        "slug": SLUG,
        "gpt_quality": GPT_QUALITY,
        "gpt_cost_per_image": GPT_COST_PER_IMAGE,
        "generated_at": time.time(),
        "n_photos_scanned": len(entries_to_test),
        "n_photos_tested": sum(1 for r in results if "skip_reason" not in r),
        "results": results,
    }, f, indent=2, ensure_ascii=False, default=str)
print(f"\n[A/B] Raw results saved : {results_path}")

# ━━━ Génère HTML report ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def render_field_check(field_name: str, fdata: dict) -> str:
    """HTML d'une ligne field_check."""
    if not fdata:
        return ""
    status = (fdata.get("status") or "PASS").upper()
    icon = {"PASS": "✅", "FAIL": "🚨", "N/A": "—"}.get(status, "—")
    color = {"PASS": "#059669", "FAIL": "#dc2626", "N/A": "#94a3b8"}.get(status, "#64748b")
    label = {
        "subject_count_added": "Sujets ajoutés",
        "pool_shape_preserved": "Forme piscine",
        "pool_surface_preserved": "Surface d'eau",
        "subject_water_boundary_respected": "Frontière sec/eau",
        "barrier_side_correct": "Côté barrière",
        "no_invented_support_under_subject": "Support existant",
    }.get(field_name, field_name)
    evidence = fdata.get("evidence", "")
    extra = ""
    if field_name == "subject_count_added":
        actual = fdata.get("actual", "?")
        extra = f" <span style='color:#64748b'>({actual})</span>"
    if field_name == "pool_shape_preserved":
        delta = fdata.get("delta_estimate_pct", 0)
        if delta:
            extra = f" <span style='color:#64748b'>(Δ {delta}%)</span>"
    return f"""<div style="display:flex; align-items:flex-start; gap:6px; padding:4px 6px; border-radius:4px; background:{'#fef2f2' if status == 'FAIL' else '#f8fafc'};">
  <span style="font-size:14px">{icon}</span>
  <div style="flex:1; font-size:11px;">
    <span style="font-weight:600; color:{color}">{label}{extra}</span>
    {('<div style="color:#64748b; font-style:italic; margin-top:2px;">' + evidence + '</div>') if evidence else ''}
  </div>
</div>"""


def render_violations(violations: list) -> str:
    if not violations:
        return '<div style="color:#059669; font-size:11px; font-weight:600;">✅ Aucune violation</div>'
    items = "".join(f"<li style='color:#dc2626;'>{v}</li>" for v in violations)
    return f'<ul style="margin:0; padding-left:14px; font-size:11px;">{items}</ul>'


def render_row(r: dict) -> str:
    if "skip_reason" in r:
        return f"""<tr><td colspan="5" style="padding:12px; background:#fef3c7; color:#92400e; font-size:12px; font-style:italic;">
            <strong>{r['filename']}</strong> — skip : {r['skip_reason']}
        </td></tr>"""

    fn = r["filename"]
    prompt_chars = r.get("prompt_chars", 0)
    expected_n = r.get("expected_subject_count", "?")

    # Input image
    input_rel = Path(r["input_path"]).resolve()
    try:
        input_src = os.path.relpath(input_rel, COMPARE_DIR)
    except ValueError:
        input_src = str(input_rel)

    # NB output
    nb = r.get("nb", {})
    nb_path = nb.get("output_path")
    nb_src = os.path.relpath(Path(nb_path).resolve(), COMPARE_DIR) if nb_path else None
    nb_dur = nb.get("result", {}).get("duration_ms", 0)
    nb_cost = nb.get("result", {}).get("cost_usd", 0)
    nb_err = nb.get("result", {}).get("error")
    nb_val = nb.get("validation") or {}
    nb_violations = nb_val.get("violations_derived", []) or []
    nb_fields = nb_val.get("field_checks", {}) or {}

    # GPT output
    gpt = r.get("gpt", {})
    gpt_path = gpt.get("output_path")
    gpt_src = os.path.relpath(Path(gpt_path).resolve(), COMPARE_DIR) if gpt_path else None
    gpt_dur = gpt.get("result", {}).get("duration_ms", 0)
    gpt_cost = gpt.get("result", {}).get("cost_usd", 0)
    gpt_err = gpt.get("result", {}).get("error")
    gpt_val = gpt.get("validation") or {}
    gpt_violations = gpt_val.get("violations_derived", []) or []
    gpt_fields = gpt_val.get("field_checks", {}) or {}

    # Verdict
    nb_score = len([v for v in nb_fields.values() if (v.get("status") or "").upper() == "PASS"])
    gpt_score = len([v for v in gpt_fields.values() if (v.get("status") or "").upper() == "PASS"])
    if nb_score > gpt_score:
        verdict = "<span style='color:#059669; font-weight:bold;'>🟢 NB meilleur</span>"
    elif gpt_score > nb_score:
        verdict = "<span style='color:#2563eb; font-weight:bold;'>🔵 GPT meilleur</span>"
    else:
        verdict = "<span style='color:#64748b;'>= égalité</span>"

    return f"""<tr style="border-top:1px solid #e2e8f0;">
    <td style="padding:14px 10px; vertical-align:top; min-width:240px;">
      <div style="font-size:13px; font-weight:bold; color:#1e293b; margin-bottom:6px;">{fn}</div>
      <div style="font-size:11px; color:#64748b; margin-bottom:8px;">
        Prompt : <strong>{prompt_chars:,} char</strong> · target N={expected_n}<br>
        {verdict}
      </div>
      <div style="font-size:11px; color:#64748b; margin-top:4px;">
        <strong>Verdict champs :</strong> NB {nb_score}/6 PASS · GPT {gpt_score}/6 PASS
      </div>
    </td>
    <td style="padding:8px; vertical-align:top; text-align:center;">
      <div style="font-size:10px; color:#64748b; margin-bottom:4px; text-transform:uppercase; font-weight:600;">Input</div>
      <img src="{input_src}" style="max-width:240px; max-height:160px; border-radius:6px; border:1px solid #e2e8f0;">
    </td>
    <td style="padding:8px; vertical-align:top; background:#f0fdf4;">
      <div style="font-size:10px; color:#059669; margin-bottom:4px; text-transform:uppercase; font-weight:600;">🟢 Nano Banana</div>
      {f'<img src="{nb_src}" style="max-width:240px; max-height:160px; border-radius:6px; border:1px solid #86efac;">' if nb_src else f'<div style="color:#dc2626; font-size:11px;">❌ {nb_err}</div>'}
      <div style="font-size:10px; color:#64748b; margin-top:4px;">{nb_dur}ms · ${nb_cost:.4f}</div>
      <div style="margin-top:8px;">{render_violations(nb_violations)}</div>
      <div style="margin-top:6px; display:flex; flex-direction:column; gap:2px;">
        {''.join(render_field_check(k, v) for k, v in nb_fields.items())}
      </div>
    </td>
    <td style="padding:8px; vertical-align:top; background:#eff6ff;">
      <div style="font-size:10px; color:#2563eb; margin-bottom:4px; text-transform:uppercase; font-weight:600;">🔵 GPT Image</div>
      {f'<img src="{gpt_src}" style="max-width:240px; max-height:160px; border-radius:6px; border:1px solid #93c5fd;">' if gpt_src else f'<div style="color:#dc2626; font-size:11px;">❌ {gpt_err}</div>'}
      <div style="font-size:10px; color:#64748b; margin-top:4px;">{gpt_dur}ms · ${gpt_cost:.4f}</div>
      <div style="margin-top:8px;">{render_violations(gpt_violations)}</div>
      <div style="margin-top:6px; display:flex; flex-direction:column; gap:2px;">
        {''.join(render_field_check(k, v) for k, v in gpt_fields.items())}
      </div>
    </td>
  </tr>"""


# Stats globales
n_tested = sum(1 for r in results if "skip_reason" not in r)
n_nb_ok = sum(1 for r in results if r.get("nb", {}).get("result", {}).get("ok"))
n_gpt_ok = sum(1 for r in results if r.get("gpt", {}).get("result", {}).get("ok"))

nb_wins = 0
gpt_wins = 0
ties = 0
for r in results:
    if "skip_reason" in r:
        continue
    nb_fields = (r.get("nb", {}).get("validation") or {}).get("field_checks") or {}
    gpt_fields = (r.get("gpt", {}).get("validation") or {}).get("field_checks") or {}
    nb_pass = sum(1 for v in nb_fields.values() if (v.get("status") or "").upper() == "PASS")
    gpt_pass = sum(1 for v in gpt_fields.values() if (v.get("status") or "").upper() == "PASS")
    if nb_pass > gpt_pass:
        nb_wins += 1
    elif gpt_pass > nb_pass:
        gpt_wins += 1
    else:
        ties += 1

total_nb_cost = sum((r.get("nb", {}).get("result", {}).get("cost_usd", 0) or 0) for r in results)
total_gpt_cost = sum((r.get("gpt", {}).get("result", {}).get("cost_usd", 0) or 0) for r in results)
total_nb_dur = sum((r.get("nb", {}).get("result", {}).get("duration_ms", 0) or 0) for r in results) / 1000
total_gpt_dur = sum((r.get("gpt", {}).get("result", {}).get("duration_ms", 0) or 0) for r in results) / 1000

html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <title>A/B Test — Nano Banana vs GPT Image ({GPT_QUALITY}) · {SLUG}</title>
  <style>
    body {{ font-family: 'Manrope', -apple-system, BlinkMacSystemFont, sans-serif; background:#f9f9f9; margin:0; padding:24px; color:#292935; }}
    h1 {{ font-size:24px; font-weight:800; margin:0 0 6px; }}
    .subtitle {{ color:#64748b; font-size:13px; margin-bottom:24px; }}
    .stats {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:24px; }}
    .stat {{ background:white; border-radius:12px; padding:14px 16px; box-shadow:0 2px 12px rgba(0,0,0,0.06); }}
    .stat-label {{ font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:0.5px; font-weight:600; }}
    .stat-value {{ font-size:24px; font-weight:800; color:#292935; margin-top:4px; }}
    .stat-sub {{ font-size:11px; color:#64748b; margin-top:2px; }}
    table {{ width:100%; background:white; border-collapse:collapse; border-radius:12px; overflow:hidden; box-shadow:0 2px 12px rgba(0,0,0,0.06); }}
    th {{ background:#f1f5f9; padding:10px; font-size:11px; text-align:left; text-transform:uppercase; color:#475569; font-weight:700; }}
    .verdict-nb {{ color:#059669; font-weight:bold; }}
    .verdict-gpt {{ color:#2563eb; font-weight:bold; }}
  </style>
</head>
<body>
  <h1>🆚 A/B Test — Nano Banana vs GPT Image</h1>
  <div class="subtitle">Slug : <code>{SLUG}</code> · Photos testées : {n_tested} / {len(results)} · Généré le {time.strftime('%d/%m/%Y %H:%M')}</div>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">Photos avec strategy add_character</div>
      <div class="stat-value">{n_tested}</div>
      <div class="stat-sub">/ {len(results)} scannées</div>
    </div>
    <div class="stat">
      <div class="stat-label">Verdict champs critiques</div>
      <div class="stat-value"><span class="verdict-nb">{nb_wins}</span> · <span style="color:#64748b">{ties}</span> · <span class="verdict-gpt">{gpt_wins}</span></div>
      <div class="stat-sub">🟢 NB gagne · = égalité · 🔵 GPT gagne</div>
    </div>
    <div class="stat">
      <div class="stat-label">Coût total NB</div>
      <div class="stat-value" style="color:#059669;">${total_nb_cost:.3f}</div>
      <div class="stat-sub">{n_nb_ok}/{n_tested} runs ok · {total_nb_dur:.1f}s cumul</div>
    </div>
    <div class="stat">
      <div class="stat-label">Coût total GPT Image</div>
      <div class="stat-value" style="color:#2563eb;">${total_gpt_cost:.3f}</div>
      <div class="stat-sub">{n_gpt_ok}/{n_tested} runs ok · {total_gpt_dur:.1f}s cumul</div>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Photo</th>
        <th>Input source</th>
        <th>🟢 Nano Banana</th>
        <th>🔵 GPT Image ({GPT_QUALITY})</th>
      </tr>
    </thead>
    <tbody>
      {''.join(render_row(r) for r in results)}
    </tbody>
  </table>

  <div style="margin-top:24px; font-size:11px; color:#64748b;">
    Validation par 6 champs critiques (validateur structuré Option B) : forme piscine,
    surface d'eau, frontière sec/eau, count sujets, côté barrière, support non-inventé.
    🚨 FAIL si un champ critique a dérivé.
  </div>
</body>
</html>
"""

html_path = COMPARE_DIR / "index.html"
with open(html_path, "w") as f:
    f.write(html)

print(f"\n[A/B] ✅ Rapport HTML généré : {html_path}")
print(f"[A/B] Ouvrir avec : open {html_path}")
print(f"\n[A/B] Stats finales :")
print(f"  Photos testées       : {n_tested}/{len(results)}")
print(f"  NB runs ok           : {n_nb_ok}/{n_tested}")
print(f"  GPT Image runs ok    : {n_gpt_ok}/{n_tested}")
print(f"  Verdict (champs)     : NB={nb_wins} | =={ties} | GPT={gpt_wins}")
print(f"  Coût total NB        : ${total_nb_cost:.3f}")
print(f"  Coût total GPT Image : ${total_gpt_cost:.3f}")
