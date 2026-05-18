"""Régénère le HTML du rapport A/B (NB vs GPT) à partir du results.json existant.

UX améliorée :
- Layout 3 colonnes pleine largeur (Input | NB | GPT) au lieu d'un tableau étroit
- Images grandes par défaut (min 400px de hauteur)
- Click sur image → ouverture en lightbox plein écran
- Sticky header par photo pour orientation
- Filtres rapides (afficher uniquement les FAIL / NB win / GPT win / etc.)

USAGE :
    python scripts/regenerate_ab_html.py [slug]
    (par défaut : booking-gates-hotel-south-beach)
"""

import os
import sys
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLUG = sys.argv[1] if len(sys.argv) > 1 else "booking-gates-hotel-south-beach"
# Quality (medium par défaut) : si "high" → cherche dans ab_test_nb_vs_gpt_high/
QUALITY = sys.argv[2] if len(sys.argv) > 2 else "medium"
DIR_NAME = "ab_test_nb_vs_gpt" if QUALITY == "medium" else f"ab_test_nb_vs_gpt_{QUALITY}"

COMPARE_DIR = ROOT / "data" / "output" / SLUG / DIR_NAME
RESULTS_PATH = COMPARE_DIR / "results.json"
HTML_PATH = COMPARE_DIR / "index.html"

if not RESULTS_PATH.exists():
    print(f"❌ {RESULTS_PATH} introuvable. Lance d'abord compare_nb_vs_gpt.py")
    sys.exit(1)

with open(RESULTS_PATH) as f:
    data = json.load(f)

results = data.get("results", [])
print(f"[regen] {len(results)} entries from results.json")


def rel(p):
    """Path relatif depuis le HTML output dir."""
    if not p:
        return None
    try:
        return os.path.relpath(Path(p).resolve(), COMPARE_DIR)
    except ValueError:
        return str(p)


FIELD_LABELS = {
    "subject_count_added": "Sujets ajoutés",
    "pool_shape_preserved": "Forme piscine",
    "pool_surface_preserved": "Surface d'eau",
    "subject_water_boundary_respected": "Frontière sec/eau",
    "barrier_side_correct": "Côté barrière",
    "no_invented_support_under_subject": "Support existant",
    "pool_float_realistic": "Bouée taille/perspective",
}

STATUS_ICON = {"PASS": "✅", "FAIL": "🚨", "N/A": "—"}
STATUS_COLOR = {"PASS": "#059669", "FAIL": "#dc2626", "N/A": "#94a3b8"}


def render_field_check(fname, fdata):
    if not fdata:
        return ""
    status = (fdata.get("status") or "PASS").upper()
    label = FIELD_LABELS.get(fname, fname)
    evidence = fdata.get("evidence", "")
    extra = ""
    if fname == "subject_count_added":
        actual = fdata.get("actual", "?")
        extra = f" <span style='color:#64748b'>({actual})</span>"
    if fname == "pool_shape_preserved":
        delta = fdata.get("delta_estimate_pct", 0)
        if delta:
            extra = f" <span style='color:#64748b'>(Δ {delta}%)</span>"
    if fname == "pool_float_realistic":
        pct = fdata.get("size_pct_of_water", 0)
        if pct:
            extra = f" <span style='color:#64748b'>({pct}% surface)</span>"
    bg = '#fef2f2' if status == 'FAIL' else '#f8fafc'
    color = STATUS_COLOR.get(status, "#64748b")
    icon = STATUS_ICON.get(status, "—")
    return f"""<div style="display:flex; align-items:flex-start; gap:8px; padding:6px 10px; border-radius:6px; background:{bg};">
  <span style="font-size:16px; flex-shrink:0;">{icon}</span>
  <div style="flex:1; font-size:12px;">
    <div style="font-weight:600; color:{color}">{label}{extra}</div>
    {('<div style="color:#64748b; font-style:italic; margin-top:3px; font-size:11px;">' + evidence + '</div>') if evidence else ''}
  </div>
</div>"""


