"""
Robot Agent: standalone service that manages one physical robot.

Each Robot Agent is an independent process that:
1. Runs an A2A server (a2a-sdk) on HTTP for task req/reply
2. Registers its A2A AgentCard to ADS (so planner can discover it)
3. Uses NATS only for fire-and-forget pub/sub of reasoning/state to gateway
4. Receives UDP video from Pi, serves WebSocket to frontend
5. Sends commands to Pi via HTTP POST /command
6. Runs CentralPiAgent + Detection Engine

Usage:
    python -m Command_Center.robot_agent \
        --robot-id crawler-alpha-patrol-004 \
        --pi-host 10.232.143.132 \
        --zone A --robot-type spider

Communication flow:
    Planner  --A2A JSON-RPC / HTTP-->  Robot Agent   (tasks, cancel)
    Robot Agent  --NATS pub/sub-->     Gateway        (reasoning steps, state deltas)
    Pi  --UDP-->  Robot Agent  --WebSocket-->  Browser   (video)
    Robot Agent  --HTTP POST-->  Pi  (commands)
"""

import argparse
import asyncio
import json
import logging
import os
import socket
import uuid
from typing import Optional

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore

from .config import (
    TRANSPORT_SERVER_ENDPOINT,
    DEFAULT_CAMERA_PORT,
    ADS_SERVER_ADDRESS,
    DEFAULT_MESSAGE_TRANSPORT,
    A2A_ENABLE_HTTP,
    A2A_ENABLE_NATS,
    A2A_ENABLE_SLIM,
    NATS_ENDPOINT,
    SLIM_ENDPOINT,
    SLIM_SHARED_SECRET,
)
from .infra.transport import NatsTransport
from .infra.robot_client import robot_client
from .infra.robot_registry import robot_registry, RegisteredRobot
from .pi_agents.base_agent import CentralPiAgent
from .pi_agents.brain import LLMClient
from .detection.engine import SearchMission, run_search_mission
from .detection.udp_receiver import UDPFrameReceiver
from .robot_a2a import a2a_http_port
from .robot_a2a.card import build_agent_card, TransportPlan
from .robot_a2a.executor import RobotAgentExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


# --- Streaming pub/sub topic convention -------------------------------------
#
# Reasoning steps and state deltas are fire-and-forget pub/sub from the
# Robot Agent to the gateway.  Not A2A — these are live UI events that
# can arrive outside the scope of any particular task.

STREAM_TOPIC_PREFIX = "agents.stream"


def reasoning_topic(robot_id: str) -> str:
    return f"{STREAM_TOPIC_PREFIX}.{robot_id}.reasoning"


def state_topic(robot_id: str) -> str:
    return f"{STREAM_TOPIC_PREFIX}.{robot_id}.state"


REASONING_TOPIC_WILDCARD = f"{STREAM_TOPIC_PREFIX}.*.reasoning"
STATE_TOPIC_WILDCARD = f"{STREAM_TOPIC_PREFIX}.*.state"


WS_PORT_BASE = 7000


