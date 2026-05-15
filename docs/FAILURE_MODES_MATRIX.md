# Matrix des Failure Modes — Pipeline AI Hotel Photos

Document maintenu pour identifier les bugs récurrents du pipeline Gemini Image + Higgsfield, leur cause racine, leur mitigation actuelle, et leur statut.

Dernière mise à jour : **13/05/2026** (Martin J)

---

## Légende statut

- 🟢 **résolu** — bug identifié, fix déployé, vérifié sur 5+ photos
- 🟡 **mitigé** — fix déployé mais taux résiduel non nul, surveillance active
- 🔴 **ouvert** — bug identifié, pas encore résolu
- ⚪ **observé** — bug rapporté une seule fois, à confirmer

---

## Section A — `ai_add_character` (= principal failure mode)

| ID | Symptôme | Cause racine | Mitigation | Statut |
|----|----------|--------------|------------|--------|
| **A.1** | Pool deletion / shrink (la piscine disparaît pour faire de la place aux loungers) | Gemini Image préfère "satisfaire le scenario" plutôt que respecter l'interdit. Scenario hardcoded "Place 3 adjacent loungers" + photo a piscine au centre → suppression piscine. | Bloc `🚨🚨 PARTICULAR ATTENTION — WATER / POOL DELETION` en début de prompt + `architecture_changed` retry avec swap scenario vers `pool_edge`. | 🟢 |
| **A.2** | Humain placé du mauvais côté d'une barrière de sécurité (rooftop / balcon) | Gemini Image place selon scenario sans vérifier la barrière en verre/garde-corps au premier plan | Bloc `RULE #0 SAFETY BARRIER LOCK` injecté en tête du prompt si `_detect_barrier_risk(unsafe_zones, category)` retourne True + cap `target_n=1` + bascule `effective_persona=solos`. Validator `subject_wrong_side_barrier`. | 🟡 |
| **A.3** | Mobilier inventé (banc / coussin / daybed) au bord d'une piscine "nue" | Le scenario "pool_edge" pour `families`/`small_groups` demande plusieurs personnes assises au bord, Gemini ajoute un banc pour "asseoir confortablement" | Scenario `pool_edge` réécrit "DIRECTLY on existing bare pool deck (concrete / tile / wood)" + `🚫 DO NOT add any cushion clause` + retry `invented_furniture` swap vers scenario plus simple | 🟡 |
| **A.4** | Mobilier inventé en **arrière-plan** (pas détecté par validator) — *bug 13/05/2026 Moxy famille piscine* | Validator scannait primo-plan seulement, manquait les inventions background | (13/05) Renforcement validator avec scan **grille 3×3** explicite (ÉTAPE 2.bis) qui force l'inspection de chaque région de l'image | 🟡 (à vérifier sur run) |
| **A.5** | Humains disproportionnés (trop grands vs scène) — *bug 13/05/2026 Moxy rooftop trio* | Scenario décrit pose mais pas distance ; SCALE LOCK existait mais perdu dans 5000 tokens | (13/05) `🚨 ABSOLUTE SIZE LIMIT` ajouté avec règle "head ≤ 8% frame height, body ≤ 25% frame width" + violation `subject_oversized` dans validator + ACTIONABLE_VIOLATIONS + retry reinforcement message | 🟡 (à vérifier sur run) |
| **A.6** | Yoga sur tapis de course — *bug 13/05/2026 Moxy gym* | `_classify_safe_zone("tapis de course")` → "unknown" → fallback `gym_mat` (yoga) ; `_SCENARIO_CATALOG[(solos, gym_mat)]` impose pose yoga | (13/05) Enrichissement `_classify_safe_zone` (FR + cardio_machine, weight_bench, weights_area) + scenarios spécifiques + scenario `AUTO-DESCRIPTION` qui parse les mots-clés de la safe_zone (running/yoga/sitting/standing) | 🟢 (testé) |
| **A.7** | Contradiction `target_n=1` vs scenario "Place exactly THREE subjects" | Si `barrier_risk` cap target_n à 1, le scenario hardcoded reste "couples" / "small_groups" qui décrit 2-3 sujets | (13/05) Bascule `effective_persona=solos` AVANT pick_scenario + `_coerce_scenario_count()` patche le texte du scenario pour aligner avec target_n | 🟢 (testé) |
| **A.8** | Sujet debout DANS la piscine avec eau aux genoux (`shallow_water_illusion`) | Gemini Image ne respecte pas la profondeur réaliste | Bloc `🌊🚨 WATER DEPTH PHYSICS` avec test visuel + violation dans ACTIONABLE_VIOLATIONS | 🟡 |
| **A.9** | Visages déformés / mannequin / plastique | Faces small/medium-distance sont la #1 failure mode de Nano Banana | Bloc `FACE QUALITY` recommandant 3/4 angle + sunglasses + hat brim shadow | 🟡 |
| **A.10** | Personnages qui regardent la caméra (look catalog/staged) | Tendance Gemini Image | Phrase "NEVER look at the camera" dans chaque scenario + `🚫 Posed models facing camera` dans NEGATIVE | 🟢 |
| **A.11** | Transat/daybed flottant inventé AU MILIEU de la piscine — *bug 13/05/2026 Moxy piscine famille* | `target_n=4` (calculé via capacity Gemini) mais seulement 3 safe_zones disponibles → Gemini invente un 4e support flottant pour placer le surplus | (13/05 v4) `target_n` capé par `len(safe_zones) + bonus(multi_seat_kws)` AVANT pick scenario + bloc explicite `INVENTED FLOATING FURNITURE` dans prompt + détection validator "rigid furniture on water surface" → `invented_furniture` | 🟡 (à vérifier sur run) |
| **A.12** | Validator faux positif `invented_pool_float` sur bouée préexistante — *bug 13/05/2026 Moxy* | Validator regardait juste "bouée dans la piscine output" sans comparer avec input | (13/05 v4) Renforcement prompt validator : "AVANT de flag `invented_pool_float`, vérifie systématiquement dans l'image originale si une bouée du même type existait déjà. Si oui → PAS de violation." | 🟡 (à vérifier) |

