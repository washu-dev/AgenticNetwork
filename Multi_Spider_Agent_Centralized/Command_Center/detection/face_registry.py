"""
Face Registry — register known faces and match against camera frames.

Uses InsightFace (RetinaFace + ArcFace) for:
- Face detection (RetinaFace)
- Face embedding (512-d vector from ArcFace)
- Matching via cosine similarity

All inference runs on Central (Pi only streams MJPEG).
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Storage directory for registered face data
_REGISTRY_DIR = Path(__file__).parent / "face_data"

# Lazy-loaded model (singleton)
_app = None


def _get_model():
    """Lazy-load InsightFace model."""
    global _app
    if _app is None:
        from insightface.app import FaceAnalysis

        _app = FaceAnalysis(
            name="buffalo_l",
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        _app.prepare(ctx_id=0, det_size=(640, 640))
        logger.info("InsightFace models loaded (RetinaFace + ArcFace)")
    return _app


def _decode_image(image_bytes: bytes) -> Optional[np.ndarray]:
    """Decode JPEG/PNG bytes to BGR numpy array."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    return frame


def compute_embedding(image_bytes: bytes) -> Optional[np.ndarray]:
    """Compute face embedding from an image. Returns 512-d array or None if no face found."""
    frame = _decode_image(image_bytes)
    if frame is None:
        return None

    app = _get_model()
    faces = app.get(frame)
    if not faces:
        logger.warning("No face detected in registration image")
        return None

    # Use the largest face (by bbox area)
    faces = sorted(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), reverse=True)
    return faces[0].embedding


def detect_and_embed_all(image_bytes: bytes) -> list[dict]:
    """Detect all faces in a frame and compute their embeddings.

    Returns list of {"bbox": [x1,y1,x2,y2], "embedding": ndarray, "confidence": float}
    """
    frame = _decode_image(image_bytes)
    if frame is None:
        return []

    app = _get_model()
    faces = app.get(frame)
    if not faces:
        return []

    results = []
    for face in faces:
        results.append({
            "bbox": face.bbox.tolist(),
            "embedding": face.embedding,
            "confidence": float(face.det_score),
        })

    return results


class FaceRegistry:
    """Registry of known faces with persistence."""

    def __init__(self, registry_dir: Optional[Path] = None):
        self.registry_dir = registry_dir or _REGISTRY_DIR
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self._faces: dict[str, dict] = {}  # name -> {"embedding": ndarray, "image_path": str}
        self._load()

    def _load(self):
        """Load registered faces from disk."""
        index_path = self.registry_dir / "index.json"
        if not index_path.exists():
            return

        try:
            with open(index_path) as f:
                index = json.load(f)

            for name, info in index.items():
                emb_path = self.registry_dir / f"{name}.npy"
                if emb_path.exists():
                    embedding = np.load(emb_path)
                    self._faces[name] = {
                        "embedding": embedding,
                        "image_path": info.get("image_path", ""),
                        "registered_at": info.get("registered_at", ""),
                    }
                else:
                    # Backward compatibility: try loading old .pt format
                    pt_path = self.registry_dir / f"{name}.pt"
                    if pt_path.exists():
                        import torch
                        embedding = torch.load(pt_path, map_location="cpu", weights_only=True).numpy()
                        self._faces[name] = {
                            "embedding": embedding,
                            "image_path": info.get("image_path", ""),
                            "registered_at": info.get("registered_at", ""),
                        }
                        # Re-save as .npy
                        np.save(emb_path, embedding)
                        logger.info(f"Migrated {name}.pt → {name}.npy")

            logger.info(f"Loaded {len(self._faces)} registered faces")
        except Exception as e:
            logger.error(f"Failed to load face registry: {e}")

    def _save_index(self):
        """Save index to disk."""
        index = {}
        for name, info in self._faces.items():
            index[name] = {
                "image_path": info.get("image_path", ""),
                "registered_at": info.get("registered_at", ""),
            }
        with open(self.registry_dir / "index.json", "w") as f:
            json.dump(index, f, indent=2)

    def register(self, name: str, image_bytes: bytes) -> dict:
        """Register a face with a name. Returns status dict."""
        embedding = compute_embedding(image_bytes)
        if embedding is None:
            return {"status": "error", "message": "No face detected in image"}

        # Save embedding as .npy
        emb_path = self.registry_dir / f"{name}.npy"
        np.save(emb_path, embedding)

        # Save reference image
        img_path = self.registry_dir / f"{name}.jpg"
        with open(img_path, "wb") as f:
            f.write(image_bytes)

        from datetime import datetime
        self._faces[name] = {
            "embedding": embedding,
            "image_path": str(img_path),
            "registered_at": datetime.utcnow().isoformat(),
        }
        self._save_index()

        logger.info(f"Registered face: {name}")
        return {"status": "ok", "name": name, "message": f"Face '{name}' registered"}

    def unregister(self, name: str) -> dict:
        """Remove a registered face."""
        if name not in self._faces:
            return {"status": "error", "message": f"'{name}' not found"}

        del self._faces[name]

        # Remove files
        for ext in [".npy", ".pt", ".jpg"]:
            p = self.registry_dir / f"{name}{ext}"
            if p.exists():
                p.unlink()

        self._save_index()
        logger.info(f"Unregistered face: {name}")
        return {"status": "ok", "message": f"Face '{name}' removed"}

    def list_faces(self) -> list[dict]:
        """List all registered faces."""
        return [
            {"name": name, "registered_at": info.get("registered_at", "")}
            for name, info in self._faces.items()
        ]

    def match(self, image_bytes: bytes, threshold: float = 0.4) -> list[dict]:
        """Match faces in an image against the registry.

        Returns list of {"name": str, "similarity": float, "bbox": [x1,y1,x2,y2]}
        for each detected face that matches a registered person.
        """
        if not self._faces:
            return []

        detected = detect_and_embed_all(image_bytes)
        if not detected:
            return []

        # Build registered embedding matrix
        reg_names = list(self._faces.keys())
        reg_embeddings = np.stack([self._faces[n]["embedding"] for n in reg_names])

        matches = []
        for det in detected:
            emb = det["embedding"]

            # Cosine similarity against all registered faces
            similarities = _cosine_similarity(emb, reg_embeddings)
            best_idx = int(np.argmax(similarities))
            best_sim = float(similarities[best_idx])

            if best_sim >= threshold:
                matches.append({
                    "name": reg_names[best_idx],
                    "similarity": round(best_sim, 3),
                    "bbox": det["bbox"],
                    "confidence": det["confidence"],
                })

        return matches

    def match_embedding(self, embedding: np.ndarray, threshold: float = 0.4) -> Optional[dict]:
        """Match a single embedding against the registry. Returns best match or None."""
        if not self._faces:
            return None

        reg_names = list(self._faces.keys())
        reg_embeddings = np.stack([self._faces[n]["embedding"] for n in reg_names])

        similarities = _cosine_similarity(embedding, reg_embeddings)
        best_idx = int(np.argmax(similarities))
        best_sim = float(similarities[best_idx])

        if best_sim >= threshold:
            return {"name": reg_names[best_idx], "similarity": round(best_sim, 3)}
        return None


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity between vector a and each row of matrix b."""
    a_norm = a / (np.linalg.norm(a) + 1e-8)
    b_norm = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-8)
    return b_norm @ a_norm


# Singleton instance
face_registry = FaceRegistry()
