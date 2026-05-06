"""Construit le journal complet de chaque photo à travers le pipeline.

Pour la visualisation workflow : on liste tous les nœuds par lesquels chaque photo
est passée, avec result (pass / stop / skip / warn / retry / fallback) et raison.

Le frontend utilise ces données pour rejouer le pipeline visuellement et permettre à
l'utilisateur de comprendre POURQUOI chaque photo a été retenue ou écartée à un nœud.

Liste des nœuds (ordre du pipeline réel) :
  1. source                — photo arrive depuis RP / Booking / Officiel / upload
  2. user_select           — désélection manuelle dans la grille
  3. gemini_analyze        — N1 ingestion + N2 analyse Gemini
  4. dedup_phash_strict    — seuil 12, élimination doublons
  5. dedup_vlm             — zone grise pHash 13-28, check VLM Gemini
  6. amenity_verifier      — 2e passe Gemini sur top candidats par bucket
  7. coverage_select       — selected_in_top / rejected_low_score / unmapped
  8. bonus_lifestyle       — route lifestyle qualifié vers bucket bonus
  9. fully_generated_inject— photos générées full IA injectées (spa/bar manquants)
 10. ordering_slot         — assignation slot 1..N selon règles brand
 11. retouche              — steps appliqués (clutter / lighting / add_character / etc.)
 12. validation_postIA     — ok / retry / fallback original
 13. lut_brand             — LUT cohérence brand appliqué
 14. final_pack            — photo dans le pack final
"""

from __future__ import annotations
from pathlib import Path


def _detect_source(filename: str) -> str:
    """Détecte la source d'une photo via son préfixe."""
    if filename.startswith("rp_"):
        return "ResortPass"
    if filename.startswith("booking_"):
        return "Booking"
    if filename.startswith("official_"):
        return "Site officiel"
    if filename.startswith("instagram_"):
        return "Instagram"
    if filename.startswith("_generated_"):
        return "Génération IA"
    return "Upload manuel"