---

## Section B — `ai_lighting` (nuit → jour)

| ID | Symptôme | Cause racine | Mitigation | Statut |
|----|----------|--------------|------------|--------|
| **B.1** | Fenêtre fabriquée à la place d'un mur opaque (`architecture_invented`) | Gemini Image "ouvre" la scène pour ajouter de la lumière naturelle | Bloc `🚨 ABSOLUTE ARCHITECTURAL PRESERVATION` + violation `architecture_invented` retry obligatoire | 🟡 |
| **B.2** | Lampes/sconces toujours allumées en plein jour | Gemini garde les fixtures émissives même sous le soleil | Bloc `💡 ARTIFICIAL LIGHTS — TURN THEM OFF / DIM TO INVISIBLE` | 🟢 |
| **B.3** | Mur de couleur remplacé par mur blanc/pastel | Tendance "beautify" de Gemini | Bloc preservation décor + neon | 🟡 |

---

## Section C — `ai_remove_clutter`

| ID | Symptôme | Cause racine | Mitigation | Statut |
|----|----------|--------------|------------|--------|
| **C.1** | Ajoute des éléments au lieu de juste retirer | `ai_remove_clutter` parfois interprété comme "améliorer" | Bloc `🛑 ADDITION-FREE RULE (#1, MOST IMPORTANT)` | 🟢 |
| **C.2** | Supprime à tort barrières de sécurité, fences piscine | Gemini les voit comme "industrial / ugly" | Bloc `⛔ DO NOT TOUCH permanent safety barriers` | 🟢 |
| **C.3** | Supprime à tort des bouées décoratives flottantes | Gemini les voit comme "clutter" | Bloc `KEEP : Decorative inflatable pool floats` | 🟢 |

---

## Section D — Slowmo (Higgsfield Kling)

| ID | Symptôme | Cause racine | Mitigation | Statut |
|----|----------|--------------|------------|--------|
| **D.1** | Visages déformés sur photos avec ai_add_character | Higgsfield Kling anime + déforme légèrement les visages générés | `pick_slowmo_target` exclut désormais les photos avec `persona_used` ou `ai_add_character` step (paramètre `enhanced_results`) | 🟢 |
| **D.2** | "Not enough credits" inattendu | 2 wallets distincts : higgsfield.ai (consumer) vs cloud.higgsfield.ai (API) | Documentation explicite + message d'erreur amélioré | 🟢 |
| **D.3** | Réponse "nsfw" sans contexte clair | Higgsfield classifie certaines photos NSFW (faux positifs sur swimwear) | Détection status="nsfw" + message user-friendly + flag `nsfw_blocked` | 🟢 |
| **D.4** | Slowmo généré non visible en "Reprendre depuis sélection" + case décochée | run.json écrasé avec slowmo_result=null lors des resume sans regen | Cache reuse en 2 étapes : (a) lit run.json (b) fallback disk-scan `/slowmo/*.mp4` + préservation du slowmo_result précédent dans run.json | 🟢 |
| **D.5** | Mouvement trop subtle / on ne voit rien bouger | motion_subject="water" + prompt "Subtle ripples" peu visible sur petite piscine | Suggestion : changer motion_subject vers `pool_float` (bouée) ou `curtains` (rideaux) qui bougent plus visiblement | ⚪ |

---

## Section E — Pipeline / UX