def render_violations(violations):
    if not violations:
        return '<div style="color:#059669; font-size:12px; font-weight:600; padding:4px 0;">✅ Aucune violation</div>'
    items = "".join(f"<li style='color:#dc2626; padding:2px 0;'>{v}</li>" for v in violations)
    return f'<ul style="margin:4px 0 0 0; padding-left:18px; font-size:12px;">{items}</ul>'


def render_photo_card(idx, r):
    """Rend une carte pleine largeur pour une photo."""
    if "skip_reason" in r:
        return f"""<div class="photo-card skipped">
            <div class="photo-header">
              <strong>⏭️ {r.get('filename', '?')}</strong> — skip : {r['skip_reason']}
            </div>
        </div>"""

    fn = r["filename"]
    prompt_chars = r.get("prompt_chars", 0)
    expected_n = r.get("expected_subject_count", "?")

    input_src = rel(r["input_path"])
    nb = r.get("nb", {})
    nb_src = rel(nb.get("output_path"))
    nb_dur = nb.get("result", {}).get("duration_ms", 0)
    nb_cost = nb.get("result", {}).get("cost_usd", 0)
    nb_err = nb.get("result", {}).get("error")
    nb_val = nb.get("validation") or {}
    nb_violations = nb_val.get("violations_derived", []) or []
    nb_fields = nb_val.get("field_checks", {}) or {}

    gpt = r.get("gpt", {})
    gpt_src = rel(gpt.get("output_path"))
    gpt_dur = gpt.get("result", {}).get("duration_ms", 0)
    gpt_cost = gpt.get("result", {}).get("cost_usd", 0)
    gpt_err = gpt.get("result", {}).get("error")
    gpt_val = gpt.get("validation") or {}
    gpt_violations = gpt_val.get("violations_derived", []) or []
    gpt_fields = gpt_val.get("field_checks", {}) or {}

    nb_pass = sum(1 for v in nb_fields.values() if (v.get("status") or "").upper() == "PASS")
    gpt_pass = sum(1 for v in gpt_fields.values() if (v.get("status") or "").upper() == "PASS")
    if nb_pass > gpt_pass:
        verdict_html = '<span class="badge-nb">🟢 NB meilleur</span>'
        data_winner = "nb"
    elif gpt_pass > nb_pass:
        verdict_html = '<span class="badge-gpt">🔵 GPT meilleur</span>'
        data_winner = "gpt"
    else:
        verdict_html = '<span class="badge-tie">= égalité</span>'
        data_winner = "tie"

    nb_img = (
        f'<img src="{nb_src}" class="comparison-img" onclick="openLightbox(this)" '
        f'alt="NB output for {fn}">' if nb_src
        else f'<div class="error-box">❌ {nb_err or "no output"}</div>'
    )
    gpt_img = (
        f'<img src="{gpt_src}" class="comparison-img" onclick="openLightbox(this)" '
        f'alt="GPT output for {fn}">' if gpt_src
        else f'<div class="error-box">❌ {gpt_err or "no output"}</div>'
    )

    return f"""<div class="photo-card" data-winner="{data_winner}" id="photo-{idx}">
      <div class="photo-header">
        <div>
          <span class="photo-idx">#{idx}</span>
          <strong>{fn}</strong>
        </div>
        <div class="photo-meta">
          <span>📝 {prompt_chars:,} char</span>
          <span>🎯 target N={expected_n}</span>
          <span>Score : NB {nb_pass}/6 · GPT {gpt_pass}/6</span>
          {verdict_html}
        </div>
      </div>

      <div class="image-grid">
        <div class="image-col">
          <div class="col-label" style="color:#64748b;">📥 Input source</div>
          <img src="{input_src}" class="comparison-img" onclick="openLightbox(this)" alt="Input {fn}">
        </div>
        <div class="image-col nb-col">
          <div class="col-label" style="color:#059669;">🟢 Nano Banana</div>
          {nb_img}
          <div class="col-meta">{nb_dur}ms · ${nb_cost:.4f}</div>
        </div>
        <div class="image-col gpt-col">
          <div class="col-label" style="color:#2563eb;">🔵 GPT Image ({QUALITY})</div>
          {gpt_img}
          <div class="col-meta">{gpt_dur}ms · ${gpt_cost:.4f}</div>
        </div>
      </div>

      <div class="details-grid">
        <div class="details-col nb-col">
          <div class="details-header">🟢 Validation Nano Banana</div>
          {render_violations(nb_violations)}
          <div class="fields-list">
            {''.join(render_field_check(k, v) for k, v in nb_fields.items())}
          </div>
        </div>
        <div class="details-col gpt-col">
          <div class="details-header">🔵 Validation GPT Image</div>
          {render_violations(gpt_violations)}
          <div class="fields-list">
            {''.join(render_field_check(k, v) for k, v in gpt_fields.items())}
          </div>
        </div>
      </div>
    </div>"""


