# Anatomie des prompts d'édition d'image

> **Objectif** : disséquer les prompts envoyés à Nano Banana (Gemini Image) pour identifier ce qui est **probabiliste** (généré par un agent), **dérivé déterministe** (calculé en Python) ou **hardcoded statique** (texte fixe). Permet de cibler les leviers d'amélioration de la fiabilité du pipeline.

Dernière mise à jour : **14/05/2026** (Martin J)

---

## Légende

| Symbole | Type | Description |
|---|---|---|
| 🔵 | **Probabiliste** | Texte généré par Gemini Vision sur la photo réelle. Varie d'un run à l'autre (avec `temperature=0`, varie surtout d'une photo à l'autre). |
| 🟡 | **Dérivé déterministe** | Valeur calculée en Python à partir de l'output Gemini ou des paramètres d'entrée. Reproductible. |
| ⬜ | **Hardcoded statique** | Texte fixe écrit en dur dans le code Python. Identique pour TOUTES les photos. |

---

## Step 1 — Lumière (nuit → jour) — `PROMPT_ENSOLEILLEMENT`

| Bloc | Type | Tokens | Source |
|---|---|---|---|
| Intégralité du prompt | ⬜ | ~700 | Constante Python `PROMPT_ENSOLEILLEMENT` dans `enhance.py` |

**Conclusion Step 1** : 0% de variance, prompt identique pour chaque photo nuit→jour.

---

## Step 2 — Ajout personnage — `build_persona_prompt()`

Construit dans `_legacy_prompts.py::build_persona_prompt_v1_long` (V1 longue par défaut).

### Blocs hardcoded inconditionnels (cœur du prompt)

| Bloc | Type | Tokens | Source / Note |
|---|---|---|---|
| RULE #1 — Subject-only addition | ⬜ | ~200 | Hardcoded |
| Particular Attention — Floating Furniture | ⬜ | ~180 | Hardcoded (ajout 13/05/2026) |
| Particular Attention — Pool Deletion | ⬜ | ~200 | Hardcoded |
| RULE #2 — Framing Lock | ⬜ | ~80 | Hardcoded |
| Structural Preservation | ⬜ | ~280 | Hardcoded |
| Counting Double-Check | ⬜ | ~80 | Hardcoded |
| Scale Lock + Absolute Size Limit | ⬜ | ~280 | Hardcoded (renforcement 13/05/2026) |
| Physical Safety & Plausibility | ⬜ | ~80 | Hardcoded |
| Priority Rule Pool/Water | ⬜ | ~120 | Hardcoded |
| Water Depth Physics (bloc 🌊 long) | ⬜ | ~350 | Hardcoded |
| Face Quality | ⬜ | ~200 | Hardcoded |
| Lighting & Realism | ⬜ | ~50 | Hardcoded |
| Integration Rules | ⬜ | ~80 | Hardcoded |
| Photography Style (Reformation/Aman/Kodak Portra…) | ⬜ | ~150 | Hardcoded |
| Negative Prompt — [SCENE PRESERVATION] | ⬜ | ~250 | Hardcoded |
| Negative Prompt — [ATTIRE & STYLING] | ⬜ | ~200 | Hardcoded |
| Negative Prompt — [POSES & PHYSICS] | ⬜ | ~200 | Hardcoded |
| Negative Prompt — [ANATOMY & FACES] | ⬜ | ~150 | Hardcoded |
| Negative Prompt — [RENDERING] | ⬜ | ~80 | Hardcoded |

### Blocs hardcoded conditionnels (inclusion ou pas selon la photo)

| Bloc | Type | Tokens | Condition d'inclusion |
|---|---|---|---|
| RULE #0 — Safety Barrier Lock | ⬜ (texte) + 🟡 (inclusion) | ~250 | Inclus ssi `_detect_barrier_risk(unsafe_zones, category)` = True (regex Python sur output Gemini matchant les mots-clés `barrière / garde-corps / balustrade / railing / parapet`) |

### Blocs probabilistes (écrits par Gemini Vision)