| ID | Symptôme | Cause racine | Mitigation | Statut |
|----|----------|--------------|------------|--------|
| **E.1** | Photos sources < 500×320 supprimées par défaut | Filtre N1_ingestion trop strict | Remplacé par upscale Lanczos x1.2-x1.5 | 🟢 |
| **E.2** | Bouton "Analyse en cours" reste bloqué alors que pipeline terminé | `build_photo_journey` calculé inline dans `return jsonify` AFTER progress.finish | Pré-calcul `photo_journey_payload` BEFORE progress.finish | 🟢 |
| **E.3** | Compteur "SLOWMO 2 ✓" pour 1 vrai slowmo (compte les `_raw.mp4`) | Counter incluait variant suffixes | Exclusion `_raw`, `_story_9x16`, `_feed_1x1`, `_youtube_16x9` | 🟢 |

---

## 🚀 V5 — Vision-Generated Scenario Writer (13/05/2026)

**Décision Martin** : passage du catalogue Python hardcoded à un scenario généré dynamiquement par Gemini Vision sur chaque photo.

### Architecture

```
analyze.py (Gemini Vision)           # JSON safe_zones (inchangé)
        ↓
scenario_writer.py (Gemini Vision)   # NEW : génère scenario adapté à la photo
        ↓
enhance.py / build_persona_prompt    # injecte scenario_block_override
        ↓
Nano Banana
        ↓
ai_validator.py (Gemini Vision)      # validation post-IA (inchangé)
```

### Pourquoi
Le catalogue Python (`_SCENARIO_CATALOG[(persona, zone_type)]`) était AVEUGLE à la photo réelle :
- Devinait via mots-clés (ex: "gym" → yoga, peu importe l'équipement réel visible)
- Hardcodait des poses incohérentes avec les zones réelles
- Hardcodait un count de sujets fixe, incompatible avec la capacité visible

Vision a la photo en main → génère un scenario où :
- `primary_anchor` = description PRÉCISE d'un élément EXISTANT
- `max_subjects_realistic` = capacité RÉELLE (vu sur la photo)
- `pose_description` = adaptée à l'équipement réel
- `pitfalls_specific_to_this_photo` = dangers propres à CETTE photo
- `alternative_anchors` = fallbacks si le primary échoue

### Coût marginal
- +$0.001 à $0.002 par photo retouchée (1 appel Gemini Vision supplémentaire)
- +15-20s par photo (latence)
- Sur un pack de 12 photos = +$0.02 et +3min total

### Fallback
Si scenario_writer échoue (API down, image absente, JSON malformé) → fallback automatique vers l'ancien catalogue Python. **Aucune régression** : on garde l'ancien comportement comme filet de sécurité.

### Activation / rollback
```bash
# Par défaut (V5 actif, recommandé) :
# rien à faire

# Rollback vers catalogue Python (V4) :
export USE_LEGACY_SCENARIO_CATALOG=1
```

### Bugs attendus résolus par V5
- **A.6** Yoga sur tapis de course → Vision voit le tapis et propose "running on treadmill", pas yoga
- **A.7** Contradiction target_n vs scenario → Vision génère le bon count dès le départ
- **A.11** Transat flottant inventé → Vision identifie la capacité réelle et propose pool_edge si pas la place
- **A.6'** Chaise inventée pour caser un humain (Moxy gym detail #11) → Vision propose un anchor existant ou skip

---

## Stratégie globale d'amélioration

### Priorité 1 : prompt clarity (court terme)
- Continuer le travail de **dilution → concentration** : moins de doublons, signal critique plus haut
- Tester progressivement V2 compacte par sous-segments (rooftop seulement, gym seulement)

### Priorité 2 : validator robustness (court terme)
- Renforcer **scan grille 3×3** sur background (en cours)
- Ajouter détection `subject_oversized` (fait 13/05) — surveiller taux de déclenchement

### Priorité 3 : architectural (moyen terme)
- **Migrer vers Imagen 3 (Vertex AI) avec masked inpainting** pour les cas critiques (rooftop, gym). Le mask zone autorisée = blanc, reste = noir → Gemini ne peut **physiquement pas** modifier l'arrière-plan. Coût ~3x mais qualité contrôle bien supérieure.
- Pipeline en 2 étapes : (a) Gemini Vision calcule bounding box précise dans la zone safe, (b) Imagen inpaint cette région seule
- Estimation : 2-3 semaines de dev, gain ~80% sur les bugs "invention décor"

### Priorité 4 : LLM-as-judge (moyen terme)
- Faire valider le résultat final par Gemini Vision avec un prompt très spécifique sur **chaque** failure mode listé ci-dessus
- Si > 2 violations détectées → fallback original automatique (pas de retry)

---

## Comment ajouter un bug ici

1. Lui donner un ID séquentiel dans la bonne section (A/B/C/D/E)
2. Décrire le **symptôme** observé (1 phrase, factuel)
3. Identifier la **cause racine** technique (pas juste "Gemini fait n'importe quoi")
4. Documenter la **mitigation** (commit, fichier, ligne)
5. Statut initial = 🔴 ouvert ou ⚪ observé ; passer à 🟡 mitigé puis 🟢 résolu après vérification