# Stats
n_tested = sum(1 for r in results if "skip_reason" not in r)
n_nb_ok = sum(1 for r in results if r.get("nb", {}).get("result", {}).get("ok"))
n_gpt_ok = sum(1 for r in results if r.get("gpt", {}).get("result", {}).get("ok"))
nb_wins = gpt_wins = ties = 0
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


# Photo cards
photo_cards_html = "".join(render_photo_card(i + 1, r) for i, r in enumerate(results))


html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <title>A/B Test — Nano Banana vs GPT Image ({QUALITY}) · {SLUG}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font-family: 'Manrope', -apple-system, BlinkMacSystemFont, sans-serif;
      background: #f4f4f6;
      margin: 0;
      padding: 0;
      color: #292935;
      line-height: 1.5;
    }}

    /* Header sticky */
    .top-header {{
      position: sticky;
      top: 0;
      z-index: 50;
      background: rgba(255, 255, 255, 0.92);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border-bottom: 1px solid #eaeaeb;
      padding: 16px 24px;
    }}
    h1 {{
      font-size: 22px;
      font-weight: 800;
      margin: 0 0 4px;
      letter-spacing: -0.5px;
    }}
    .subtitle {{
      color: #64748b;
      font-size: 12px;
      margin: 0;
    }}

    /* Stats grid */
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      padding: 16px 24px;
      background: white;
      border-bottom: 1px solid #eaeaeb;
    }}
    .stat {{
      background: #f9f9f9;
      border-radius: 10px;
      padding: 12px 14px;
    }}
    .stat-label {{
      font-size: 10px;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-weight: 600;
    }}
    .stat-value {{
      font-size: 22px;
      font-weight: 800;
      color: #292935;
      margin-top: 2px;
      letter-spacing: -0.5px;
    }}
    .stat-sub {{
      font-size: 10px;
      color: #94a3b8;
      margin-top: 2px;
    }}

    /* Filter bar */
    .filter-bar {{
      display: flex;
      gap: 8px;
      padding: 12px 24px;
      background: white;
      border-bottom: 1px solid #eaeaeb;
      align-items: center;
      flex-wrap: wrap;
    }}
    .filter-label {{
      font-size: 11px;
      color: #64748b;
      font-weight: 600;
      text-transform: uppercase;
      margin-right: 4px;
    }}
    .filter-btn {{
      padding: 6px 14px;
      border: 1px solid #eaeaeb;
      border-radius: 100px;
      background: white;
      color: #54545d;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
    }}
    .filter-btn:hover {{
      border-color: #FFAF36;
      background: rgba(255, 175, 54, 0.06);
    }}
    .filter-btn.active {{
      background: linear-gradient(62deg, #FFAF36 0%, #FFC536 100%);
      color: #292935;
      border-color: transparent;
    }}

    /* Photo cards */
    .photos-container {{
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }}
    .photo-card {{
      background: white;
      border-radius: 16px;
      box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
      overflow: hidden;
      transition: all 0.3s ease;
    }}
    .photo-card.hidden {{ display: none; }}
    .photo-card.skipped {{ background: #fffbeb; }}

    .photo-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 20px;
      background: linear-gradient(to right, #f9f9f9, white);
      border-bottom: 1px solid #eaeaeb;
      flex-wrap: wrap;
      gap: 12px;
    }}
    .photo-idx {{
      display: inline-block;
      background: #FFAF36;
      color: #292935;
      font-size: 11px;
      font-weight: 700;
      padding: 3px 9px;
      border-radius: 100px;
      margin-right: 8px;
    }}
    .photo-meta {{
      display: flex;
      gap: 16px;
      font-size: 12px;
      color: #54545d;
      align-items: center;
      flex-wrap: wrap;
    }}

    .badge-nb, .badge-gpt, .badge-tie {{
      padding: 4px 10px;
      border-radius: 100px;
      font-size: 11px;
      font-weight: 700;
    }}
    .badge-nb {{ background: #d1fae5; color: #065f46; }}
    .badge-gpt {{ background: #dbeafe; color: #1e40af; }}
    .badge-tie {{ background: #f1f5f9; color: #64748b; }}

    /* Image grid : 3 colonnes pleine largeur */
    .image-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 12px;
      padding: 16px;
      background: #fafafa;
    }}
    .image-col {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .col-label {{
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .comparison-img {{
      width: 100%;
      height: auto;
      min-height: 300px;
      max-height: 600px;
      object-fit: contain;
      border-radius: 10px;
      background: #f1f5f9;
      cursor: zoom-in;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
      border: 2px solid transparent;
    }}
    .comparison-img:hover {{
      transform: scale(1.01);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
    }}
    .nb-col .comparison-img:hover {{ border-color: #86efac; }}
    .gpt-col .comparison-img:hover {{ border-color: #93c5fd; }}
    .col-meta {{
      font-size: 11px;
      color: #94a3b8;
      text-align: center;
    }}
    .error-box {{
      padding: 24px;
      background: #fef2f2;
      color: #dc2626;
      border-radius: 10px;
      font-size: 13px;
      min-height: 300px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}

    /* Details grid (validation) : 2 colonnes */
    .details-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      padding: 12px 16px 16px;
      background: white;
    }}
    .details-col {{
      padding: 12px;
      border-radius: 10px;
    }}
    .details-col.nb-col {{ background: #f0fdf4; }}
    .details-col.gpt-col {{ background: #eff6ff; }}
    .details-header {{
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 8px;
    }}
    .details-col.nb-col .details-header {{ color: #059669; }}
    .details-col.gpt-col .details-header {{ color: #2563eb; }}
    .fields-list {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-top: 8px;
    }}

    /* Lightbox */
    .lightbox {{
      display: none;
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.92);
      z-index: 100;
      cursor: zoom-out;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .lightbox.active {{ display: flex; }}
    .lightbox img {{
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
      box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
    }}
    .lightbox-close {{
      position: fixed;
      top: 16px;
      right: 24px;
      color: white;
      font-size: 28px;
      cursor: pointer;
      background: rgba(255, 255, 255, 0.1);
      width: 44px;
      height: 44px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      backdrop-filter: blur(8px);
    }}
    .lightbox-hint {{
      position: fixed;
      bottom: 24px;
      left: 50%;
      transform: translateX(-50%);
      color: white;
      font-size: 12px;
      background: rgba(0, 0, 0, 0.6);
      padding: 8px 16px;
      border-radius: 100px;
    }}

    /* Responsive */
    @media (max-width: 1024px) {{
      .image-grid {{
        grid-template-columns: 1fr;
      }}
      .details-grid {{
        grid-template-columns: 1fr;
      }}
      .comparison-img {{
        min-height: 250px;
      }}
    }}
  </style>
</head>
<body>
  <header class="top-header">
    <h1>🆚 A/B Test — Nano Banana vs GPT Image</h1>
    <p class="subtitle">Slug : <code>{SLUG}</code> · Photos testées : {n_tested} · Généré le {time.strftime('%d/%m/%Y %H:%M')} · Click sur image pour zoom</p>
  </header>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">Photos avec ai_add_character</div>
      <div class="stat-value">{n_tested}</div>
      <div class="stat-sub">/ {len(results)} scannées</div>
    </div>
    <div class="stat">
      <div class="stat-label">Verdict champs critiques</div>
      <div class="stat-value">
        <span style="color:#059669;">{nb_wins}</span> ·
        <span style="color:#64748b;">{ties}</span> ·
        <span style="color:#2563eb;">{gpt_wins}</span>
      </div>
      <div class="stat-sub">🟢 NB · = · 🔵 GPT</div>
    </div>
    <div class="stat">
      <div class="stat-label">Coût total Nano Banana</div>
      <div class="stat-value" style="color:#059669;">${total_nb_cost:.3f}</div>
      <div class="stat-sub">{n_nb_ok}/{n_tested} ok · {total_nb_dur:.1f}s</div>
    </div>
    <div class="stat">
      <div class="stat-label">Coût total GPT Image</div>
      <div class="stat-value" style="color:#2563eb;">${total_gpt_cost:.3f}</div>
      <div class="stat-sub">{n_gpt_ok}/{n_tested} ok · {total_gpt_dur:.1f}s</div>
    </div>
  </div>

  <div class="filter-bar">
    <span class="filter-label">Filtrer :</span>
    <button class="filter-btn active" data-filter="all">Toutes ({n_tested})</button>
    <button class="filter-btn" data-filter="nb">🟢 NB gagne ({nb_wins})</button>
    <button class="filter-btn" data-filter="tie">= Égalité ({ties})</button>
    <button class="filter-btn" data-filter="gpt">🔵 GPT gagne ({gpt_wins})</button>
  </div>

  <main class="photos-container">
    {photo_cards_html}
  </main>

  <!-- Lightbox -->
  <div class="lightbox" id="lightbox" onclick="closeLightbox(event)">
    <div class="lightbox-close" onclick="closeLightbox(event)">×</div>
    <img id="lightbox-img" alt="">
    <div class="lightbox-hint">Click ou ESC pour fermer · Touches ← / → pour naviguer</div>
  </div>

  <script>
    // Lightbox
    const lightbox = document.getElementById('lightbox');
    const lightboxImg = document.getElementById('lightbox-img');
    let currentImages = [];
    let currentIdx = 0;

    function openLightbox(imgEl) {{
      // Récupère TOUTES les images de la page pour navigation
      currentImages = Array.from(document.querySelectorAll('.comparison-img'));
      currentIdx = currentImages.indexOf(imgEl);
      lightboxImg.src = imgEl.src;
      lightboxImg.alt = imgEl.alt;
      lightbox.classList.add('active');
      document.body.style.overflow = 'hidden';
    }}

    function closeLightbox(e) {{
      // Ne ferme pas si on a cliqué sur l'image
      if (e && e.target.tagName === 'IMG') return;
      lightbox.classList.remove('active');
      document.body.style.overflow = '';
    }}

    document.addEventListener('keydown', (e) => {{
      if (!lightbox.classList.contains('active')) return;
      if (e.key === 'Escape') closeLightbox();
      if (e.key === 'ArrowRight' && currentIdx < currentImages.length - 1) {{
        currentIdx++;
        lightboxImg.src = currentImages[currentIdx].src;
      }}
      if (e.key === 'ArrowLeft' && currentIdx > 0) {{
        currentIdx--;
        lightboxImg.src = currentImages[currentIdx].src;
      }}
    }});

    // Filtres
    document.querySelectorAll('.filter-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const f = btn.dataset.filter;
        document.querySelectorAll('.photo-card').forEach(card => {{
          if (f === 'all') {{
            card.classList.remove('hidden');
          }} else {{
            card.classList.toggle('hidden', card.dataset.winner !== f);
          }}
        }});
      }});
    }});
  </script>
</body>
</html>
"""

with open(HTML_PATH, "w") as f:
    f.write(html)

print(f"[regen] ✅ HTML regénéré : {HTML_PATH}")
print(f"[regen] Ouvrir : open {HTML_PATH}")