def _ws_port_for(robot_id: str) -> int:
    """Deterministic WebSocket port per robot (unchanged)."""
    import hashlib
    h = int(hashlib.md5(robot_id.encode()).hexdigest()[:8], 16)
    return WS_PORT_BASE + h % 1000


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class RobotAgentService:
    """Standalone Robot Agent service for one physical robot.

    Hosts an A2A HTTP server plus ancillary NATS pub/sub and video
    forwarding.  Task dispatch is invoked by the A2A
    :class:`RobotAgentExecutor` via ``run_agent_task`` /
    ``run_direct_control`` / ``cancel_current_mission``.
    """

    def __init__(
        self,
        robot_id: str,
        pi_host: str,
        pi_port: int = 9004,
        camera_port: int = DEFAULT_CAMERA_PORT,
        zone: str = "",
        robot_type: str = "spider",
        a2a_host: str = "0.0.0.0",
        a2a_port: Optional[int] = None,
    ):
        self.robot_id = robot_id
        self.pi_host = pi_host
        self.pi_port = pi_port
        self.camera_port = camera_port
        self.zone = zone
        self.robot_type = robot_type
        self.ws_port = _ws_port_for(robot_id)
        self.a2a_host = a2a_host
        self.a2a_port = a2a_port if a2a_port is not None else a2a_http_port(robot_id)

        self._transport: Optional[NatsTransport] = None
        self._agent: Optional[CentralPiAgent] = None
        self._robot: Optional[RegisteredRobot] = None
        self._udp_receiver: Optional[UDPFrameReceiver] = None
        self._ws_clients: set = set()
        self._current_mission: Optional[SearchMission] = None
        self._app_session = None  # agntcy AppSession (set at startup)

    # ── A2A AgentCard (used both for server registration and ADS) ────────

    def _transport_plan(self) -> TransportPlan:
        """Assemble the TransportPlan declared by current env/config."""
        card_host = _get_local_ip() if self.a2a_host in ("0.0.0.0", "") else self.a2a_host
        return TransportPlan(
            preferred=DEFAULT_MESSAGE_TRANSPORT,
            enable_http=A2A_ENABLE_HTTP,
            enable_nats=A2A_ENABLE_NATS,
            enable_slim=A2A_ENABLE_SLIM,
            a2a_host=card_host,
            a2a_port=self.a2a_port,
            nats_endpoint=NATS_ENDPOINT,
            slim_endpoint=SLIM_ENDPOINT,
        )

    @property
    def agent_card(self):
        return build_agent_card(
            robot_id=self.robot_id,
            robot_type=self.robot_type,
            zone=self.zone,
            plan=self._transport_plan(),
        )

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self):
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

        await robot_registry._fetch_capabilities(self.robot_id, self.pi_host, self.pi_port)

        brain = LLMClient(robot_type=self.robot_type)
        self._agent = CentralPiAgent(self._robot, brain)
        logger.info(
            f"Robot Agent initialized: {self.robot_id} [{self.robot_type}] "
            f"zone={self.zone} pi={self.pi_host}:{self.pi_port}"
        )

        # NATS is only for streaming pub/sub (reasoning/state) — not A2A.
        try:
            self._transport = NatsTransport(endpoint=TRANSPORT_SERVER_ENDPOINT)
            await self._transport.connect()
            logger.info(f"NATS connected for stream pub/sub: {TRANSPORT_SERVER_ENDPOINT}")
        except Exception as e:
            logger.warning(f"NATS unreachable (stream pub/sub disabled): {e}")
            self._transport = None

        async def _publish_reasoning(robot_id: str, step: str):
            if self._transport and self._transport.is_connected:
                try:
                    msg = json.dumps({"robot_id": robot_id, "step": step}).encode()
                    await self._transport.publish(reasoning_topic(robot_id), msg)
                except Exception:
                    pass

        async def _publish_state(robot_id: str, state: dict):
            if self._transport and self._transport.is_connected:
                try:
                    payload = {"robot_id": robot_id, **state}
                    await self._transport.publish(
                        state_topic(robot_id), json.dumps(payload).encode()
                    )
                except Exception:
                    pass

        self._agent.on_reasoning = _publish_reasoning
        self._agent.on_state_update = _publish_state

        self._udp_receiver = UDPFrameReceiver(listen_port=0)
        await self._udp_receiver.start()

        local_ip = _get_local_ip()
        stream_result = await robot_client.start_video_stream(
            self.pi_host, self.camera_port, local_ip, self._udp_receiver.port, fps=10
        )
        logger.info(
            f"Video stream: Pi -> UDP port {self._udp_receiver.port} "
            f"({stream_result.get('status', '?')})"
        )

        await self._register_to_ads()

        ws_task = asyncio.create_task(self._run_ws_server())
        forward_task = asyncio.create_task(self._forward_frames_to_ws())
        health_task = asyncio.create_task(self._monitor_pi_health())
        logger.info(f"WebSocket video: ws://localhost:{self.ws_port}")

        try:
            await self._run_a2a_server()
        finally:
            ws_task.cancel()
            forward_task.cancel()
            health_task.cancel()
            if self._udp_receiver:
                self._udp_receiver.stop()
            try:
                await robot_client.stop_video_stream(self.pi_host, self.camera_port)
            except Exception:
                pass
            if self._transport and self._transport.is_connected:
                await self._transport.close()

    # ── A2A multi-transport server ───────────────────────────────────────

    async def _run_a2a_server(self):
        """Run the A2A server over all enabled transports (HTTP/NATS/SLIM).

        Uses agntcy-app-sdk's ``CardBuilder`` to expand
        ``card.additional_interfaces`` into an ``AppContainer`` per
        transport (matches v1_classification's transport-pluggable
        pattern).  The preferred transport is selected via
        ``DEFAULT_MESSAGE_TRANSPORT``.
        """
        from agntcy_app_sdk.factory import AgntcyFactory

        executor = RobotAgentExecutor(service=self)
        request_handler = DefaultRequestHandler(
            agent_executor=executor,
            task_store=InMemoryTaskStore(),
        )

        card = self.agent_card
        plan = self._transport_plan()

        factory = AgntcyFactory("multi_spider.robot_agent", enable_tracing=False)
        session = factory.create_app_session()
        self._app_session = session

        builder = session.add_a2a_card(card, request_handler).with_factory(factory)
        if plan.enable_slim:
            builder = builder.with_shared_secret(SLIM_SHARED_SECRET)

        interfaces_summary = [
            f"{iface.transport}@{iface.url}" for iface in (card.additional_interfaces or [])
        ]
        logger.info(
            f"A2A server starting — preferred={card.preferred_transport} "
            f"url={card.url} interfaces={interfaces_summary}"
        )

        try:
            await builder.start(keep_alive=True)
        finally:
            try:
                await session.stop_all_sessions()
            except Exception:
                logger.exception("Error stopping app sessions")

    # ── Task handlers (invoked by RobotAgentExecutor) ────────────────────

    async def run_agent_task(self, request: dict) -> dict:
        # Trigger mock_pi to play its recorded video from frame 0 (no-op on
        # real Pi — they don't have /stream/play).  Mock Pi reports the
        # video's length back; we use that to extend the mission observation
        # window so every recorded scenario runs to completion, independent
        # of whatever duration the prompt happened to specify.
        video_s = await robot_client.trigger_video_playback(self.pi_host, self.camera_port)
        if video_s and not request.get("duration_s"):
            # +2 s guard so YOLO still sees the final frames before mission ends.
            request = {**request, "duration_s": int(video_s) + 2}
            logger.info(
                f"[{self.robot_id}] mock-mode: extending mission to {request['duration_s']}s "
                f"to cover the full {video_s:.1f}s video"
            )

        from .pi_agents.base_agent import pi_agent_manager
        mgr_agent = pi_agent_manager.get_or_create(self._robot)
        if not mgr_agent.on_reasoning and self._agent and self._agent.on_reasoning:
            mgr_agent.on_reasoning = self._agent.on_reasoning

        report_all = request.get("report_all", False)
        vlm_describe_objects = request.get("vlm_describe_objects", False)
        vlm_describe_scene = request.get("vlm_describe_scene", False)
        alarm_on_detection = request.get("alarm_on_detection", False)

        detection_threshold = 0.35 if (report_all or vlm_describe_objects) else 0.7

        user_duration = request.get("duration_s", 0)
        if user_duration == 0 and alarm_on_detection:
            user_duration = 600

        mission = SearchMission(
            mission_id=request.get("mission_id", str(uuid.uuid4())[:8]),
            agent_id=self.robot_id,
            host=self.pi_host,
            camera_port=self.camera_port,
            target_type=request.get("target_type", "object"),
            target_name=request.get("target_name", ""),
            task_goal=request.get("task_goal", ""),
            report_all=report_all,
            vlm_verify=request.get("vlm_verify", False),
            vlm_describe_objects=vlm_describe_objects,
            vlm_describe_scene=vlm_describe_scene,
            alarm_on_detection=alarm_on_detection,
            duration_s=user_duration,
            threshold=detection_threshold,
        )
        self._current_mission = mission
        robot_registry.set_busy(self.robot_id, mission.mission_id)
        try:
            result = await run_search_mission(mission, existing_udp_receiver=self._udp_receiver)
        finally:
            robot_registry.clear_busy(self.robot_id)
            self._current_mission = None

        ok = result["status"] in ("completed", "timeout", "cancelled", "duration_reached")
        return {
            "status": "success" if ok else "error",
            "agent_id": self.robot_id,
            "zone": self.zone,
            "mission_result": result,
        }

    async def run_direct_control(self, request: dict) -> dict:
        # Trigger mock_pi playback for direct-control missions too.
        await robot_client.trigger_video_playback(self.pi_host, self.camera_port)

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

    async def cancel_current_mission(self) -> dict:
        if self._current_mission and not self._current_mission.is_cancelled:
            self._current_mission.cancel()
            logger.info(
                f"[{self.robot_id}] Mission {self._current_mission.mission_id} cancelled"
            )
        if self._agent:
            self._agent.stop_patrol()
        try:
            await robot_client.execute_action(
                self.pi_host, self.pi_port, "stop", robot_id=self.robot_id
            )
        except Exception as e:
            logger.warning(f"[{self.robot_id}] Pi stop failed: {e}")
        # Mock-only: tell mock_pi to rewind the video to frame 0 so the UI
        # stops showing stale footage from the cancelled run.  Real Pi has
        # no /stream/pause endpoint — 404 is silently ignored.
        await robot_client.pause_video_playback(self.pi_host, self.camera_port)
        return {"status": "cancelled", "agent_id": self.robot_id}

    # ── WebSocket server (unchanged) ─────────────────────────────────────

    async def _run_ws_server(self):
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed, frontend video disabled")
            return

        async def handler(websocket, path=None):
            self._ws_clients.add(websocket)
            logger.info(f"WebSocket client connected ({len(self._ws_clients)} total)")
            try:
                async for _ in websocket:
                    pass
            finally:
                self._ws_clients.discard(websocket)
                logger.info(f"WebSocket client disconnected ({len(self._ws_clients)} total)")

        server = await websockets.serve(handler, "0.0.0.0", self.ws_port)
        logger.info(f"WebSocket server started on port {self.ws_port}")
        await server.wait_closed()

    async def _forward_frames_to_ws(self):
        while True:
            frame = await self._udp_receiver.wait_for_frame(timeout=2.0)
            if frame is None or not self._ws_clients:
                continue
            disconnected = set()
            for ws in self._ws_clients.copy():
                try:
                    await ws.send(frame)
                except Exception:
                    disconnected.add(ws)
            self._ws_clients -= disconnected

    # ── Pi health monitor (unchanged) ────────────────────────────────────

    async def _monitor_pi_health(self):
        pi_online = True
        while True:
            await asyncio.sleep(15)
            try:
                health = await robot_client.health_check(self.pi_host, self.pi_port)
                if health and health.get("status") == "ok":
                    if not pi_online:
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

    # ── ADS registration ─────────────────────────────────────────────────

    async def _register_to_ads(self):
        try:
            from .infra.ads_registration import register_robot_agent_to_ads
            register_robot_agent_to_ads(
                robot_id=self.robot_id,
                robot_type=self.robot_type,
                zone=self.zone,
                pi_host=self.pi_host,
                pi_port=self.pi_port,
                camera_port=self.camera_port,
                a2a_host=_get_local_ip() if self.a2a_host in ("0.0.0.0", "") else self.a2a_host,
                a2a_port=self.a2a_port,
                ads_address=ADS_SERVER_ADDRESS,
                transport_plan=self._transport_plan(),
            )
            logger.info(f"Registered to ADS: {self.robot_id}")
        except Exception as e:
            logger.warning(f"ADS registration failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Robot Agent Service (A2A)")
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--pi-host", required=True)
    parser.add_argument("--pi-port", type=int, default=9004)
    parser.add_argument("--camera-port", type=int, default=DEFAULT_CAMERA_PORT)
    parser.add_argument("--zone", default="")
    parser.add_argument("--robot-type", default="spider")
    parser.add_argument("--a2a-host", default=os.getenv("A2A_HTTP_HOST", "0.0.0.0"))
    parser.add_argument(
        "--a2a-port",
        type=int,
        default=None,
        help="A2A HTTP port (defaults to deterministic hash of robot-id)",
    )
    args = parser.parse_args()

    service = RobotAgentService(
        robot_id=args.robot_id,
        pi_host=args.pi_host,
        pi_port=args.pi_port,
        camera_port=args.camera_port,
        zone=args.zone,
        robot_type=args.robot_type,
        a2a_host=args.a2a_host,
        a2a_port=args.a2a_port,
    )

    print("=" * 60)
    print("Robot Agent Service (A2A, multi-transport)")
    print(f"Robot:         {service.robot_id}")
    print(f"Type:          {service.robot_type}")
    print(f"Zone:          {service.zone}")
    print(f"Pi:            {service.pi_host}:{service.pi_port}")
    print(f"Camera:        {service.pi_host}:{service.camera_port}")
    print(f"Preferred:     {DEFAULT_MESSAGE_TRANSPORT}")
    print(
        f"Enabled:       "
        f"HTTP={A2A_ENABLE_HTTP} NATS={A2A_ENABLE_NATS} SLIM={A2A_ENABLE_SLIM}"
    )
    if A2A_ENABLE_HTTP:
        print(f"  HTTP:        http://{service.a2a_host}:{service.a2a_port}")
    if A2A_ENABLE_NATS:
        from .robot_a2a.card import nats_topic_for
        print(f"  NATS topic:  {nats_topic_for(service.robot_id)} @ {NATS_ENDPOINT}")
    if A2A_ENABLE_SLIM:
        from .robot_a2a.card import slim_identity_for
        print(f"  SLIM name:   {slim_identity_for(service.robot_id)} @ {SLIM_ENDPOINT}")
    print(f"WebSocket:     ws://0.0.0.0:{service.ws_port}")
    print(f"Stream NATS:   {TRANSPORT_SERVER_ENDPOINT}")
    print("=" * 60)

    try:
        asyncio.run(service.start())
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
