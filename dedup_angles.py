"""Dédup d'angles — détecte les photos quasi-identiques (même cadrage, angle proche).

Approche : perceptual hash (pHash) + clustering par distance de Hamming.
- pHash transforme une image en un vecteur 64-bit invariant à la couleur/compression mais sensible à la composition.
- Distance de Hamming entre deux pHash = nombre de bits différents.
- Seuil typique : ≤ 10 bits de différence ≈ "même photo / même cadrage".

Usage standalone :
    python dedup_angles.py data/input/

Usage programmatique :
    from dedup_angles import find_duplicate_clusters
    clusters = find_duplicate_clusters(["a.jpg", "b.jpg", ...], threshold=10)
"""

from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image
import imagehash


# Seuil de Hamming : ≤ THRESHOLD = considérées comme doublons d'angle.
# 14 = bon compromis pour détecter les "même cadrage avec micro-variations" (couleur, exposition légère).
# 10 = trop strict (laisse passer des doublons quasi-identiques).
DEFAULT_THRESHOLD = 16


def hash_image(path: Path) -> imagehash.ImageHash:
    """pHash perceptual de l'image."""
    with Image.open(path) as img:
        return imagehash.phash(img)


def find_duplicate_clusters(paths: list[Path], threshold: int = DEFAULT_THRESHOLD) -> list[list[Path]]:
    """Regroupe les images par similarité d'angle.

    Returns:
        Liste de clusters. Chaque cluster = liste de paths considérés comme doublons.
        Les images uniques (pas de doublon) sont dans des clusters de taille 1.
    """
    if not paths:
        return []

    # Calcule tous les hashes
    hashes = [(p, hash_image(p)) for p in paths]

    # Clustering union-find naïf : O(n²) mais suffisant pour 30 photos par hôtel
    n = len(hashes)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            distance = hashes[i][1] - hashes[j][1]  # Hamming distance via __sub__
            if distance <= threshold:
                union(i, j)

    # Regroupe par root
    clusters: dict[int, list[Path]] = {}
    for i, (p, _) in enumerate(hashes):
        root = find(i)
        clusters.setdefault(root, []).append(p)

    return list(clusters.values())


def select_best_per_cluster(clusters: list[list[Path]], scores: dict[Path, float] | None = None) -> tuple[list[Path], list[Path]]:
    """Pour chaque cluster, garde le 'meilleur' selon scores (ou le premier si pas de scores).

    Returns:
        (kept, dropped) — liste des photos gardées, liste des photos écartées comme doublons.
    """
    kept = []
    dropped = []
    for cluster in clusters:
        if len(cluster) == 1:
            kept.append(cluster[0])
            continue
        if scores:
            best = max(cluster, key=lambda p: scores.get(p, 0))
        else:
            best = cluster[0]
        kept.append(best)
        dropped.extend(p for p in cluster if p != best)
    return kept, dropped


def main():
    if len(sys.argv) < 2:
        print("Usage: python dedup_angles.py <dossier_images> [threshold]")
        sys.exit(1)

    folder = Path(sys.argv[1])
    threshold = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_THRESHOLD

    paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    print(f"# Dédup sur {len(paths)} photos (threshold={threshold})")

    clusters = find_duplicate_clusters(paths, threshold)
    kept, dropped = select_best_per_cluster(clusters)

    print(f"\n→ {len(clusters)} clusters distincts")
    for i, c in enumerate(clusters, 1):
        if len(c) > 1:
            print(f"\n  Cluster {i} (taille {len(c)} — doublons d'angle):")
            for p in c:
                marker = "✓ kept" if p in kept else "✗ dropped"
                print(f"    [{marker}] {p.name}")

    print(f"\n→ Final : {len(kept)} photos uniques, {len(dropped)} doublons écartés")


if __name__ == "__main__":
    main()