| Bloc | Type | Tokens | Source |
|---|---|---|---|
| 🎬 **SCENARIO — description sujet + pose + outfit** | 🔵 | ~300-500 | **(V5 actif par défaut)** Écrit par Gemini Vision via `scenario_writer.py` à chaque photo. Si V5 échoue/skip → fallback ⬜ catalogue Python `_SCENARIO_CATALOG[(persona, zone_type)]` |
| 🎯 **SAFE ZONES** — `ZONE 1: …` `ZONE 2: …` `ZONE 3: …` | 🔵 | ~100-200 | Descriptions textuelles écrites par Gemini Vision (`analyze.py`). Champ `safe_zones_for_humans.safe_areas` |
| 🚫 **FORBIDDEN** — unsafe_areas | 🔵 | ~50-150 | Idem — descriptions textuelles écrites par Gemini Vision. Champ `safe_zones_for_humans.unsafe_areas` |

### Variables dérivées déterministes (1 token à ~30 chacune)

| Variable | Type | Source / Calcul |
|---|---|---|
| `{persona}` | 🟡 | Param d'entrée `personas_allowed[0]` ou `persona_override` |
| `{target_n}` | 🟡 | `compute_target_humans(persona, capacity)` + cap si barrière, + cap par `len(safe_zones) + bonus_multi_seat` |
| `{vibe_mood}` (1 phrase d'ambiance) | 🟡 | Lookup dans dict Python par `vibe` |
| `{pool_float_hint}` (bouée si tirage ~35%) | 🟡 | `pick_pool_float_hint()` — hash filename, déterministe |
| `{max_h}` (max humains scène) | 🟡 | Valeur int extraite de `safe_zones_for_humans.max_recommended` (output Gemini Vision) |
| Wrapper `📌 STRICT RULES on these zones` | ⬜ | Hardcoded ~100 tokens |
| Wrapper `🔢 QUANTITY HARD LOCK` (avec `{target_n}` injecté ×6) | ⬜ + 🟡 | Texte hardcoded ~200 tokens + injection variable |

---

## 📈 Bilan en tokens

| Type | Tokens approx | % du prompt |
|---|---|---|
| 🔵 **Probabiliste (Gemini écrit)** | ~500-800 tokens | **10-16 %** |
| 🟡 **Dérivé déterministe (Python calcule)** | ~50 tokens | **~1 %** |
| ⬜ **Hardcoded statique** | ~4200-4400 tokens | **83-88 %** |

Total : **~5000 tokens** par appel à Nano Banana pour `ai_add_character`.

---

## 🔍 Implications structurelles

### Les seules sources de variance créative dans le prompt

1. **Les `safe_zones` / `unsafe_zones`** — descriptions textuelles écrites par Gemini Vision (`analyze.py`). Avec `temperature=0`, très reproductibles run-to-run sur la même photo.

2. **Le `scenario_block`** — depuis V5 (par défaut), écrit dynamiquement par Gemini Vision (`scenario_writer.py`). C'est LA partie qui varie le plus selon la photo et qui devrait éliminer les bugs "yoga sur tapis de course".

3. **Les variables dérivées** (`target_n`, `persona`, `pool_float_hint`, inclusion conditionnelle de RULE #0…) : déterministes, mais varient selon les paramètres d'entrée et l'analyse Gemini.

### Ce que cette répartition révèle

| Constat | Implication |
|---|---|
| **83-88 % du prompt est identique à chaque photo** | Si Nano Banana a du mal à respecter une règle, le rajout d'instructions dans la partie hardcoded a un poids de plus en plus faible (dilution attention). |
| **Les règles "no zoom / no decor change" sont noyées dans 4000+ tokens de hardcoded** | Difficile pour Nano Banana de prioriser : 200 tokens `ABSOLUTE SIZE LIMIT` vs 4000 tokens d'autres règles. D'où les bugs de framing / décor altéré. |
| **Seul ~10-16 % du prompt est adapté à la photo réelle** | Beaucoup de marge pour pousser plus de personnalisation contextuelle. |
| **`target_n` et `safe_zones` sont les SEULES variables qui matchent vraiment la scène** | Tout le reste (Face Quality, Water Depth Physics, Photography Style…) reste générique. |

---

## 💡 Pistes pour réduire la part hardcoded (= augmenter la part probabiliste)

L'hypothèse : **plus le prompt est adapté à la photo précise, plus Nano Banana respecte ses contraintes** (moins de dilution sur des règles non pertinentes).

### Niveau 1 — Quick wins (1-2 jours, opt-in via env var)

| Action | Bloc concerné | Effet attendu |
|---|---|---|
| Faire écrire à Gemini Vision le bloc `pitfalls_specific_to_this_photo` adapté à CETTE photo (au lieu des règles génériques NEGATIVE PROMPT) | NEGATIVE PROMPT | Réduit ~500 tokens de hardcoded en ~150 tokens ciblés |
| Faire générer le `lighting_hint` adapté à la palette détectée par Gemini | LIGHTING & REALISM | Plus pertinent que la phrase générique actuelle |
| Faire générer le `scale_hint_specific` (calculé à partir du shot_type Gemini : wide vs medium vs close) | SCALE LOCK + ABSOLUTE SIZE LIMIT | Évite que la règle "head ≤ 8 % frame height" soit ignorée sur les wides où elle est critique |
| Générer dynamiquement le `negative_prompt` ciblé sur les violations historiques de CE type de photo | Negative Prompts | Pertinence x3, taille réduite |

### Niveau 2 — Refonte architecturale (1-2 semaines)

Compression du hardcoded en abstractions plus haute densité :

| Avant | Après |
|---|---|
| Water Depth Physics : 350 tokens de règles détaillées | "Subject in pool : chest-deep mandatory (rule reference WD-1)" — 15 tokens, le détail est dans la doc système |
| Pool Deletion : 200 tokens | "Preserve pool surface 100 % (rule PD-1)" — 10 tokens |
| Face Quality : 200 tokens | "Face quality : PHOTO-1 standard (3/4 angle preferred)" — 12 tokens |

Risque : Nano Banana ne reconnaît pas ces "rule references" → tester avant de déployer.

### Niveau 3 — Pipeline 2-passes Crop-Inpaint-Paste

Approche structurelle qui rend la part hardcoded NON NÉCESSAIRE :

1. Gemini Vision retourne une **bounding box précise** où placer le sujet (x, y, w, h)
2. On **crop** l'image autour de cette bbox (+ padding)
3. On envoie à Nano Banana **uniquement le crop** avec un prompt **ultra-court** (~500 tokens : "place subject X on this surface")
4. On **colle** le résultat dans l'image originale

Bénéfices :
- ~80 % du hardcoded devient inutile (pas besoin de "DO NOT change architecture" si le modèle ne voit que la zone autorisée)
- Élimine quasi tous les bugs `architecture_changed`, `decor_elements_lost`, `subject_oversized`
- Coût : +1 semaine de dev

---

## 📋 Annexe — Exemple de prompt pour une photo

Pour générer le prompt FINAL d'une photo précise avec annotations 🔵/🟡/⬜ par ligne, on peut ajouter un mode debug :

```bash
export DEBUG_PROMPT_ANATOMY=1
```

Qui sortirait dans les logs Flask un dump coloré du prompt envoyé pour chaque appel `ai_add_character`. (À implémenter si besoin pour audit visuel.)

---

## Comment maintenir ce document

À mettre à jour quand on modifie significativement :
- `_legacy_prompts.py::build_persona_prompt_v1_long` (ajout/suppression de blocs)
- `scenario_writer.py::META_PROMPT_TEMPLATE` (méta-prompt qui guide Gemini Vision dans la rédaction du scenario)
- `analyze.py::SYSTEM_PROMPT` (structure du JSON retourné par Gemini Vision sur chaque photo)

Si l'audit montre une dérive (par ex part probabiliste qui chute), c'est le signal qu'on a re-empilé du hardcoded au lieu de chercher des solutions structurelles.
