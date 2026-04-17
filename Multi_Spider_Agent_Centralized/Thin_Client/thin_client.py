"""
PiCrawler Thin Client — single-process server for hardware control + video stream.

Two HTTP servers in one process:
  - Port 9004: hardware control (commands, health, capabilities)
  - Port 9000: video stream (UDP push, photo snapshot)

Robot Agent (on server) sends commands via HTTP POST /command.
"""

import asyncio
import json
import logging
import os
import socket
import struct
import threading
import time

import cv2
import numpy as np
from picamera2 import Picamera2
import libcamera
from uvicorn import Config, Server
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_spider.config import AGENT_ID, AGENT_PORT
from agent_spider.capabilities import get_capabilities_json
from agent_spider.hardware import CrawlerControl

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "thin_client.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

STREAM_PORT = int(os.getenv("STREAM_PORT", "9000"))
CAMERA_FPS = int(os.getenv("CAMERA_FPS", "10"))

_start_time = time.time()
_control: CrawlerControl = None

# ── Camera (picamera2 direct) ────────────────────────────

_picam2: Picamera2 = None
_current_frame = None
_frame_lock = threading.Lock()
_camera_ready = False


def _camera_capture_loop():
    """Background thread: capture frames from picamera2 at CAMERA_FPS."""
    global _current_frame, _camera_ready

    interval = 1.0 / CAMERA_FPS
    logger.info(f"Camera capture loop started ({CAMERA_FPS} fps)")
    _camera_ready = True

    while True:
        t0 = time.time()
        try:
            frame = _picam2.capture_array()
            with _frame_lock:
                _current_frame = frame
        except Exception as e:
            logger.error(f"Camera capture error: {e}")
            time.sleep(0.1)
            continue

        elapsed = time.time() - t0
        if elapsed < interval:
            time.sleep(interval - elapsed)


def _get_frame():
    """Get latest camera frame (thread-safe copy)."""
    with _frame_lock:
        if _current_frame is None:
            return None
        return _current_frame.copy()


def _start_camera():
    """Initialize picamera2 and start capture thread."""
    global _picam2

    _picam2 = Picamera2()
    config = _picam2.create_preview_configuration(
        main={"size": (640, 480), "format": "RGB888"},
        buffer_count=4,
        controls={"FrameRate": CAMERA_FPS},
        transform=libcamera.Transform(hflip=False, vflip=False),
    )
    _picam2.configure(config)
    _picam2.start()

    capture_thread = threading.Thread(target=_camera_capture_loop, daemon=True)
    capture_thread.start()

    # Wait for first frame
    for _ in range(80):
        if _get_frame() is not None:
            return True
        time.sleep(0.1)
    return False


# ── Action map ─────────────────────────────────────────────

def _build_action_map(control: CrawlerControl) -> dict:
    return {
        "stand_up": control.stand,
        "sit_down": control.sit,
        "forward": control.forward,
        "backward": control.backward,
        "turn_left": control.turn_left,
        "turn_right": control.turn_right,
        "turn_left_angle": control.turn_left_angle,
        "turn_right_angle": control.turn_right_angle,
        "look_left": control.look_left,
        "look_right": control.look_right,
        "look_up": control.look_up,
        "look_down": control.look_down,
        "wave": control.wave,
        "dance": control.dance,
        "push_up": control.push_up,
        "nod": control.nod,
        "shake_head": control.shake_head,
        "shake_hand": control.shake_hand,
        "play_dead": control.play_dead,
        "stop": lambda **kw: None,
    }


_action_map: dict = {}
_action_lock = asyncio.Lock()


# ── Command handlers ─────────────────────────────────────

async def _execute_action(metadata: dict) -> dict:
    from agent_spider.hardware import Cancelled

    action = metadata.get("action", "stop")
    params = metadata.get("params", {})
    handler = _action_map.get(action)
    if not handler:
        return {"status": "error", "error": f"Unknown action: {action}"}

    if action == "stop":
        await asyncio.to_thread(_control.stop)
        return {"status": "ok", "action": "stop"}

    _control.cancel()

    async with _action_lock:
        _control._cancel.clear()
        t0 = time.time()
        try:
            await asyncio.to_thread(handler, **params)
            ms = int((time.time() - t0) * 1000)
            logger.info(f"Executed {action}({params}) in {ms}ms")
            return {"status": "ok", "action": action, "duration_ms": ms}
        except Cancelled:
            ms = int((time.time() - t0) * 1000)
            logger.info(f"Cancelled {action}({params}) after {ms}ms")
            return {"status": "cancelled", "action": action, "duration_ms": ms}
        except Exception as e:
            return {"status": "error", "action": action, "error": str(e)}


async def _read_sensor(metadata: dict) -> dict:
    sensor = metadata.get("sensor", "ultrasonic")
    if sensor == "ultrasonic":
        distance = await asyncio.to_thread(_control.read_distance)
        return {"distance_cm": distance}
    return {"error": f"Unknown sensor: {sensor}"}


# ── Control endpoints (port 9004) ────────────────────────

async def health(request: Request):
    return JSONResponse({
        "status": "ok",
        "agent_id": AGENT_ID,
        "uptime_s": int(time.time() - _start_time),
        "standing": _control._standing if _control else False,
    })


async def capabilities_endpoint(request: Request):
    data = get_capabilities_json()
    data["agent_id"] = AGENT_ID
    return JSONResponse(data)


