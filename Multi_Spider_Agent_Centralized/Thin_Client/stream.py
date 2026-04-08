#!/usr/bin/env python3
"""
PiCrawler UDP video streamer.

Captures camera frames and pushes them via UDP to a target address.
The target is set at runtime by Robot Agent via HTTP:

  POST /stream/start  {"target_host": "10.229.117.154", "target_port": 5000}
  POST /stream/stop

Also provides:
  GET /status   — streaming state
  GET /photo    — single JPEG snapshot (for debugging)
"""

import logging
import os
import signal
import socket
import struct
import sys
import threading
from time import sleep, time

import cv2
from flask import Flask, Response, jsonify, request
from vilib import Vilib

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "stream.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# State
_running = True
_streaming = False
_target_host = ""
_target_port = 0
_udp_sock: socket.socket = None
_stream_thread: threading.Thread = None
_fps_target = 10  # frames per second


def _get_frame():
    img = getattr(Vilib, "flask_img", None)
    if img is None or not hasattr(img, "shape"):
        return None
    try:
        return img.copy()
    except Exception:
        return None


def _warmup(timeout_s=8):
    for _ in range(int(timeout_s / 0.1)):
        if _get_frame() is not None:
            return True
        sleep(0.1)
    return False


def _stream_loop():
    """Background thread: capture frames and send via UDP."""
    global _streaming

    interval = 1.0 / _fps_target
    logger.info(f"UDP stream started -> {_target_host}:{_target_port} ({_fps_target} fps)")

    while _streaming and _running:
        t0 = time()
        frame = _get_frame()
        if frame is None:
            sleep(0.05)
            continue

        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            sleep(0.02)
            continue

        data = jpg.tobytes()

        # Send frame: 4-byte length header + JPEG data
        # Split into chunks if > 60000 bytes (UDP max safe payload)
        try:
            chunk_size = 60000
            total = len(data)
            num_chunks = (total + chunk_size - 1) // chunk_size

            # Header: total_size (4B) + num_chunks (2B) + frame_id (4B)
            frame_id = int(t0 * 1000) & 0xFFFFFFFF
            for i in range(num_chunks):
                offset = i * chunk_size
                chunk = data[offset:offset + chunk_size]
                # Chunk header: frame_id(4) + chunk_index(2) + num_chunks(2) + chunk_data
                header = struct.pack("!IHH", frame_id, i, num_chunks)
                _udp_sock.sendto(header + chunk, (_target_host, _target_port))

        except Exception as e:
            logger.error(f"UDP send error: {e}")
            sleep(0.1)

        elapsed = time() - t0
        if elapsed < interval:
            sleep(interval - elapsed)

    logger.info("UDP stream stopped")


# ── HTTP control endpoints ──────────────────────────────

@app.route("/stream/start", methods=["POST"])
def stream_start():
    global _streaming, _target_host, _target_port, _udp_sock, _stream_thread, _fps_target

    data = request.get_json(silent=True) or {}
    _target_host = data.get("target_host", "")
    _target_port = int(data.get("target_port", 0))
    _fps_target = int(data.get("fps", 10))

    if not _target_host or not _target_port:
        return jsonify({"error": "target_host and target_port required"}), 400

    # Stop existing stream if running
    if _streaming:
        _streaming = False
        if _stream_thread:
            _stream_thread.join(timeout=2)

    _udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _streaming = True
    _stream_thread = threading.Thread(target=_stream_loop, daemon=True)
    _stream_thread.start()

    return jsonify({"status": "streaming", "target": f"{_target_host}:{_target_port}", "fps": _fps_target})


@app.route("/stream/stop", methods=["POST"])
def stream_stop():
    global _streaming
    _streaming = False
    return jsonify({"status": "stopped"})


@app.route("/photo")
def photo():
    frame = _get_frame()
    if frame is None:
        return Response("No frame available\n", status=503, mimetype="text/plain")
    ok, jpg = cv2.imencode(".jpg", frame)
    if not ok:
        return Response("Encode failed\n", status=500, mimetype="text/plain")
    return Response(jpg.tobytes(), mimetype="image/jpeg")


@app.route("/status")
def status():
    return jsonify({
        "camera": _get_frame() is not None,
        "streaming": _streaming,
        "target": f"{_target_host}:{_target_port}" if _streaming else None,
        "fps": _fps_target,
    })


# ── Main ───────────────────────────────────────────────

def cleanup():
    global _running, _streaming
    _running = False
    _streaming = False
    try:
        Vilib.camera_close()
    except Exception:
        pass


def handle_signal(signum, frame):
    cleanup()
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("Starting camera...")
    Vilib.camera_start(vflip=False, hflip=False)

    if not _warmup(8):
        logger.warning("Camera warmup timed out")

    logger.info("Camera ready. Waiting for stream/start command.")

    try:
        app.run(host="0.0.0.0", port=9000, threaded=True, debug=False, use_reloader=False)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
