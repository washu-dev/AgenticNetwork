"""
YOLO Object Detection module.

Runs YOLOv8 on Central to analyze frames from Pi camera streams.
Pi doesn't need YOLO installed - Central handles all inference.
"""

import io
import logging
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Lazy-loaded YOLO model (singleton)
_model = None
_model_name = "yolov8n.pt"  # nano model for speed; change to yolov8s.pt for accuracy


def _get_model():
    """Lazy-load YOLO model on first use."""
    global _model
    if _model is None:
        from ultralytics import YOLO
        logger.info(f"Loading YOLO model: {_model_name}")
        _model = YOLO(_model_name)
        logger.info("YOLO model loaded")
    return _model


def preload_model():
    """Pre-load YOLO model at startup so first detection is fast."""
    _get_model()


def detect_frame(image_bytes: bytes, conf_threshold: float = 0.25) -> dict:
    """
    Run YOLO detection on a single JPEG frame.

    Returns:
        {
            "detections": [{"label", "confidence", "bbox", "class_id"}],
            "inference_ms": float,
            "image_width": int,
            "image_height": int,
            "annotated_jpeg": bytes  # JPEG with bounding boxes drawn
        }
    """
    import cv2

    # Decode JPEG to numpy array
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return {"error": "Failed to decode image", "detections": []}

    h, w = frame.shape[:2]

    # Run YOLO inference
    model = _get_model()
    start = time.time()
    results = model(frame, conf=conf_threshold, verbose=False)
    inference_ms = (time.time() - start) * 1000

    # Parse results
    detections = []
    result = results[0]
    for box in result.boxes:
        cls_id = int(box.cls[0])
        conf = float(box.conf[0])
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        label = result.names[cls_id]

        detections.append({
            "label": label,
            "confidence": round(conf, 3),
            "class_id": cls_id,
            "bbox": {
                "x1": round(x1, 1),
                "y1": round(y1, 1),
                "x2": round(x2, 1),
                "y2": round(y2, 1),
            },
        })

    # Draw annotated image
    annotated = result.plot()
    _, jpeg_buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
    annotated_jpeg = jpeg_buf.tobytes()

    logger.info(f"YOLO: {len(detections)} objects in {inference_ms:.0f}ms ({w}x{h})")

    return {
        "detections": detections,
        "inference_ms": round(inference_ms, 1),
        "image_width": w,
        "image_height": h,
        "annotated_jpeg": annotated_jpeg,
    }


def grab_mjpeg_frame(mjpeg_data: bytes) -> Optional[bytes]:
    """Extract a single JPEG frame from MJPEG stream data."""
    start = mjpeg_data.find(b'\xff\xd8')  # JPEG SOI
    end = mjpeg_data.find(b'\xff\xd9', start)  # JPEG EOI
    if start != -1 and end != -1:
        return mjpeg_data[start:end + 2]
    return None
