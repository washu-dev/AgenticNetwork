"""
Robot Agent: standalone service that manages one physical robot.

Each Robot Agent is an independent process that:
1. Registers itself to ADS
2. Subscribes to NATS for tasks from Planner Agent
3. Receives UDP video from Pi, serves WebSocket to frontend
4. Sends commands to Pi via HTTP POST /command
5. Runs CentralPiAgent + Detection Engine

Usage:
    python -m Command_Center.robot_agent \
        --robot-id crawler-alpha-patrol-004 \
        --pi-host 10.232.143.132 \
        --zone A --robot-type spider

Video flow:  Pi --UDP--> Robot Agent --WebSocket--> Browser
Command flow: Robot Agent --HTTP POST--> Pi
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import socket
import uuid

from .config import (
    DEFAULT_MESSAGE_TRANSPORT,
    TRANSPORT_SERVER_ENDPOINT,
    DEFAULT_CAMERA_PORT,
    ADS_SERVER_ADDRESS,
)
from .infra.transport import NatsTransport
from .infra.robot_client import robot_client
from .infra.robot_registry import robot_registry, RegisteredRobot
from .pi_agents.base_agent import CentralPiAgent, PiAgentManager
from .pi_agents.brain import LLMClient
from .detection.engine import SearchMission, run_search_mission
from .detection.udp_receiver import UDPFrameReceiver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

TASK_TOPIC_PREFIX = "robot_agent"
WS_PORT_BASE = 7000  # WebSocket port = WS_PORT_BASE + last digits of robot_id hash


def task_topic(robot_id: str) -> str:
    """NATS topic for receiving tasks from Planner."""
    return f"{TASK_TOPIC_PREFIX}.{robot_id}.task"


def cancel_topic(robot_id: str) -> str:
    """NATS topic for cancel commands."""
    return f"{TASK_TOPIC_PREFIX}.{robot_id}.cancel"


def ping_topic(robot_id: str) -> str:
    """NATS topic for ping/health check."""
    return f"{TASK_TOPIC_PREFIX}.{robot_id}.ping"


def _ws_port_for(robot_id: str) -> int:
    """Deterministic WebSocket port per robot (consistent across processes)."""
    import hashlib
    h = int(hashlib.md5(robot_id.encode()).hexdigest()[:8], 16)
    return WS_PORT_BASE + h % 1000


def _get_local_ip() -> str:
    """Get this machine's LAN IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class RobotAgentService:
    """Standalone Robot Agent service for one physical robot."""

    def __init__(
        self,
        robot_id: str,
        pi_host: str,
        pi_port: int = 9004,
        camera_port: int = DEFAULT_CAMERA_PORT,
        zone: str = "",
        robot_type: str = "spider",
    ):
        self.robot_id = robot_id
        self.pi_host = pi_host
        self.pi_port = pi_port
        self.camera_port = camera_port
        self.zone = zone
        self.robot_type = robot_type
        self.ws_port = _ws_port_for(robot_id)

        self._transport: NatsTransport = None
        self._agent: CentralPiAgent = None
        self._robot: RegisteredRobot = None
        self._udp_receiver: UDPFrameReceiver = None
        self._ws_clients: set = set()

    async def start(self):
        """Initialize everything and start listening."""
        # Register robot in local registry
        self._robot = RegisteredRobot(
            robot_id=self.robot_id,
            host=self.pi_host,
            port=self.pi_port,
            camera_port=self.camera_port,
            robot_type=self.robot_type,
            zone=self.zone,
            status="online",
        )
        robot_registry._robots[self.robot_id] = self._robot

        # Fetch capabilities from Pi
        await robot_registry._fetch_capabilities(
            self.robot_id, self.pi_host, self.pi_port
        )

        # Create CentralPiAgent
        brain = LLMClient(robot_type=self.robot_type)
        self._agent = CentralPiAgent(self._robot, brain)
        logger.info(
            f"Robot Agent initialized: {self.robot_id} [{self.robot_type}] "
            f"zone={self.zone} pi={self.pi_host}:{self.pi_port}"
        )

        # Start UDP receiver for video
        self._udp_receiver = UDPFrameReceiver(listen_port=0)
        await self._udp_receiver.start()

        # Tell Pi to start pushing UDP video
        local_ip = _get_local_ip()
        stream_result = await robot_client.start_video_stream(
            self.pi_host, self.camera_port, local_ip, self._udp_receiver.port, fps=10
        )
        logger.info(f"Video stream: Pi -> UDP port {self._udp_receiver.port} ({stream_result.get('status', '?')})")

        # Connect to NATS
        self._transport = NatsTransport(endpoint=TRANSPORT_SERVER_ENDPOINT)
        await self._transport.connect()

        # Subscribe to task, cancel, and ping topics
        topic = task_topic(self.robot_id)
        cancel = cancel_topic(self.robot_id)
        ping = ping_topic(self.robot_id)
        await self._transport.subscribe([topic], self._handle_task)
        await self._transport.subscribe([cancel], self._handle_cancel)
        await self._transport.subscribe([ping], self._handle_ping)
        logger.info(f"Listening for tasks on: {topic}")

        # Register to ADS
        await self._register_to_ads()

        # Start WebSocket server for frontend video
        ws_task = asyncio.create_task(self._run_ws_server())

        # Start frame forwarding (UDP → WebSocket)
        forward_task = asyncio.create_task(self._forward_frames_to_ws())

        logger.info(f"WebSocket video: ws://localhost:{self.ws_port}")

        # Start Pi health monitor (auto-reconnect video stream)
        health_task = asyncio.create_task(self._monitor_pi_health())

        # Keep alive
        try:
            while self._transport.is_connected:
                await asyncio.sleep(1)
        finally:
            self._udp_receiver.stop()
            await robot_client.stop_video_stream(self.pi_host, self.camera_port)
            ws_task.cancel()
            forward_task.cancel()
            health_task.cancel()

    # ── WebSocket server ─────────────────────────────────

    async def _run_ws_server(self):
        """Run WebSocket server for frontend video streaming."""
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed, frontend video disabled. pip install websockets")
            return

        async def handler(websocket, path=None):
            self._ws_clients.add(websocket)
            logger.info(f"WebSocket client connected ({len(self._ws_clients)} total)")
            try:
                async for _ in websocket:
                    pass  # keep connection alive, ignore incoming messages
            finally:
                self._ws_clients.discard(websocket)
                logger.info(f"WebSocket client disconnected ({len(self._ws_clients)} total)")

        server = await websockets.serve(handler, "0.0.0.0", self.ws_port)
        logger.info(f"WebSocket server started on port {self.ws_port}")
        await server.wait_closed()

    async def _forward_frames_to_ws(self):
        """Forward UDP frames to all WebSocket clients."""
        while True:
            frame = await self._udp_receiver.wait_for_frame(timeout=2.0)
            if frame is None or not self._ws_clients:
                continue
            # Send JPEG as binary to all connected clients
            disconnected = set()
            for ws in self._ws_clients.copy():
                try:
                    await ws.send(frame)
                except Exception:
                    disconnected.add(ws)
            self._ws_clients -= disconnected

    # ── Pi health monitor ────────────────────────────────

    async def _monitor_pi_health(self):
        """Periodically check Pi health. Reconnect video stream if Pi was offline and comes back."""
        pi_online = True
        while True:
            await asyncio.sleep(15)
            try:
                health = await robot_client.health_check(self.pi_host, self.pi_port)
                if health and health.get("status") == "ok":
                    if not pi_online:
                        # Pi came back online — reconnect
                        logger.info(f"[{self.robot_id}] Pi reconnected, restarting video stream")
                        await robot_registry._fetch_capabilities(
                            self.robot_id, self.pi_host, self.pi_port
                        )
                        local_ip = _get_local_ip()
                        await robot_client.start_video_stream(
                            self.pi_host, self.camera_port,
                            local_ip, self._udp_receiver.port, fps=10
                        )
                        pi_online = True
                else:
                    if pi_online:
                        logger.warning(f"[{self.robot_id}] Pi offline")
                    pi_online = False
            except Exception:
                if pi_online:
                    logger.warning(f"[{self.robot_id}] Pi offline")
                pi_online = False

    # ── NATS task handlers ───────────────────────────────

    _current_mission: SearchMission = None

    async def _handle_ping(self, payload: bytes, reply_fn):
        """Lightweight ping — no side effects."""
        await reply_fn(json.dumps({"status": "ok", "agent_id": self.robot_id}).encode())

    async def _handle_cancel(self, payload: bytes, reply_fn):
        """Handle cancel command — stop current patrol/mission and halt Pi immediately."""
        logger.info(f"[{self.robot_id}] Cancel received")
        if self._current_mission and not self._current_mission.is_cancelled:
            self._current_mission.cancel()
            logger.info(f"[{self.robot_id}] Mission {self._current_mission.mission_id} cancelled")
        if self._agent:
            self._agent.stop_patrol()
        # Send stop to Pi immediately to interrupt current physical movement
        await robot_client.execute_action(
            self.pi_host, self.pi_port, "stop", robot_id=self.robot_id
        )
        await reply_fn(json.dumps({"status": "cancelled", "agent_id": self.robot_id}).encode())

    async def _handle_task(self, payload: bytes, reply_fn):
        """Handle incoming task request from Planner via NATS."""
        try:
            request = json.loads(payload)
            task_type = request.get("task_type", "direct_control")
            logger.info(f"[{self.robot_id}] Received task: {task_type}")

            if task_type == "agent":
                result = await self._handle_agent_task(request)
            elif task_type == "direct_control":
                result = await self._handle_direct_control(request)
            else:
                result = {"status": "error", "error": f"Unknown task_type: {task_type}"}

            await reply_fn(json.dumps(result).encode())
        except Exception as e:
            logger.error(f"[{self.robot_id}] Task handler error: {e}")
            await reply_fn(json.dumps({"status": "error", "error": str(e)}).encode())

    async def _handle_agent_task(self, request: dict) -> dict:
        mission = SearchMission(
            mission_id=request.get("mission_id", str(uuid.uuid4())[:8]),
            agent_id=self.robot_id,
            host=self.pi_host,
            camera_port=self.camera_port,
            target_type=request.get("target_type", "object"),
            target_name=request.get("target_name", ""),
            task_goal=request.get("task_goal", ""),
            search_pattern=request.get("search_pattern", "patrol"),
            report_all=request.get("report_all", False),
            duration_s=request.get("duration_s", 0),
        )
        self._current_mission = mission
        robot_registry.set_busy(self.robot_id, mission.mission_id)
        result = await run_search_mission(mission, existing_udp_receiver=self._udp_receiver)
        robot_registry.clear_busy(self.robot_id)
        self._current_mission = None
        return {
            "status": "success" if result["status"] in ("completed", "timeout", "cancelled") else "error",
            "agent_id": self.robot_id,
            "zone": self.zone,
            "mission_result": result,
        }

    async def _handle_direct_control(self, request: dict) -> dict:
        prompt = request.get("prompt", "")
        actions = request.get("actions", [])
        if actions:
            result = await self._agent.execute_steps([{"action": a} for a in actions])
        elif prompt:
            result = await self._agent.execute_steps_from_prompt(prompt)
        else:
            result = {"mission_status": "error", "summary": "No prompt or actions provided"}
        return {
            "status": "success" if result.get("mission_status") in ("completed", "partial") else "error",
            "agent_id": self.robot_id,
            "execution_result": result,
        }

    async def _register_to_ads(self):
        try:
            from .infra.ads_registration import register_robot_agent_to_ads
            register_robot_agent_to_ads(
                robot_id=self.robot_id,
                robot_type=self.robot_type,
                zone=self.zone,
                host=self.pi_host,
                port=self.pi_port,
                ads_address=ADS_SERVER_ADDRESS,
            )
            logger.info(f"Registered to ADS: {self.robot_id}")
        except Exception as e:
            logger.warning(f"ADS registration failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Robot Agent Service")
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--pi-host", required=True)
    parser.add_argument("--pi-port", type=int, default=9004)
    parser.add_argument("--camera-port", type=int, default=DEFAULT_CAMERA_PORT)
    parser.add_argument("--zone", default="")
    parser.add_argument("--robot-type", default="spider")
    args = parser.parse_args()

    service = RobotAgentService(
        robot_id=args.robot_id,
        pi_host=args.pi_host,
        pi_port=args.pi_port,
        camera_port=args.camera_port,
        zone=args.zone,
        robot_type=args.robot_type,
    )

    ws_port = _ws_port_for(args.robot_id)
    print("=" * 50)
    print(f"Robot Agent Service")
    print(f"Robot:     {args.robot_id}")
    print(f"Type:      {args.robot_type}")
    print(f"Zone:      {args.zone}")
    print(f"Pi:        {args.pi_host}:{args.pi_port}")
    print(f"Camera:    {args.pi_host}:{args.camera_port}")
    print(f"NATS:      {TRANSPORT_SERVER_ENDPOINT}")
    print(f"WebSocket: ws://0.0.0.0:{ws_port}")
    print(f"Topic:     {task_topic(args.robot_id)}")
    print("=" * 50)

    try:
        asyncio.run(service.start())
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
