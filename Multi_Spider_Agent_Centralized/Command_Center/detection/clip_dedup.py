"""CLIP-based visual similarity pre-clustering for dedup.

VLM dedup is expensive (~3s per label-bucket VLM call) and occasionally
inconsistent on "same object different angle" vs "different object same
color" judgements.  CLIP cosine similarity between crop embeddings is
faster (≥ 1000 crops/s on a 4090) and more stable for pure visual
similarity — exactly what dedup needs.

This module:
  1. Loads the CLIP model lazily once per process, on GPU when available.
  2. Batch-embeds all track crops in one forward pass (~60 ms for 40 crops).
  3. Clusters keys by agglomerative grouping on cosine similarity.

The caller (`_vlm_dedup_objects`) uses CLIP groups as the primary dedup
result when CLIP is confident (tight intra-cluster, clean inter-cluster
separation), and falls back to VLM only when clusters are ambiguous or
can't be formed with confidence.
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_model = None
_processor = None
_device: Optional[str] = None
_load_lock = threading.Lock()


def _load():
    """Lazy-load the CLIP model on first use.  Thread-safe."""
    global _model, _processor, _device
    if _model is not None:
        return _model, _processor, _device
    with _load_lock:
        if _model is not None:
            return _model, _processor, _device
        import torch
        from transformers import CLIPModel, CLIPProcessor
        _device = "cuda" if torch.cuda.is_available() else "cpu"
        model_name = "openai/clip-vit-base-patch32"
        logger.info(f"CLIP dedup: loading {model_name} on {_device}")
        _model = CLIPModel.from_pretrained(model_name).to(_device).eval()
        _processor = CLIPProcessor.from_pretrained(model_name)
        logger.info("CLIP dedup: model ready")
    return _model, _processor, _device


def embed_crops(crops: dict[str, bytes]) -> dict[str, np.ndarray]:
    """Embed every crop with CLIP. Returns {key → L2-normalized np.float32 [512]}.

    One forward pass for all crops (batched).  Gracefully returns an
    empty dict on any error — caller falls back to the VLM path.
    """
    if not crops:
        return {}
    try:
        import torch
        from PIL import Image
        model, processor, device = _load()
        keys = list(crops.keys())
        images = []
        for k in keys:
            try:
                img = Image.open(io.BytesIO(crops[k])).convert("RGB")
                images.append(img)
            except Exception as e:
                logger.warning(f"CLIP dedup: decode failed for {k}: {e}")
                return {}
        with torch.no_grad():
            inputs = processor(images=images, return_tensors="pt").to(device)
            emb = model.get_image_features(**inputs)
            emb = emb / emb.norm(dim=-1, keepdim=True)
        emb_np = emb.detach().cpu().numpy().astype(np.float32)
        return {k: emb_np[i] for i, k in enumerate(keys)}
    except Exception as e:
        logger.warning(f"CLIP dedup: embedding failed: {e}")
        return {}


def cluster_keys(
    keys: list[str],
    embeddings: dict[str, np.ndarray],
    merge_threshold: float = 0.90,
) -> list[list[str]]:
    """Agglomerative clustering of `keys` by cosine similarity.

    Start with each key in its own cluster; iteratively merge the two
    clusters whose MAX pairwise inter-cluster similarity exceeds
    `merge_threshold`, until no more merges qualify.

    A merge threshold of 0.90 is conservative — same object from
    moderately different angles typically scores 0.92-0.98; different
    physical objects of similar class usually score < 0.85.

    Keys without embeddings become their own singleton clusters.
    """
    if not keys:
        return []
    # Split into with/without-embedding
    with_emb = [k for k in keys if k in embeddings]
    without_emb = [k for k in keys if k not in embeddings]
    clusters: list[list[str]] = [[k] for k in with_emb]

    def _max_sim(c_i, c_j):
        # Max cosine sim between any pair across the two clusters
        best = -1.0
        for a in c_i:
            ea = embeddings[a]
            for b in c_j:
                eb = embeddings[b]
                s = float(np.dot(ea, eb))
                if s > best:
                    best = s
        return best

    while True:
        n = len(clusters)
        if n < 2:
            break
        best_i, best_j, best_s = -1, -1, -1.0
        for i in range(n):
            for j in range(i + 1, n):
                s = _max_sim(clusters[i], clusters[j])
                if s > best_s:
                    best_s, best_i, best_j = s, i, j
        if best_s < merge_threshold:
            break
        # Merge j into i; remove j
        clusters[best_i] = clusters[best_i] + clusters[best_j]
        clusters.pop(best_j)

    # Tack on singletons for keys without embeddings
    for k in without_emb:
        clusters.append([k])
    return clusters


def confidence(clusters: list[list[str]], embeddings: dict[str, np.ndarray]) -> float:
    """Rough confidence score for a clustering — higher = more trustworthy.

    Computes: (min intra-cluster similarity) - (max inter-cluster
    similarity).  Positive means all clusters are tighter internally than
    they are to each other.  Near-zero or negative = ambiguous (VLM
    fallback recommended).
    """
    if not clusters or len(clusters) < 2:
        return 1.0
    min_intra = 1.0
    for c in clusters:
        if len(c) <= 1:
            continue
        for i in range(len(c)):
            for j in range(i + 1, len(c)):
                if c[i] in embeddings and c[j] in embeddings:
                    s = float(np.dot(embeddings[c[i]], embeddings[c[j]]))
                    if s < min_intra:
                        min_intra = s
    max_inter = -1.0
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            for a in clusters[i]:
                for b in clusters[j]:
                    if a in embeddings and b in embeddings:
                        s = float(np.dot(embeddings[a], embeddings[b]))
                        if s > max_inter:
                            max_inter = s
    if max_inter < 0:
        return 1.0
    return min_intra - max_inter
