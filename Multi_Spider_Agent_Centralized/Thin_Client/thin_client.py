"""
PiCrawler Thin Client — HTTP server for hardware control.

No LLM, no NATS, no registration. Just:
  - HTTP endpoints for receiving commands from Robot Agent (on server)
  - Video stream (via stream.py, separate process)

Robot Agent (on server) sends commands via HTTP POST /command.
"""

import asyncio
import json
import logging
import os
import time

from uvicorn import Config, Server
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
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

_start_time = time.time()
_control: CrawlerControl = None


# ── Action map ─────────────────────────────────────────────

def _build_action_map(control: CrawlerControl) -> dict:
    """Map action names to hardware callables."""
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

    # Emergency stop: bypass lock, freeze servos immediately
    if action == "stop":
        await asyncio.to_thread(_control.stop)
        return {"status": "ok", "action": "stop"}

    # Cancel whatever is running now
    _control.cancel()

    async with _action_lock:
        # Clear cancel flag before starting new action
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


# ── HTTP Endpoints ───────────────────────────────────────

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
    """Receive command from Robot Agent (on server) via HTTP POST.

    Request body: {"task_type": "execute|read_sensor|speak|notification", ...metadata}
    """
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


# ── Server startup ─────────────────────────────────────────

async def main():
    global _control, _action_map

    print("=" * 50)
    print(f"PiCrawler Thin Client")
    print(f"Robot:  {AGENT_ID}")
    print(f"Port:   {AGENT_PORT}")
    print("=" * 50)

    _control = CrawlerControl(speed=int(os.getenv("PATROL_SPEED", "50")))
    _action_map = _build_action_map(_control)

    app = Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/capabilities", capabilities_endpoint, methods=["GET"]),
        Route("/status", status_endpoint, methods=["GET"]),
        Route("/command", command_endpoint, methods=["POST"]),
    ])

    http_config = Config(app=app, host="0.0.0.0", port=AGENT_PORT, loop="asyncio")
    await Server(http_config).serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
