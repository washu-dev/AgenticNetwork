"""
AprilTag detection module.

Per-frame AprilTag detection running alongside YOLO. Used as position
anchors: when a robot's camera sees a tag, the system knows that robot
is at (approximately) that tag's real-world location.

Tag positions are loaded from config_data/tag_positions.json.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_detector = None
_tag_positions: dict = {}  # str(tag_id) → {"zone": str, "pos": [x, y]}


def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config_data" / "tag_positions.json"


def load_tag_positions(path: Optional[Path] = None) -> dict:
    """Load tag → {zone, pos} mapping from JSON. Strips metadata keys."""
    global _tag_positions
    cfg_path = path or _config_path()
    try:
        with open(cfg_path) as f:
            raw = json.load(f)
        _tag_positions = {
            str(k): v for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, dict) and "pos" in v
        }
        logger.info(f"Loaded {len(_tag_positions)} AprilTag positions from {cfg_path}")

        # Warn about placeholder [0.0, 0.0] values — these are almost certainly
        # unfilled defaults, not real measurements, and would collapse all robots
        # to the origin when computing nearest-robot distance.
        placeholders = [
            tid for tid, v in _tag_positions.items()
            if list(v.get("pos", [])) == [0.0, 0.0]
        ]
        if placeholders:
            logger.warning(
                f"{len(placeholders)} AprilTag(s) have placeholder position [0.0, 0.0]: "
                f"{sorted(placeholders)}. Fill in real measurements before running support-dispatch demos."
            )
    except FileNotFoundError:
        logger.warning(f"Tag positions config not found at {cfg_path}, starting empty")
        _tag_positions = {}
    except Exception as e:
        logger.error(f"Failed to load tag positions: {e}")
        _tag_positions = {}
    return _tag_positions


def get_tag_info(tag_id: int) -> Optional[dict]:
    """Return {zone, pos} for a tag, or None if unknown."""
    return _tag_positions.get(str(tag_id))


def _get_detector():
    """Lazy-load the AprilTag detector."""
    global _detector
    if _detector is None:
        try:
            from pupil_apriltags import Detector
            _detector = Detector(
                families="tag36h11",
                nthreads=2,
                quad_decimate=1.0,
                quad_sigma=0.0,
                refine_edges=1,
                decode_sharpening=0.25,
                debug=0,
            )
            logger.info("AprilTag detector initialized (tag36h11)")
        except ImportError:
            logger.error("pupil-apriltags not installed. Run: pip install pupil-apriltags")
            raise
    return _detector


def detect_tags(image_bytes: bytes) -> list[dict]:
    """Detect AprilTags in a JPEG frame.

    Returns:
        List of {tag_id, center, corners, decision_margin}. Empty list if no tags.
    """
    import cv2

    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    try:
        detector = _get_detector()
    except ImportError:
        return []

    raw = detector.detect(gray)
    results = []
    for det in raw:
        results.append({
            "tag_id": int(det.tag_id),
            "center": [float(det.center[0]), float(det.center[1])],
            "corners": [[float(c[0]), float(c[1])] for c in det.corners],
            "decision_margin": float(det.decision_margin),
        })
    return results


# Load on import
load_tag_positions()
