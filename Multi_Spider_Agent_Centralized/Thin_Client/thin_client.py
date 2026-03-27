"""
PiCrawler Thin Client — A2A server that executes hardware commands from Central.

No LLM, no decision-making. Just hardware execution via A2A protocol.
Central Command Center handles all intelligence (detection, LLM decisions, planning).

This server:
1. Runs A2A server on AGENT_PORT (default 9004)
2. Registers with ADS for Central to discover
3. Sends heartbeat to Central every 10s to stay online in registry
4. Executes hardware commands received via A2A message/send
"""

import asyncio
import json
import logging
import os
import time
from uuid import uuid4

import httpx
from uvicorn import Config, Server
from starlette.requests import Request
from starlette.responses import JSONResponse

from a2a.server.apps import A2AStarletteApplication
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import (
    Message, Role, Part, TextPart,
    InternalError,
)
from a2a.utils import new_task
from a2a.utils.errors import ServerError

# NATS transport (nats-py >= 2.6.0, Python >= 3.7)
_HAS_NATS = False
try:
    import nats
    _HAS_NATS = True
except ImportError:
    pass

from Thin_Client.config import (
    AGENT_ID, AGENT_PORT, SPEAKER_ENABLED,
    COMMAND_CENTER_URL, ADS_ADDRESS, OASF_ADDRESS,
    DEFAULT_MESSAGE_TRANSPORT, TRANSPORT_SERVER_ENDPOINT, BROADCAST_TOPIC,
    HEARTBEAT_INTERVAL,
)
from Thin_Client.capabilities import get_capabilities_json
from Thin_Client.hardware import CrawlerControl
from Thin_Client.card import AGENT_CARD, register_to_ads
from Thin_Client import speaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
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


# ── A2A Executor (thin client) ────────────────────────────

class ThinClientExecutor(AgentExecutor):
    """Routes A2A requests to hardware commands. No LLM, no missions."""

    def __init__(self, control: CrawlerControl):
        self.control = control
        self.action_map = _build_action_map(control)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context or not context.message:
            return

        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        try:
            metadata = context.message.metadata or {}
            task_type = metadata.get("task_type", "execute")

            if task_type == "execute":
                result = await self._execute_action(metadata)
            elif task_type == "read_sensor":
                result = await self._read_sensor(metadata)
            elif task_type == "status":
                result = self._get_status()
            elif task_type == "speak":
                result = await self._speak(metadata)
            else:
                result = {"error": f"Unknown task_type: {task_type}"}

            # Return result as JSON text in A2A response
            msg = Message(
                message_id=str(uuid4()),
                role=Role.agent,
                parts=[Part(TextPart(text=json.dumps(result)))],
            )
            await event_queue.enqueue_event(msg)

        except Exception as e:
            logger.error(f"Executor error: {e}")
            raise ServerError(error=InternalError()) from e

    async def _execute_action(self, metadata: dict) -> dict:
        """Execute a hardware action."""
        action = metadata.get("action", "stop")
        params = metadata.get("params", {})

        handler = self.action_map.get(action)
        if not handler:
            return {"status": "error", "error": f"Unknown action: {action}"}

        t0 = time.time()
        try:
            await asyncio.to_thread(handler, **params)
            duration_ms = int((time.time() - t0) * 1000)
            logger.info(f"Executed {action}({params}) in {duration_ms}ms")
            return {"status": "ok", "action": action, "duration_ms": duration_ms}
        except Exception as e:
            return {"status": "error", "action": action, "error": str(e)}

    async def _read_sensor(self, metadata: dict) -> dict:
        """Read a sensor."""
        sensor = metadata.get("sensor", "ultrasonic")
        if sensor == "ultrasonic":
            distance = await asyncio.to_thread(self.control.read_distance)
            return {"distance_cm": distance}
        return {"error": f"Unknown sensor: {sensor}"}

    def _get_status(self) -> dict:
        """Return robot status."""
        return {
            "agent_id": AGENT_ID,
            "standing": self.control._standing,
            "uptime_s": int(time.time() - _start_time),
        }

    async def _speak(self, metadata: dict) -> dict:
        """Text-to-speech."""
        text = metadata.get("text", "")
        if text and SPEAKER_ENABLED:
            await asyncio.to_thread(speaker.say, text)
        return {"status": "ok"}

    async def cancel(self, context, event_queue):
        pass


# ── HTTP Endpoints (alongside A2A) ────────────────────────

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


# ── Heartbeat to Command Center ───────────────────────────

async def _heartbeat_loop():
    """Send heartbeat to Command Center every 10s to stay online in registry."""
    await asyncio.sleep(3)  # Wait for server to start

    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                await client.post(
                    f"{COMMAND_CENTER_URL}/api/robots/{AGENT_ID}/heartbeat",
                    json={
                        "standing": _control._standing if _control else False,
                        "port": AGENT_PORT,
                        "camera_port": int(os.getenv("STREAM_PORT", "9000")),
                        "capabilities": get_capabilities_json(),
                    },
                )
            except Exception as e:
                logger.warning(f"Heartbeat failed: {e}")
            await asyncio.sleep(10)