def build_photo_journey(
    slug: str,
    photo_paths_all: list[Path],
    deselected_set: set[str],
    analyses: list[dict],
    cov: dict,
    generated_photos: list[dict],
    ordered_pack: list[dict],
    enhanced_results: list[dict],
    vlm_dedup_results: list[dict] | None = None,
    verifier_results: list[dict] | None = None,
) -> list[dict]:
    """Consolide les transitions de chaque photo à travers tous les nœuds du pipeline.

    Returns une liste de dicts {filename, thumb_url, source, events[], final_status, stopped_at_node}.
    """
    journey: dict[str, dict] = {}

    # ━━ Phase 1 : init pour chaque photo en input ━━
    for p in photo_paths_all:
        source = _detect_source(p.name)
        journey[p.name] = {
            "filename": p.name,
            "thumb_url": f"/uploads/{slug}/{p.name}",
            "source": source,
            "is_generated": False,
            "events": [
                {"node": "source", "result": "pass", "details": {"source": source}},
            ],
            "final_status": "in_progress",
            "stopped_at_node": None,
        }

    # ━━ Phase 2 : user_select (désélection manuelle) ━━
    for fname in deselected_set:
        if fname in journey:
            journey[fname]["events"].append({
                "node": "user_select",
                "result": "stop",
                "reason": "désélectionnée par l'utilisateur dans la grille",
            })
            journey[fname]["final_status"] = "user_deselected"
            journey[fname]["stopped_at_node"] = "user_select"

    # ━━ Phase 3 : gemini_analyze (N1 ingestion + N2 Gemini) ━━
    for a in analyses or []:
        fname = (a.get("input") or {}).get("filename")
        if not fname or fname not in journey or journey[fname]["final_status"] != "in_progress":
            continue
        trace = a.get("trace") or []
        n1 = next((t for t in trace if t.get("node") == "N1_ingestion"), None)
        n2 = next((t for t in trace if t.get("node") == "N2_analyze_gemini"), None)

        if n1 and n1.get("result") == "reject":
            journey[fname]["events"].append({
                "node": "gemini_analyze",
                "result": "stop",
                "reason": f"ingestion : {n1.get('error', 'résolution trop faible ou fichier corrompu')}",
            })
            journey[fname]["final_status"] = "ingestion_failed"
            journey[fname]["stopped_at_node"] = "gemini_analyze"
            continue

        if not n2 or n2.get("result") != "pass":
            err = (n2 or {}).get("error", "Gemini n'a pas démarré")
            journey[fname]["events"].append({
                "node": "gemini_analyze",
                "result": "stop",
                "reason": f"analyse Gemini échouée : {(err or '')[:140]}",
            })
            journey[fname]["final_status"] = "gemini_failed"
            journey[fname]["stopped_at_node"] = "gemini_analyze"
            continue

        an = a.get("analysis") or {}
        f = an.get("factual") or {}
        ps = (an.get("emotional") or {}).get("pillar_scores") or {}
        journey[fname]["events"].append({
            "node": "gemini_analyze",
            "result": "pass",
            "details": {
                "category": f.get("category"),
                "human_count": f.get("human_count"),
                "human_presence": f.get("human_presence_type"),
                "pillar": int(ps.get("freedom", 0) + ps.get("wellness", 0) + ps.get("experience", 0)),
                "shot_type": (an.get("shot_type") or {}).get("type"),
                "amenity_dominance": (an.get("amenity_dominance") or {}).get("primary_amenity_visible_pct"),
                "hero_score": (an.get("hero_quality") or {}).get("score"),
                "time_of_day": f.get("time_of_day"),
            },
        })

    # ━━ Phase 4 : dedup_phash_strict (seuil 12) ━━
    for a in analyses or []:
        fname = (a.get("input") or {}).get("filename")
        if not fname or fname not in journey or journey[fname]["final_status"] != "in_progress":
            continue
        if a.get("dedup_status") == "duplicate_dropped":
            journey[fname]["events"].append({
                "node": "dedup_phash_strict",
                "result": "stop",
                "reason": "doublon pHash (cluster) — la meilleure variante du cluster a été gardée",
            })
            journey[fname]["final_status"] = "duplicate_phash"
            journey[fname]["stopped_at_node"] = "dedup_phash_strict"
            continue
        journey[fname]["events"].append({"node": "dedup_phash_strict", "result": "pass"})

    # ━━ Phase 5 : dedup_vlm (info — la pénalisation s'est faite via dedup_status) ━━
    vlm_checked_filenames: set[str] = set()
    for r in (vlm_dedup_results or []):
        for k in ("a", "b"):
            fn = r.get(k)
            if fn:
                vlm_checked_filenames.add(fn)
    for fname, j in journey.items():
        if j["final_status"] != "in_progress":
            continue
        if fname in vlm_checked_filenames:
            j["events"].append({"node": "dedup_vlm", "result": "pass", "details": {"checked": True}})
        else:
            j["events"].append({"node": "dedup_vlm", "result": "skip"})

    # ━━ Phase 6 : amenity_verifier (corrige dominance via 2e passe Gemini) ━━
    verifier_by_filename = {r.get("filename"): r for r in (verifier_results or []) if r.get("filename")}
    for fname, j in journey.items():
        if j["final_status"] != "in_progress":
            continue
        v = verifier_by_filename.get(fname)
        if v:
            j["events"].append({
                "node": "amenity_verifier",
                "result": "pass" if v.get("is_focused") else "downgraded",
                "details": {
                    "is_focused": v.get("is_focused"),
                    "real_dominance_pct": v.get("real_dominance_pct"),
                    "claimed_category": v.get("claimed_category"),
                    "reason": v.get("reason", "")[:200] if v.get("reason") else None,
                },
            })
        else:
            j["events"].append({"node": "amenity_verifier", "result": "skip"})

    # ━━ Phase 7 : coverage_select (selected / rejected_low_score / unmapped) ━━
    rejected_by_filename = {r["filename"]: r for r in (cov.get("rejected_low_score") or [])}
    # Construit map filename → list of (cat, rank, selected, score)
    bucket_info_by_filename: dict[str, list[dict]] = {}
    for cat, info in (cov.get("by_category") or {}).items():
        for idx, ph in enumerate(info.get("photos") or []):
            bucket_info_by_filename.setdefault(ph["filename"], []).append({
                "category": cat,
                "rank": idx + 1,
                "selected_in_top": ph.get("selected_in_top"),
                "score": ph.get("score_brand_total"),
            })
    bonus_filenames = {b["filename"] for b in (cov.get("bonus_lifestyle") or [])}

    for fname, j in journey.items():
        if j["final_status"] != "in_progress":
            continue
        # Photos rejetées explicitement (low_score / disqualifying issues / non-amenity pillar)
        if fname in rejected_by_filename:
            r = rejected_by_filename[fname]
            j["events"].append({
                "node": "coverage_select",
                "result": "stop",
                "reason": r.get("reason", "rejetée au scoring"),
                "details": {"score": r.get("score"), "category": r.get("category")},
            })
            j["final_status"] = "coverage_rejected"
            j["stopped_at_node"] = "coverage_select"
            continue
        bucket_info = bucket_info_by_filename.get(fname, [])
        if not bucket_info and fname not in bonus_filenames:
            j["events"].append({
                "node": "coverage_select",
                "result": "stop",
                "reason": "non mappée à un bucket amenity (catégorie hors shopping list — chambre, staff, ou catégorie absente RP)",
            })
            j["final_status"] = "unmapped"
            j["stopped_at_node"] = "coverage_select"
            continue
        any_selected = any(b["selected_in_top"] for b in bucket_info)
        if any_selected:
            j["events"].append({
                "node": "coverage_select",
                "result": "pass",
                "details": {"buckets": bucket_info},
            })
        elif bucket_info:
            best = bucket_info[0]
            j["events"].append({
                "node": "coverage_select",
                "result": "stop",
                "reason": f"dans bucket {best['category']} mais hors top-N (rang {best['rank']})",
                "details": {"buckets": bucket_info},
            })
            j["final_status"] = "out_of_top_N"
            j["stopped_at_node"] = "coverage_select"

    # ━━ Phase 8 : bonus_lifestyle ━━
    for fname in bonus_filenames:
        if fname in journey and journey[fname]["final_status"] == "in_progress":
            journey[fname]["events"].append({
                "node": "bonus_lifestyle",
                "result": "pass",
                "details": {"is_bonus": True},
            })

    # ━━ Phase 9 : fully_generated_inject (photos générées full IA injectées) ━━
    for gp in (generated_photos or []):
        gen_fname = gp.get("filename")
        if not gen_fname:
            continue
        if gen_fname not in journey:
            journey[gen_fname] = {
                "filename": gen_fname,
                "thumb_url": f"/uploads/{slug}/{gen_fname}",
                "source": "Génération IA",
                "is_generated": True,
                "events": [
                    {"node": "fully_generated_inject", "result": "pass",
                     "details": {"category": gp.get("category"), "cost_usd": gp.get("cost_usd")}},
                ],
                "final_status": "in_progress",
                "stopped_at_node": None,
            }

    # ━━ Phase 10 : ordering_slot (assignation slot 1..N) ━━
    final_order_by_fname = {
        e["input"]["filename"]: idx + 1
        for idx, e in enumerate(ordered_pack or [])
    }
    ordered_filenames_set = set(final_order_by_fname.keys())
    for fname, j in journey.items():
        if j["final_status"] != "in_progress":
            continue
        if fname not in ordered_filenames_set:
            j["events"].append({
                "node": "ordering_slot",
                "result": "stop",
                "reason": "passée par coverage mais saturation des buckets / règles brand (target_count_max atteint)",
            })
            j["final_status"] = "not_in_final_pack"
            j["stopped_at_node"] = "ordering_slot"
            continue
        j["events"].append({
            "node": "ordering_slot",
            "result": "pass",
            "details": {"slot": final_order_by_fname[fname]},
        })

    # ━━ Phase 11 : retouche + validation + lut + final ━━
    enhanced_by_fname = {r.get("filename"): r for r in (enhanced_results or [])}
    for fname, j in journey.items():
        if j["final_status"] != "in_progress":
            continue
        r = enhanced_by_fname.get(fname)
        if not r:
            j["events"].append({"node": "retouche", "result": "skip", "reason": "pas de retouche enregistrée"})
            j["final_status"] = "in_pack_no_enhance"
            j["stopped_at_node"] = None
            continue

        steps_actions = [s.get("action") for s in (r.get("steps") or [])]
        j["events"].append({
            "node": "retouche",
            "result": "pass",
            "details": {
                "actions": steps_actions,
                "persona_used": r.get("persona_used"),
                "duration_ms": r.get("duration_ms"),
                "cost_usd": r.get("cost_usd"),
            },
        })

        ai_validation = r.get("ai_validation") or {}
        if r.get("retry_attempted"):
            j["events"].append({
                "node": "validation_postIA",
                "result": "retry",
                "details": {"violations_before_retry": ai_validation.get("violations_before_retry", [])},
            })
        if r.get("fallback_to_original"):
            j["events"].append({
                "node": "validation_postIA",
                "result": "fallback_original",
                "reason": "Validation post-IA échouée 2× → fallback original",
            })
        elif "ok" in ai_validation:
            j["events"].append({
                "node": "validation_postIA",
                "result": "pass" if ai_validation.get("ok") else "warn",
                "details": {"violations": ai_validation.get("violations", [])},
            })

        if r.get("brand_lut_applied"):
            j["events"].append({"node": "lut_brand", "result": "pass"})

        j["events"].append({
            "node": "final_pack",
            "result": "pass",
            "details": {"slot": final_order_by_fname.get(fname)},
        })
        j["final_status"] = "in_final_pack"

    return list(journey.values())