async def status_endpoint(request: Request):
    return JSONResponse({
        "agent_id": AGENT_ID,
        "standing": _control._standing if _control else False,
        "uptime_s": int(time.time() - _start_time),
    })


async def command_endpoint(request: Request):
    try:
        metadata = await request.json()
        logger.info(f"Received command: {metadata}")
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    task_type = metadata.get("task_type", "execute")

    if task_type == "execute":
        result = await _execute_action(metadata)
    elif task_type == "read_sensor":
        result = await _read_sensor(metadata)
    elif task_type == "status":
        result = {
            "agent_id": AGENT_ID,
            "standing": _control._standing if _control else False,
            "uptime_s": int(time.time() - _start_time),
        }
    elif task_type == "notification":
        action = metadata.get("action")
        if action:
            result = await _execute_action(metadata)
        else:
            result = {"status": "ok"}
    else:
        result = {"status": "error", "error": f"Unknown task_type: {task_type}"}

    return JSONResponse(result)


# ── Video stream (port 9000) ─────────────────────────────

_streaming = False
_target_host = ""
_target_port = 0
_udp_sock: socket.socket = None
_stream_thread: threading.Thread = None
_fps_target = 10


def _stream_loop():
    global _streaming

    interval = 1.0 / _fps_target
    logger.info(f"UDP stream started -> {_target_host}:{_target_port} ({_fps_target} fps)")

    while _streaming:
        t0 = time.time()
        frame = _get_frame()
        if frame is None:
            time.sleep(0.05)
            continue

        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            time.sleep(0.02)
            continue

        data = jpg.tobytes()
        try:
            chunk_size = 60000
            total = len(data)
            num_chunks = (total + chunk_size - 1) // chunk_size
            frame_id = int(t0 * 1000) & 0xFFFFFFFF
            for i in range(num_chunks):
                offset = i * chunk_size
                chunk = data[offset:offset + chunk_size]
                header = struct.pack("!IHH", frame_id, i, num_chunks)
                _udp_sock.sendto(header + chunk, (_target_host, _target_port))
        except Exception as e:
            logger.error(f"UDP send error: {e}")
            time.sleep(0.1)

        elapsed = time.time() - t0
        if elapsed < interval:
            time.sleep(interval - elapsed)

    logger.info("UDP stream stopped")


async def stream_start(request: Request):
    global _streaming, _target_host, _target_port, _udp_sock, _stream_thread, _fps_target

    data = await request.json()
    _target_host = data.get("target_host", "")
    _target_port = int(data.get("target_port", 0))
    _fps_target = min(int(data.get("fps", 10)), 10)  # cap at 10fps

    if not _target_host or not _target_port:
        return JSONResponse({"error": "target_host and target_port required"}, status_code=400)

    if _streaming:
        _streaming = False
        if _stream_thread:
            _stream_thread.join(timeout=2)

    _udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _streaming = True
    _stream_thread = threading.Thread(target=_stream_loop, daemon=True)
    _stream_thread.start()

    return JSONResponse({"status": "streaming", "target": f"{_target_host}:{_target_port}", "fps": _fps_target})


async def stream_stop(request: Request):
    global _streaming
    _streaming = False
    return JSONResponse({"status": "stopped"})


async def stream_photo(request: Request):
    frame = _get_frame()
    if frame is None:
        return Response("No frame available\n", status_code=503, media_type="text/plain")
    ok, jpg = cv2.imencode(".jpg", frame)
    if not ok:
        return Response("Encode failed\n", status_code=500, media_type="text/plain")
    return Response(jpg.tobytes(), media_type="image/jpeg")


async def stream_status(request: Request):
    return JSONResponse({
        "camera": _camera_ready,
        "streaming": _streaming,
        "target": f"{_target_host}:{_target_port}" if _streaming else None,
        "fps": _fps_target,
    })


# ── Server startup ─────────────────────────────────────────

async def main():
    global _control, _action_map

    print("=" * 50)
    print(f"PiCrawler Thin Client")
    print(f"Robot:   {AGENT_ID}")
    print(f"Control: port {AGENT_PORT}")
    print(f"Stream:  port {STREAM_PORT}")
    print(f"Camera:  {CAMERA_FPS} fps capture")
    print("=" * 50)

    # Start camera (picamera2 direct, no vilib)
    logger.info("Starting camera...")
    if _start_camera():
        logger.info("Camera ready.")
    else:
        logger.warning("Camera warmup timed out")

    # Init hardware
    _control = CrawlerControl(speed=int(os.getenv("PATROL_SPEED", "50")))
    _action_map = _build_action_map(_control)

    # Control server (port 9004)
    control_app = Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/capabilities", capabilities_endpoint, methods=["GET"]),
        Route("/status", status_endpoint, methods=["GET"]),
        Route("/command", command_endpoint, methods=["POST"]),
    ])

    # Stream server (port 9000)
    stream_app = Starlette(routes=[
        Route("/stream/start", stream_start, methods=["POST"]),
        Route("/stream/stop", stream_stop, methods=["POST"]),
        Route("/photo", stream_photo, methods=["GET"]),
        Route("/status", stream_status, methods=["GET"]),
    ])

    control_server = Server(Config(app=control_app, host="0.0.0.0", port=AGENT_PORT, loop="asyncio"))
    stream_server = Server(Config(app=stream_app, host="0.0.0.0", port=STREAM_PORT, loop="asyncio"))

    await asyncio.gather(
        control_server.serve(),
        stream_server.serve(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
        if _picam2:
            try:
                _picam2.close()
            except Exception:
                pass