# ── NATS Transport (direct nats-py, no agntcy-app-sdk) ────

_nats_nc = None  # global NATS connection
_nats_executor = None  # reference to executor for NATS handler


async def _nats_message_handler(msg):
    """Handle incoming A2A messages from NATS.

    Parses JSON-RPC request, routes to executor, sends reply.
    """
    try:
        data = json.loads(msg.data)
        params = data.get("params", {})
        message_data = params.get("message", {})
        metadata = message_data.get("metadata", {})
        parts = message_data.get("parts", [])

        # Extract prompt text
        prompt = ""
        for p in parts:
            if p.get("kind") == "text":
                prompt = p.get("text", "")
                break

        # Route to executor based on task_type
        task_type = metadata.get("task_type", "execute")

        if task_type == "execute":
            result = await _nats_executor._execute_action(metadata)
        elif task_type == "read_sensor":
            result = await _nats_executor._read_sensor(metadata)
        elif task_type == "status":
            result = _nats_executor._get_status()
        elif task_type == "speak":
            result = await _nats_executor._speak(metadata)
        else:
            result = {"error": f"Unknown task_type: {task_type}"}

        # Build A2A JSON-RPC response
        response = {
            "jsonrpc": "2.0",
            "id": data.get("id"),
            "result": {
                "parts": [{"kind": "text", "text": json.dumps(result)}],
            },
        }

        # Reply
        if msg.reply:
            await _nats_nc.publish(msg.reply, json.dumps(response).encode())

    except Exception as e:
        logger.error(f"NATS handler error: {e}")
        if msg.reply and _nats_nc:
            error_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(e)},
            }
            await _nats_nc.publish(msg.reply, json.dumps(error_resp).encode())


async def run_nats_transport(executor: ThinClientExecutor):
    """Connect to NATS and subscribe to agent topics.

    Uses nats-py directly (Python >= 3.7, no agntcy-app-sdk needed).
    Subscribes to:
      - agents.direct.{AGENT_ID}  (personal messages from Command Center)
      - agents.broadcast           (broadcast messages to all agents)
    """
    global _nats_nc, _nats_executor

    if not _HAS_NATS:
        logger.info("nats-py not installed, skipping NATS transport (HTTP-only mode)")
        return

    _nats_executor = executor
    personal_topic = f"agents.direct.{AGENT_ID}"

    try:
        _nats_nc = await nats.connect(TRANSPORT_SERVER_ENDPOINT)
        await _nats_nc.subscribe(personal_topic, cb=_nats_message_handler)
        await _nats_nc.subscribe(BROADCAST_TOPIC, cb=_nats_message_handler)
        logger.info(
            f"NATS connected: {TRANSPORT_SERVER_ENDPOINT} "
            f"topics: [{personal_topic}, {BROADCAST_TOPIC}]"
        )

        # Keep alive until disconnected
        while _nats_nc.is_connected:
            await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"NATS transport error: {e}")
        logger.info("Continuing with HTTP-only A2A mode")


# ── Server startup ─────────────────────────────────────────

async def main():
    global _control

    transport_info = DEFAULT_MESSAGE_TRANSPORT if _HAS_NATS else "HTTP-only"
    print("=" * 50)
    print(f"PiCrawler Thin Client (A2A)")
    print(f"Robot:     {AGENT_ID}")
    print(f"A2A port:  {AGENT_PORT}")
    print(f"Central:   {COMMAND_CENTER_URL}")
    print(f"Transport: {transport_info}")
    if _HAS_NATS:
        print(f"Endpoint:  {TRANSPORT_SERVER_ENDPOINT}")
    print("=" * 50)

    _control = CrawlerControl(speed=int(os.getenv("PATROL_SPEED", "50")))

    executor = ThinClientExecutor(_control)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    a2a_server = A2AStarletteApplication(
        agent_card=AGENT_CARD,
        http_handler=request_handler,
    )

    app = a2a_server.build()
    # Add non-A2A endpoints
    app.add_route("/health", health, methods=["GET"])
    app.add_route("/capabilities", capabilities_endpoint, methods=["GET"])
    app.add_route("/status", status_endpoint, methods=["GET"])

    # ADS self-registration (for Central to discover via ADS)
    if ADS_ADDRESS and OASF_ADDRESS:
        register_to_ads(ADS_ADDRESS, OASF_ADDRESS)

    # Start all tasks concurrently
    tasks = []

    # HTTP A2A server (always available)
    http_config = Config(app=app, host="0.0.0.0", port=AGENT_PORT, loop="asyncio")
    tasks.append(asyncio.create_task(Server(http_config).serve()))

    # NATS transport (if nats-py installed)
    if _HAS_NATS and DEFAULT_MESSAGE_TRANSPORT.upper() == "NATS":
        tasks.append(asyncio.create_task(run_nats_transport(executor)))

    # Heartbeat to Command Center
    tasks.append(asyncio.create_task(_heartbeat_loop()))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