# Liste ordonnée des nœuds avec labels FR + description en mots simples (pour la viz UI)
NODE_DEFINITIONS = [
    {
        "id": "source", "label": "Sources", "icon": "📥",
        "description": "Photos téléchargées depuis ResortPass, Booking et le site officiel. "
                       "Chaque source enrichit le pool. Une dédup pHash inter-sources élimine immédiatement les doublons "
                       "qui se chevauchent entre 2 sources.",
        "test": "Photo correctement téléchargée depuis au moins une des sources cochées ?",
    },
    {
        "id": "user_select", "label": "Sélection utilisateur", "icon": "👆",
        "description": "Avant de lancer l'analyse, tu peux décocher manuellement des photos dans la grille (par ex. photos floues "
                       "que tu vois à l'œil nu). Ces photos désélectionnées sortent du pipeline immédiatement.",
        "test": "Photo cochée dans la grille de pré-sélection ?",
    },
    {
        "id": "gemini_analyze", "label": "Analyse Gemini Vision", "icon": "🔍",
        "description": "Gemini regarde chaque photo et la décrit en JSON structuré : catégorie (piscine, cabana, rooftop, beach…), "
                       "nombre d'humains visibles, ambiance, palette, score brand sur 300 (freedom + wellness + experience), "
                       "qualité hero (WOW factor 0-100), type de plan (close_up / medium / wide / aerial), "
                       "dominance de l'amenity dans le cadre, safe zones pour ajout d'humain. "
                       "Échoue si l'image est trop petite (<500x320) ou corrompue.",
        "test": "Image chargeable + Gemini Vision retourne un JSON exploitable ?",
    },
    {
        "id": "dedup_phash_strict", "label": "Dédup pHash strict", "icon": "🔁",
        "description": "Empreinte visuelle (pHash) calculée sur chaque photo. Distance Hamming ≤ 12 = doublon quasi-identique. "
                       "On garde la version la mieux notée du cluster (best score brand) et on retire les autres.",
        "test": "Photo unique (pas un doublon pHash d'une autre photo du pack) ?",
    },
    {
        "id": "dedup_vlm", "label": "Dédup VLM Gemini", "icon": "🧪",
        "description": "Pour les paires en zone grise (distance pHash 13-28), Gemini compare 2 à 2 et décide si c'est la "
                       "« même scène » (à filtrer) ou « scènes différentes » (à garder). Filtre auto pour les photos jour/nuit "
                       "du même lieu = scènes différentes.",
        "test": "Photo distincte des autres au sens VLM (pas la même scène) ?",
    },
    {
        "id": "amenity_verifier", "label": "Verifier amenity (2e passe)", "icon": "🛡",
        "description": "Pour les top candidates de chaque bucket amenity (pool, cabana, etc.), on redemande à Gemini : "
                       "« cette piscine est-elle VRAIMENT le sujet principal ? ». Si non (ex: c'est en fait un humain "
                       "en gros plan avec piscine en fond) → la dominance est rabaissée, la photo est rétrogradée dans le tri.",
        "test": "Amenity confirmée comme sujet principal par 2e passe Gemini ?",
    },
    {
        "id": "coverage_select", "label": "Coverage & sélection top-N", "icon": "🎯",
        "description": "On range les photos dans les buckets amenity correspondant aux services RP "
                       "(Pool 3-4 photos, Cabana 2-4, Rooftop 1-3, Spa 1-2, Beach 1-3, Food/Bar/Gym max 1 chacun). "
                       "Score = pillar + dominance + bonus hero/transformable − pénalité lifestyle. "
                       "Photos avec score < 80 et non transformables sont rejetées. Chambre/staff exclus dès le départ.",
        "test": "Score ≥ 80 ET dans le top-N de son bucket ET catégorie compatible Day Pass ?",
    },
    {
        "id": "bonus_lifestyle", "label": "Bonus lifestyle", "icon": "✨",
        "description": "Photos focus humain (close-up bikini avec piscine en fond, couple à table…) où on voit bien le visage "
                       "(face_visibility = complete) sont routées vers un bucket bonus séparé. Max 2 par hôtel, "
                       "injectées en QUEUE de pack. Ces photos ne comptent PAS dans les buckets amenity (pas de doublon). "
                       "Photos décapitées / focus body sans visage rejetées.",
        "test": "Photo lifestyle avec visage visible et ≤ 2 humains ? Si oui, candidate bonus.",
    },
    {
        "id": "fully_generated_inject", "label": "Génération full IA", "icon": "🎨",
        "description": "Si l'hôtel a un spa OU un bar qui n'a aucune photo dans les sources → on génère une photo "
                       "100% IA générique (table de massage pour spa, cocktail générique pour bar) avec un prompt brand. "
                       "Photo marquée explicitement « 100% IA » pour transparence. Pas de génération sur les amenities "
                       "centrales (pool/cabana/rooftop/beach/food).",
        "test": "Bucket amenity vide ET catégorie autorisée à la génération ? Si oui, photo IA injectée.",
    },
    {
        "id": "ordering_slot", "label": "Ordering pack final", "icon": "📐",
        "description": "On ordonne les photos sélectionnées selon les règles brand : slot 1 = photo de couverture WOW "
                       "(tier 1 obligatoire : pool/cabana/rooftop/beach), alternance humain stricte 1 sur 2 jamais 2 sans "
                       "humain à la suite, round-robin entre amenities (pas 3 piscines à la suite), bar/food/gym en queue de pack.",
        "test": "Photo retenue dans les 12-18 slots finaux (cible 15) selon règles brand ?",
    },
    {
        "id": "retouche", "label": "Retouches Nano Banana 2", "icon": "🖌",
        "description": "Pour chaque photo du pack final, on applique 1 ou 2 transformations IA chaînées : "
                       "recadrage local Pillow (gratuit), ajout personnage IA (solos/couples/small_groups en rotation), "
                       "transformation nuit → jour ensoleillé, retrait de clutter (caméras, logos tiers, escaliers de secours), "
                       "ou simple warm boost local. Coût : ~$0.067 par appel Nano Banana.",
        "test": "Stratégie de retouche choisie selon catégorie + analyse Gemini ?",
    },
    {
        "id": "validation_postIA", "label": "Validation post-IA", "icon": "🔬",
        "description": "Gemini Vision compare l'avant/après de chaque retouche IA. Détecte les violations : mobilier inventé "
                       "(ex: lounger qui n'existait pas), personne sur l'eau impossible, scène quasi-régénérée, architecture "
                       "modifiée. Si violation actionnable → retry avec prompt durci ciblé. Si encore violation → fallback original "
                       "(mieux qu'une IA pétée en publication).",
        "test": "Output IA conforme (pas de mobilier inventé, pas de structure altérée) ?",
    },
    {
        "id": "lut_brand", "label": "LUT brand Dayuse", "icon": "🌅",
        "description": "Toutes les photos finales passent par la LUT brand Dayuse (post-traitement Pillow déterministe). "
                       "Garantit une cohérence visuelle inter-photos et inter-hôtels : mêmes tons chauds, même saturation, "
                       "même contraste. C'est ce qui donne l'identité Dayuse à la fiche.",
        "test": "Photo passée par la LUT brand pour homogénéité ?",
    },
    {
        "id": "final_pack", "label": "Pack final", "icon": "📦",
        "description": "Le pack ZIP de 12-18 photos (cible 15), ordonnées slot 1 → N selon les règles brand. "
                       "Téléchargeable directement, prêt pour publication sur la fiche Day Pass de l'hôtel.",
        "test": "Photo dans le ZIP final livrable ?",
    },
]
