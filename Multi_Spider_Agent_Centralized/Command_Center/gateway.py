import asyncio
import base64
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request as FastAPIRequest, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from .detection.engine import SearchMission, run_search_mission, get_active_missions, has_active_mission, wait_for_new_detections, cancel_mission, cancel_all_missions
from .infra.robot_registry import robot_registry
from .infra.robot_client import robot_client
from .detection.face_registry import face_registry
from .config import create_vision_llm
from .planner.graph import run_planner, planner_graph
from .detection.yolo_detector import detect_frame

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"

# ---------------------------------------------------------------------------
# Conversation context: remember recent tasks for "do that again" etc.
# ---------------------------------------------------------------------------
_conversation_history: list[dict] = []  # [{prompt, result, timestamp}]
MAX_HISTORY = 20

app = FastAPI(title="PiCrawler Command Center", version="1.0.0")

_background_tasks: list[asyncio.Task] = []


@app.on_event("startup")
async def startup_event():
    """Run initial ADS discovery, preload models, connect transport, and start background cleanup."""
    # Pre-load YOLO model so first detection is instant
    from .detection.yolo_detector import preload_model
    await asyncio.to_thread(preload_model)
    # Connect NATS/SLIM transport for A2A messaging
    from .infra.shared import ensure_connected
    try:
        transport = await ensure_connected()
        logger.info(f"Transport connected: {transport.__class__.__name__}")
    except Exception as e:
        logger.warning(f"Transport connection failed (will use HTTP fallback): {e}")
    # Subscribe to robot reasoning steps via NATS
    try:
        from .robot_agent import REASONING_TOPIC_WILDCARD
        transport = await ensure_connected()

        async def _on_robot_reasoning(payload: bytes, reply_fn):
            data = json.loads(payload)
            rid = data.get("robot_id", "")
            step = data.get("step", "")
            if rid and step:
                # Scope the reasoning to the currently-active task only.
                # Messages arriving after the task ends (or before the next one
                # starts) have no owning task and are dropped — preventing
                # cross-task pollution of the robot reasoning panels.
                if _current_task_id:
                    _live_robot_reasoning.setdefault(_current_task_id, {}).setdefault(rid, []).append(step)
                    if step.startswith("Intent:"):
                        _emit_comm_event(_current_task_id, rid, rid, "think", step[7:].strip())
                    elif step.startswith("Tool:"):
                        _emit_comm_event(_current_task_id, rid, "hardware", "execute", step[5:].strip())
                    elif step.startswith("Auto:"):
                        _emit_comm_event(_current_task_id, rid, "hardware", "auto", step[5:].strip())
                    elif step.startswith("VLM:") or step.startswith("VLM "):
                        # Vision-LLM is a robot-side reasoning event
                        _emit_comm_event(_current_task_id, rid, rid, "think", step.strip())
                    elif step.startswith("Result:"):
                        # Detection results are observations, not task completion
                        if "detected" in step or "Mission finished" in step:
                            _emit_comm_event(_current_task_id, rid, "planner", "observe", step[7:].strip())
                        else:
                            _emit_comm_event(_current_task_id, rid, "planner", "result", step[7:].strip())

        await transport.subscribe([REASONING_TOPIC_WILDCARD], _on_robot_reasoning)
        logger.info(f"Subscribed to robot reasoning: {REASONING_TOPIC_WILDCARD}")
    except Exception as e:
        logger.warning(f"Robot reasoning subscription failed: {e}")

    # Subscribe to Robot Agent state deltas (heading offset, etc.) so the
    # gateway's registry mirrors live robot state published from the RA
    # LOCALIZATION: robot-state subscriber only carried turn_offset_deg —
    # disabled along with the rest of localization.
    # try:
    #     from .robot_agent import STATE_TOPIC_WILDCARD
    #     transport = await ensure_connected()
    #
    #     async def _on_robot_state(payload: bytes, reply_fn):
    #         try:
    #             data = json.loads(payload)
    #             rid = data.get("robot_id", "")
    #             if not rid:
    #                 return
    #             robot = robot_registry.get_robot(rid)
    #             if not robot:
    #                 return
    #             if "turn_offset_deg" in data:
    #                 robot.turn_offset_deg = float(data["turn_offset_deg"])
    #         except Exception as e:
    #             logger.debug(f"state update parse failed: {e}")
    #
    #     await transport.subscribe([STATE_TOPIC_WILDCARD], _on_robot_state)
    #     logger.info(f"Subscribed to robot state: {STATE_TOPIC_WILDCARD}")
    # except Exception as e:
    #     logger.warning(f"Robot state subscription failed: {e}")

    # Initial discovery: populate registry from ADS/static config
    _background_tasks.append(asyncio.create_task(_initial_discovery()))
    # Periodic health check: actively check robot health + re-discover from ADS
    _background_tasks.append(asyncio.create_task(_periodic_health_check_loop()))


@app.on_event("shutdown")
async def shutdown_event():
    """Cancel active missions, close transports, and stop background tasks."""
    cancel_all_missions()
    from .infra.shared import get_transport
    try:
        transport = get_transport()
        if transport.is_connected:
            await transport.close()
    except Exception:
        pass
    try:
        from .planner.a2a_client import close_a2a_client
        await close_a2a_client()
    except Exception:
        pass
    for task in _background_tasks:
        task.cancel()


async def _initial_discovery():
    """Discover robots via ADS/static on startup to pre-populate registry."""
    from .infra.discovery import AgentDiscovery
    try:
        discovery = AgentDiscovery()
        agents = await discovery.get_online_agents()
        for agent_info in agents:
            robot_registry.register_from_discovery(agent_info)
        if agents:
            logger.info(f"Initial discovery: {len(agents)} robot(s) found")
        else:
            logger.info("Initial discovery: no robots found (will retry when tasks arrive)")
    except Exception as e:
        logger.warning(f"Initial discovery failed: {e}")


async def _periodic_health_check_loop():
    """Periodically health-check all robots and re-discover from ADS.

    Replaces heartbeat-based online detection. Central actively checks
    each robot's /health endpoint every HEALTH_CHECK_INTERVAL seconds.
    Also re-runs ADS discovery to pick up newly registered robots.
    """
    from .config import HEALTH_CHECK_INTERVAL
    from .infra.discovery import AgentDiscovery
    import time

    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        try:
            # Re-discover from ADS (picks up new robots)
            discovery = AgentDiscovery()
            agents = await discovery.get_online_agents()

            # Update registry with discovered agents
            seen_ids = set()
            for agent_info in agents:
                robot_registry.register_from_discovery(agent_info)
                seen_ids.add(agent_info.get("id", agent_info.get("agent_id", "")))

            # Mark robots offline if not found in discovery
            for robot in robot_registry.get_all_robots():
                if robot.robot_id not in seen_ids and robot.status == "online":
                    robot_registry.mark_offline(robot.robot_id)

        except Exception as e:
            logger.warning(f"Periodic health check failed: {e}")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    prompt: str
    target_type: Optional[str] = None
    strategy: Optional[str] = Field(default=None, description="single/parallel/sequential (None = auto)")
    agents: Optional[list[str]] = Field(default=None, description="Specific agent IDs to use")


class AgentResult(BaseModel):
    agent_id: str = ""
    status: str = ""
    response: Optional[str] = None
    error: Optional[str] = None
    structured: Optional[dict] = None


class SearchResponse(BaseModel):
    task_id: str
    status: str
    agents_used: list[str] = []
    results: list[AgentResult] = []
    summary: str = ""
    error: Optional[str] = None
    reasoning: list[str] = []  # LLM reasoning steps (for frontend display)
    server_start_ts: float = 0.0  # Unix seconds; frontend uses for topology event filtering


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def root():
    """Serve the frontend UI."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/flowchart")
async def flowchart_page():
    """Serve the Agent Communication flow chart page."""
    return FileResponse(FRONTEND_DIR / "flowchart.html")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "command-center"}


# ---------------------------------------------------------------------------
# Robot Registry endpoints
# ---------------------------------------------------------------------------


@app.post("/api/robots/register")
async def register_robot(request: FastAPIRequest):
    """Register a new robot. Called by Pi on startup with full capabilities."""
    data = await request.json()
    if not data.get("host") and request.client:
        data["host"] = request.client.host
    result = await robot_registry.register(data)
    return result


@app.post("/api/robots/{robot_id}/heartbeat")
async def robot_heartbeat(robot_id: str, request: FastAPIRequest):
    """Lightweight heartbeat from a robot. Called every 10s by Pi.

    Only carries state (standing). Capabilities are sent once via POST /api/robots/register.
    If the robot is not yet registered, auto-register with capabilities fetched from the robot.
    """
    data = await request.json()
    result = await robot_registry.heartbeat(robot_id, data)
    if result.get("error") == "robot not registered":
        # Auto-register: Pi sent heartbeat before register call arrived
        host = request.client.host if request.client else ""
        port = data.get("port", 9004)
        caps = {}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://{host}:{port}/capabilities")
                if resp.status_code == 200:
                    caps = resp.json()
        except Exception:
            pass
        reg_data = {
            "robot_id": robot_id,
            "host": host,
            "port": port,
            "camera_port": data.get("camera_port", 9000),
            "capabilities": caps,
        }
        result = await robot_registry.register(reg_data)
    return result


@app.post("/api/robots/discover")
async def trigger_discovery():
    """Manually trigger ADS/static discovery to refresh robot registry."""
    from .infra.discovery import AgentDiscovery
    discovery = AgentDiscovery()
    agents = await discovery.get_online_agents()
    for agent_info in agents:
        robot_registry.register_from_discovery(agent_info)
    return {
        "discovered": len(agents),
        "agents": [a["id"] for a in agents],
    }


@app.delete("/api/robots/{robot_id}")
async def unregister_robot(robot_id: str):
    """Unregister a robot. Called by Pi on graceful shutdown."""
    result = await robot_registry.unregister(robot_id)
    return result


@app.get("/api/robots")
async def list_robots():
    """List all robots and their online status."""
    all_robots = robot_registry.get_all_robots()
    online = robot_registry.get_online_robots()

    return {
        "agents": [r.to_dict() for r in all_robots],
        "online_count": len(online),
        "total_count": len(all_robots),
    }


@app.get("/api/agents")
async def list_agents():
    """List all robots (backward-compatible alias for /api/robots)."""
    return await list_robots()


# LOCALIZATION: /api/positions, /api/map_meta, /api/tag_positions —
# disabled along with the rest of localization.
# @app.get("/api/positions")
# async def robot_positions():
#     """Lightweight endpoint: just positions for map overlay (safe to poll)."""
#     online = robot_registry.get_online_robots()
#     return {
#         "positions": [
#             {
#                 "robot_id": r.robot_id,
#                 "zone": r.zone,
#                 "position": list(r.position) if r.position else None,
#                 "last_tag_id": r.last_tag_id,
#                 "updated_at": r.position_updated_at,
#                 "turn_offset_deg": r.turn_offset_deg,
#                 "busy": r.busy,
#             }
#             for r in online
#         ],
#     }
#
#
# @app.get("/api/map_meta")
# async def map_meta():
#     """Return map metadata (for frontend to translate world coords ↔ pixels)."""
#     import json as _json
#     from pathlib import Path
#     meta_path = Path(__file__).resolve().parent / "frontend" / "map_meta.json"
#     try:
#         with open(meta_path) as f:
#             return _json.load(f)
#     except FileNotFoundError:
#         return {"error": "map_meta.json not found"}
#
#
# @app.get("/api/tag_positions")
# async def tag_positions():
#     """Expose configured AprilTag positions to the frontend."""
#     from .detection.apriltag_detector import _tag_positions, load_tag_positions
#     return {"tags": load_tag_positions()}


@app.get("/api/latest_detections")
async def latest_detections():
    """Compact snapshot of what each robot is currently detecting, for map overlay.

    Returns one entry per robot that has any cached detections, listing the
    unique target labels currently in view (deduplicated).
    """
    from .detection.engine import _latest_detections
    summary = {}
    for agent_id, data in _latest_detections.items():
        dets = data.get("detections", []) if isinstance(data, dict) else []
        # Keep only target-like detections (is_target == True if present, else keep all)
        target_labels = []
        seen = set()
        for d in dets:
            if d.get("is_target") is False:
                continue
            label = d.get("label", "unknown")
            if label in seen:
                continue
            seen.add(label)
            target_labels.append(label)
        if target_labels:
            summary[agent_id] = {
                "agent_id": agent_id,
                "labels": target_labels,
                "count": len(target_labels),
            }
    return {"detections": summary}


# ---------------------------------------------------------------------------
# Debug endpoints — let you simulate tag sightings, target detections, and
# support-dispatch decisions without physical AprilTags or live robots.
# Safe to leave enabled in dev; consider removing or gating in production.
# ---------------------------------------------------------------------------


# LOCALIZATION: /api/debug/simulate_tag — disabled.
# @app.post("/api/debug/simulate_tag")
# async def debug_simulate_tag(data: dict):
#     """Simulate a robot seeing an AprilTag, updating its position accordingly."""
#     import time
#     from .detection.apriltag_detector import get_tag_info
#     from .infra.position_tracker import update_robot_position
#
#     robot_id = data.get("robot_id")
#     tag_id = data.get("tag_id")
#     if not robot_id or tag_id is None:
#         return {"status": "error", "error": "robot_id and tag_id required"}
#
#     info = get_tag_info(int(tag_id))
#     if not info or not info.get("pos"):
#         return {"status": "error", "error": f"tag #{tag_id} not in tag_positions.json"}
#
#     robot = robot_registry.get_robot(robot_id)
#     if robot is None:
#         return {"status": "error", "error": f"robot {robot_id} not registered"}
#
#     robot.last_heartbeat = time.time()
#     robot.status = "online"
#     update_robot_position(robot_id, tag_id=int(tag_id), pos=tuple(info["pos"]), zone=info.get("zone"))
#     return {
#         "status": "ok",
#         "robot_id": robot_id,
#         "tag_id": int(tag_id),
#         "position": list(info["pos"]),
#         "zone": info.get("zone"),
#     }


@app.post("/api/debug/fake_target")
async def debug_fake_target(data: dict):
    """Inject a fake target detection on a robot, visible via /api/latest_detections.

    Body: {"robot_id": "...", "label": "tank"}  or  {"robot_id": "...", "labels": ["tank", "person"]}
    """
    from .detection.engine import _latest_detections
    robot_id = data.get("robot_id")
    if not robot_id:
        return {"status": "error", "error": "robot_id required"}

    labels = data.get("labels") or ([data.get("label")] if data.get("label") else [])
    if not labels:
        return {"status": "error", "error": "label or labels required"}

    detections = [{"label": lab, "confidence": 0.9, "is_target": True} for lab in labels]
    _latest_detections[robot_id] = {
        "detections": detections,
        "image_width": 640,
        "image_height": 480,
    }
    return {"status": "ok", "robot_id": robot_id, "labels": labels}


@app.post("/api/debug/clear_target")
async def debug_clear_target(data: dict):
    """Remove the fake detection for one (or all) robots.

    Body: {"robot_id": "..."}  — omit robot_id to clear everything.
    """
    from .detection.engine import _latest_detections
    robot_id = data.get("robot_id") if isinstance(data, dict) else None
    if robot_id:
        _latest_detections.pop(robot_id, None)
        return {"status": "ok", "cleared": [robot_id]}
    cleared = list(_latest_detections.keys())
    _latest_detections.clear()
    return {"status": "ok", "cleared": cleared}


# LOCALIZATION: /api/debug/nearest_support — disabled.
# @app.get("/api/debug/nearest_support")
# async def debug_nearest_support(finder_robot_id: str):
#     from .infra.position_tracker import find_nearest_robot, are_adjacent, get_position
#     finder_pos = get_position(finder_robot_id)
#     if finder_pos is None:
#         return {"status": "error", "error": f"{finder_robot_id} has no known position"}
#
#     candidates = []
#     finder_robot = robot_registry.get_robot(finder_robot_id)
#     for r in robot_registry.get_online_robots():
#         if r.robot_id == finder_robot_id or r.position is None:
#             continue
#         from .infra.position_tracker import euclidean
#         dist = euclidean(finder_pos, r.position)
#         adj = True
#         if finder_robot and finder_robot.zone and r.zone:
#             adj = are_adjacent(r.zone, finder_robot.zone)
#         candidates.append({
#             "robot_id": r.robot_id, "zone": r.zone,
#             "position": list(r.position), "distance_m": round(dist, 3),
#             "busy": r.busy, "zone_adjacent_to_finder": adj,
#         })
#     candidates.sort(key=lambda c: c["distance_m"])
#     nearest = find_nearest_robot(finder_pos, exclude_ids=[finder_robot_id], require_idle=True)
#     return {
#         "status": "ok",
#         "finder": {"robot_id": finder_robot_id, "position": list(finder_pos), "zone": finder_robot.zone if finder_robot else None},
#         "chosen": {"robot_id": nearest["robot_id"], "distance_m": round(nearest["distance"], 3)} if nearest else None,
#         "all_candidates": candidates,
#     }


@app.get("/api/agents/{agent_id}/health")
async def agent_health(agent_id: str):
    """Health check a specific robot."""
    robot = robot_registry.get_robot(agent_id)
    if not robot:
        return {"error": f"Unknown robot: {agent_id}", "status": "not_found"}

    health_result = await robot_client.health_check(robot.host, robot.port)
    if health_result:
        return {"agent_id": agent_id, "status": "online", "health": health_result}
    return {"agent_id": agent_id, "status": "offline"}


@app.get("/api/agents/{agent_id}/status")
async def agent_status(agent_id: str):
    """Get real-time status (position, posture) from a robot."""
    robot = robot_registry.get_robot(agent_id)
    if not robot:
        return {"error": f"Unknown robot: {agent_id}", "status": "not_found"}

    result = await robot_client.get_status(robot.host, robot.port, robot_id=agent_id)
    return result


@app.get("/api/agents/{agent_id}/camera")
async def agent_camera(agent_id: str):
    """Proxy camera frame from a robot as JPEG."""
    from fastapi.responses import Response

    robot = robot_registry.get_robot(agent_id)
    if not robot:
        return {"error": f"Unknown robot: {agent_id}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://{robot.host}:{robot.camera_port}/camera/frame")
            resp.raise_for_status()
            return Response(content=resp.content, media_type="image/jpeg")
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/yolo/detect")
async def yolo_detect(
    agent_id: Optional[str] = Query(default=None, description="Agent to capture from"),
    conf: float = Query(default=0.25, ge=0.05, le=0.95, description="Confidence threshold"),
):
    """Capture a frame from Pi and run YOLO object detection on Central."""
    from fastapi.responses import Response

    # Find robot
    if agent_id:
        robot = robot_registry.get_robot(agent_id)
        if not robot:
            return {"error": f"Unknown robot: {agent_id}"}
    else:
        online = robot_registry.get_online_robots()
        if not online:
            return {"error": "No robots online"}
        robot = online[0]
        agent_id = robot.robot_id

    # Capture frame
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://{robot.host}:{robot.camera_port}/camera/frame")
            resp.raise_for_status()
            image_data = resp.content
    except Exception as e:
        return {"error": f"Failed to capture frame: {e}"}

    # Run YOLO
    result = detect_frame(image_data, conf_threshold=conf)
    if "error" in result:
        return result


    return {
        "agent_id": agent_id,
        "detections": result["detections"],
        "inference_ms": result["inference_ms"],
        "image_size": f"{result['image_width']}x{result['image_height']}",
        "detection_count": len(result["detections"]),
    }


@app.post("/api/yolo/detect/image")
async def yolo_detect_image(
    agent_id: Optional[str] = Query(default=None),
    conf: float = Query(default=0.25, ge=0.05, le=0.95),
):
    """Same as /api/yolo/detect but returns the annotated JPEG image."""
    from fastapi.responses import Response

    if agent_id:
        robot = robot_registry.get_robot(agent_id)
        if not robot:
            return {"error": f"Unknown robot: {agent_id}"}
    else:
        online = robot_registry.get_online_robots()
        if not online:
            return {"error": "No robots online"}
        robot = online[0]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://{robot.host}:{robot.camera_port}/camera/frame")
            resp.raise_for_status()
            image_data = resp.content
    except Exception as e:
        return {"error": f"Failed to capture frame: {e}"}

    result = detect_frame(image_data, conf_threshold=conf)
    if "error" in result:
        return result

    return Response(content=result["annotated_jpeg"], media_type="image/jpeg")


@app.get("/api/yolo/stream/{agent_id}")
async def yolo_stream(agent_id: str):
    """SSE stream: continuously grab frames from Pi, run YOLO, send results."""
    from fastapi.responses import StreamingResponse
    import asyncio
    import json as _json

    robot = robot_registry.get_robot(agent_id)
    if not robot:
        return {"error": f"Unknown robot: {agent_id}"}

    async def event_generator():
        while True:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"http://{robot.host}:{robot.camera_port}/camera/frame")
                    resp.raise_for_status()
                    image_data = resp.content

                result = detect_frame(image_data, conf_threshold=0.3)
                event_data = {
                    "detections": result.get("detections", []),
                    "inference_ms": result.get("inference_ms", 0),
                    "detection_count": len(result.get("detections", [])),
                }
                yield f"data: {_json.dumps(event_data)}\n\n"
            except Exception as e:
                yield f"data: {_json.dumps({'error': str(e)})}\n\n"

            await asyncio.sleep(0.5)  # ~2 FPS for YOLO analysis

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/detections/stream/{agent_id}")
async def detection_overlay_stream(agent_id: str):
    """SSE stream of detection bbox data from active missions (for canvas overlay).
    Reuses already-computed YOLO results — no extra inference or MJPEG connection."""
    from fastapi.responses import StreamingResponse
    import asyncio
    import json as _json

    async def event_generator():
        while True:
            if not has_active_mission(agent_id):
                # No mission — send empty detections so frontend clears overlay
                yield f"data: {_json.dumps({'detections': []})}\n\n"
                await asyncio.sleep(1)
                continue

            result = await wait_for_new_detections(agent_id, timeout=1.0)
            if result:
                yield f"data: {_json.dumps(result)}\n\n"
            else:
                yield f"data: {_json.dumps({'detections': []})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.websocket("/ws/stream/{agent_id}")
async def video_stream_ws(websocket: WebSocket, agent_id: str):
    """WebSocket video stream — forwards JPEG frames from the Robot Agent's internal WS
    to the browser. Replaces the former MJPEG-over-HTTP endpoint so a page showing many
    robots doesn't exhaust Chrome's 6-per-host HTTP/1.1 connection limit."""
    from .robot_agent import _ws_port_for

    # Accept first (so uvicorn logs a clean connection-close instead of HTTP 403),
    # then wait up to 5s for the robot to appear in the registry. This smooths
    # over the startup race: the browser can reconnect the moment uvicorn binds
    # the port, but initial ADS discovery takes a few seconds to populate.
    await websocket.accept()

    robot = robot_registry.get_robot(agent_id)
    for _ in range(10):
        if robot:
            break
        await asyncio.sleep(0.5)
        robot = robot_registry.get_robot(agent_id)
    if not robot:
        await websocket.close(code=1008, reason="robot not registered")
        return

    try:
        import websockets
    except ImportError:
        logger.error("websockets not installed for video proxy")
        await websocket.close(code=1011)
        return

    ws_port = _ws_port_for(agent_id)
    ws_url = f"ws://localhost:{ws_port}"
    client_alive = True
    while client_alive:
        try:
            async with websockets.connect(ws_url) as upstream:
                async for frame in upstream:
                    if not isinstance(frame, bytes):
                        continue
                    try:
                        await websocket.send_bytes(frame)
                    except (WebSocketDisconnect, RuntimeError):
                        client_alive = False
                        break
        except WebSocketDisconnect:
            client_alive = False
        except Exception:
            # Upstream failure — wait and retry unless browser is gone.
            await asyncio.sleep(1)


@app.post("/api/vision/analyze")
async def vision_analyze(
    agent_id: Optional[str] = Query(default=None, description="Agent to capture from"),
    question: str = Query(default="What do you see in this image? Describe everything.", description="Question about the image"),
):
    """Capture a frame from Pi's camera and analyze it with GPT-4o vision."""
    # Find robot to capture from
    if agent_id:
        robot = robot_registry.get_robot(agent_id)
        if not robot:
            return {"error": f"Unknown robot: {agent_id}"}
    else:
        online = robot_registry.get_online_robots()
        if not online:
            return {"error": "No robots online"}
        robot = online[0]
        agent_id = robot.robot_id

    # Capture frame from Pi
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://{robot.host}:{robot.camera_port}/camera/frame")
            resp.raise_for_status()
            image_data = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg")
    except Exception as e:
        logger.error(f"Vision: failed to capture frame from {agent_id}: {e}")
        return {"error": f"Failed to capture frame: {e}"}

    # Encode image as base64 for GPT-4o
    b64_image = base64.b64encode(image_data).decode("utf-8")
    logger.info(f"Vision: captured {len(image_data)} bytes from {agent_id}, analyzing...")

    # Send to GPT-4o vision
    try:
        from langchain_core.messages import HumanMessage

        vision_llm = create_vision_llm()
        message = HumanMessage(content=[
            {"type": "text", "text": f"You are analyzing a camera feed from a PiCrawler robot. {question}"},
            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64_image}"}},
        ])
        response = await vision_llm.ainvoke([message])
        analysis = response.content

        logger.info(f"Vision: analysis complete for {agent_id}")
        return {
            "agent_id": agent_id,
            "question": question,
            "analysis": analysis,
            "image_size_bytes": len(image_data),
        }
    except Exception as e:
        logger.error(f"Vision: GPT-4o analysis failed: {e}")
        return {"error": f"Vision analysis failed: {e}"}


# ---------------------------------------------------------------------------
# Browser-origin voice I/O (commander at the Command Center, not Pi mic)
# ---------------------------------------------------------------------------


@app.post("/api/voice/transcribe")
async def voice_transcribe(request: FastAPIRequest):
    """Transcribe an audio blob POSTed from the frontend.

    Expected: multipart/form-data with a 'file' field (audio blob from
    browser MediaRecorder — typically webm/opus but Whisper auto-detects).
    Returns: {"text": "<transcribed text>"}
    """
    try:
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            return {"error": "missing 'file' field"}
        audio_bytes = await upload.read()
        if not audio_bytes:
            return {"error": "empty audio"}

        # Whisper needs a filename for format detection
        suffix = ".webm"
        if hasattr(upload, "filename") and upload.filename and "." in upload.filename:
            suffix = "." + upload.filename.rsplit(".", 1)[1]

        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        import io
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = f"recording{suffix}"
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )
        text = (transcript.text or "").strip()
        logger.info(f"Voice transcribe: {len(audio_bytes)}B → {text!r}")
        return {"text": text}
    except Exception as e:
        logger.error(f"Voice transcribe failed: {e}")
        return {"error": str(e)}


class VoiceSpeakRequest(BaseModel):
    text: str
    voice: str = "onyx"       # onyx / echo / fable / nova / shimmer / alloy
    speed: float = 1.0


@app.post("/api/voice/speak")
async def voice_speak(req: VoiceSpeakRequest):
    """Turn text into mp3 audio via OpenAI TTS; returned as audio/mpeg."""
    from fastapi.responses import Response as _Resp
    try:
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        async with client.audio.speech.with_streaming_response.create(
            model="tts-1",
            voice=req.voice,
            input=req.text[:4000],     # Whisper TTS caps at 4096 chars
            speed=max(0.5, min(2.0, req.speed)),
        ) as response:
            audio_bytes = await response.read()
        return _Resp(content=audio_bytes, media_type="audio/mpeg")
    except Exception as e:
        logger.error(f"Voice speak failed: {e}")
        return {"error": str(e)}


@app.post("/api/voice")
async def voice_command(
    duration: int = Query(default=5, ge=1, le=30, description="Recording duration in seconds"),
    strategy: Optional[str] = Query(default=None),
):
    """Record voice from the first online robot, transcribe with Whisper, then execute."""
    task_id = f"task-{uuid.uuid4().hex[:8]}"

    # Step 1: Find an online robot to record from
    online = robot_registry.get_online_robots()
    if not online:
        return SearchResponse(task_id=task_id, status="failed", error="No robots online for voice recording")

    pi = online[0]
    record_url = f"http://{pi.host}:{pi.port}/voice/record?duration={duration}"
    logger.info(f"[{task_id}] Voice: recording {duration}s from {pi.robot_id} ({pi.host})")

    # Step 2: Record audio from Pi
    try:
        async with httpx.AsyncClient(timeout=duration + 30) as client:
            resp = await client.post(record_url)
            resp.raise_for_status()
            wav_data = resp.content
        logger.info(f"[{task_id}] Voice: received {len(wav_data)} bytes WAV")
    except Exception as e:
        logger.error(f"[{task_id}] Voice: recording failed: {e}")
        return SearchResponse(task_id=task_id, status="failed", error=f"Recording failed: {e}")

    # Step 3: Transcribe with Whisper
    try:
        openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_data)
            tmp_path = f.name

        with open(tmp_path, "rb") as audio_file:
            transcript = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en",
            )
        os.unlink(tmp_path)
        prompt = transcript.text.strip()
        logger.info(f"[{task_id}] Voice: transcribed -> '{prompt}'")

        if not prompt:
            return SearchResponse(task_id=task_id, status="failed", error="No speech detected")

    except Exception as e:
        logger.error(f"[{task_id}] Voice: transcription failed: {e}")
        return SearchResponse(task_id=task_id, status="failed", error=f"Transcription failed: {e}")

    # Step 4: Run planner with transcribed text
    try:
        result = await run_planner(prompt=prompt, strategy=strategy or "")
        agent_results = result.get("agent_results", [])
        agents_used = [r.get("agent_id", "") for r in agent_results]

        return {
            "task_id": task_id,
            "status": "completed" if not result.get("error") else "partial",
            "transcript": prompt,
            "agents_used": agents_used,
            "results": [
                {
                    "agent_id": r.get("agent_id", ""),
                    "status": r.get("status", "unknown"),
                    "response": r.get("response"),
                    "error": r.get("error"),
                }
                for r in agent_results
            ],
            "summary": result.get("summary", ""),
            "error": result.get("error"),
        }
    except Exception as e:
        logger.exception(f"[{task_id}] Voice: planner failed")
        return {"task_id": task_id, "status": "failed", "transcript": prompt, "error": str(e)}


# ---------------------------------------------------------------------------
# Memory / History endpoints
# ---------------------------------------------------------------------------




@app.get("/api/context/history")
async def context_history(limit: int = Query(default=10, ge=1, le=50)):
    """Get recent conversation history (prompts only)."""
    recent = _conversation_history[-limit:]
    return {
        "history": [
            {"prompt": h["prompt"], "timestamp": h["timestamp"]}
            for h in reversed(recent)
        ]
    }


# Context-aware phrases that reference previous tasks
_CONTEXT_PHRASES = {
    "do that again": "repeat",
    "do it again": "repeat",
    "again": "repeat",
    "repeat": "repeat",
    "再来一次": "repeat",
    "one more time": "repeat",
    "what happened": "last_result",
    "what was the result": "last_result",
    "结果怎么样": "last_result",
    "last result": "last_result",
}


def _resolve_context_prompt(prompt: str) -> tuple[Optional[str], Optional[dict]]:
    """Resolve context-aware prompts. Returns (resolved_prompt, cached_result) or (None, None)."""
    p = prompt.strip().lower().rstrip(".!?")

    for phrase, action in _CONTEXT_PHRASES.items():
        if p == phrase or p.startswith(phrase):
            if action == "repeat" and _conversation_history:
                last = _conversation_history[-1]
                logger.info(f"Context: repeating last prompt: {last['prompt']}")
                return last["prompt"], None
            elif action == "last_result" and _conversation_history:
                last = _conversation_history[-1]
                return None, last  # Return cached result directly
    return None, None


# Active background tasks
_active_tasks: dict[str, asyncio.Task] = {}
_task_results: dict[str, SearchResponse] = {}
_active_task_robots: dict[str, set[str]] = {}  # task_id → robot IDs dispatched for this task
_live_reasoning: dict[str, list[str]] = {}  # task_id → live reasoning steps
_live_robot_reasoning: dict[str, dict[str, list[str]]] = {}  # task_id → {robot_id → reasoning steps}
_live_comm_events: dict[str, list[dict]] = {}  # task_id → communication events for flow chart
_task_start_ts: dict[str, float] = {}  # task_id → server unix timestamp (for client-side event filtering)
_current_task_id: str = ""  # tracks active task for robot reasoning → comm_event association
_comm_event_counter: int = 0


def _emit_comm_event(task_id: str, from_: str, to_: str, type_: str, label: str, status: str = "success"):
    """Add a communication event for the flow chart."""
    global _comm_event_counter
    import time
    events = _live_comm_events.setdefault(task_id, [])
    events.append({
        "id": _comm_event_counter,
        "ts": time.time(),
        "from": from_,
        "to": to_,
        "type": type_,
        "label": label[:80],
        "status": status,
    })
    _comm_event_counter += 1


def _parse_reasoning_to_comm_event(task_id: str, content: str):
    """Parse a Planner reasoning string into a comm_event.

    Entities are: user, planner, robot agents, hardware.
    LLM is internal to each agent — not a separate entity.
    """
    import re
    robot_ids = re.findall(r'crawler-alpha-patrol-\d+', content)

    if content.startswith("Parse:") or content.startswith("Intent:"):
        # Planner's internal LLM decision — show as planner thinking
        _emit_comm_event(task_id, "planner", "planner", "think", content.split(":", 1)[1].strip())
    elif content.startswith("Discovery:"):
        _emit_comm_event(task_id, "planner", "planner", "discovery", content[10:].strip())
    elif content.startswith("Execute:"):
        for rid in robot_ids:
            short = rid.replace("crawler-alpha-patrol-", "")
            _emit_comm_event(task_id, "planner", rid, "dispatch", f"dispatch → {short}")
            _active_task_robots.setdefault(task_id, set()).add(rid)
        if not robot_ids:
            _emit_comm_event(task_id, "planner", "planner", "dispatch", content[8:].strip())
    elif content.startswith("Tool:"):
        # Planner ReAct tool call → dispatch to robot
        target = robot_ids[0] if robot_ids else "planner"
        _emit_comm_event(task_id, "planner", target, "tool_call", content[5:].strip())
    elif content.startswith("Result:"):
        source = robot_ids[0] if robot_ids else "planner"
        _emit_comm_event(task_id, source, "planner", "result", content[7:].strip())
    elif content.startswith("Agent result:"):
        # Robot has finished and returned — this IS a result.
        source = robot_ids[0] if robot_ids else "planner"
        _emit_comm_event(task_id, source, "planner", "result", content[13:].strip())
    elif content.startswith("Agent:"):
        # Planner announcing an upcoming dispatch to this robot — NOT a result.
        # Emit as a planner → robot dispatch so the robot shows "executing", not "done".
        for rid in robot_ids:
            _emit_comm_event(task_id, "planner", rid, "dispatch", content[6:].strip())
            _active_task_robots.setdefault(task_id, set()).add(rid)
        if not robot_ids:
            _emit_comm_event(task_id, "planner", "planner", "think", content[6:].strip())
    elif content.startswith("Done:"):
        _emit_comm_event(task_id, "planner", "user", "result", content[5:].strip())
    elif content.startswith("Summary:"):
        _emit_comm_event(task_id, "planner", "user", "summary", content[8:].strip())
    elif content.startswith("Coordinator:"):
        _emit_comm_event(task_id, "planner", "planner", "think", content[12:].strip())
    elif content.startswith("ReAct"):
        _emit_comm_event(task_id, "planner", "planner", "think", content.strip())
    elif content.startswith("VLM:") or content.startswith("VLM "):
        # Vision-LLM reasoning — treat as planner 'think' for topology pulses
        _emit_comm_event(task_id, "planner", "planner", "think", content.strip())


@app.post("/api/search", response_model=SearchResponse)
async def search(request: SearchRequest):
    """Submit a task. Returns immediately with task_id. Task runs in background."""
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    logger.info(f"[{task_id}] Search request: {request.prompt}")

    # --- Conversation context ---
    resolved_prompt, cached = _resolve_context_prompt(request.prompt)
    if cached is not None:
        logger.info(f"[{task_id}] Context: returning cached last result")
        last_resp = cached.get("response", {})
        return SearchResponse(
            task_id=task_id,
            status=last_resp.get("status", "completed"),
            agents_used=last_resp.get("agents_used", []),
            results=[AgentResult(**r) for r in last_resp.get("results", [])],
            summary=f"[Previous result for: {cached['prompt']}]\n{last_resp.get('summary', '')}",
        )
    if resolved_prompt:
        request.prompt = resolved_prompt
        logger.info(f"[{task_id}] Context resolved to: {request.prompt}")

    # Live reasoning: intercept messages_log appends
    global _current_task_id, _comm_event_counter
    import time as _time
    _live_reasoning[task_id] = []
    _live_robot_reasoning[task_id] = {}       # per-task robot reasoning dict
    _live_comm_events[task_id] = []
    _task_start_ts[task_id] = _time.time()
    _current_task_id = task_id
    _comm_event_counter = 0
    _emit_comm_event(task_id, "user", "planner", "command", request.prompt[:60])

    # Patch AIMessage to capture reasoning in real-time
    from langchain_core.messages import AIMessage as _AIMessage
    _original_prompt = request.prompt

    class _LiveMessagesList(list):
        """List that captures AIMessage content to live reasoning as they're appended."""
        def append(self, item):
            super().append(item)
            if hasattr(item, "content") and item.content and not item.content.startswith(_original_prompt):
                _live_reasoning.setdefault(task_id, []).append(item.content)
                _parse_reasoning_to_comm_event(task_id, item.content)

    # Start task in background
    async def _run_task():
        try:
            # Inject our live messages list into the planner
            from langchain_core.messages import HumanMessage
            live_messages = _LiveMessagesList([HumanMessage(content=request.prompt)])

            result = await planner_graph.ainvoke({
                "prompt": request.prompt,
                "strategy": request.strategy or "",
                "iteration": 1,
                "start_time": __import__("datetime").datetime.utcnow().timestamp(),
                "messages": live_messages,
            })

            agent_results = result.get("agent_results", [])
            agents_used = [r.get("agent_id", "") for r in agent_results]

            reasoning = _live_reasoning.get(task_id, [])

            resp = SearchResponse(
                task_id=task_id,
                status="completed" if not result.get("error") else "partial",
                agents_used=agents_used,
                results=[
                    AgentResult(
                        agent_id=r.get("agent_id", ""),
                        status=r.get("status", "unknown"),
                        response=r.get("response"),
                        error=r.get("error"),
                        structured=r.get("structured"),
                    )
                    for r in agent_results
                ],
                summary=result.get("summary", ""),
                error=result.get("error"),
                reasoning=reasoning,
            )
            _task_results[task_id] = resp
            _save_to_history(request.prompt, resp)
        except asyncio.CancelledError:
            _task_results[task_id] = SearchResponse(
                task_id=task_id, status="cancelled", summary="Task was cancelled",
                reasoning=_live_reasoning.get(task_id, []),
            )
        except Exception as e:
            logger.exception(f"[{task_id}] Search failed")
            _task_results[task_id] = SearchResponse(
                task_id=task_id, status="failed", error=str(e),
                reasoning=_live_reasoning.get(task_id, []),
            )
        finally:
            _active_tasks.pop(task_id, None)
            _active_task_robots.pop(task_id, None)
            # Schedule cleanup of per-task reasoning/events after a grace period
            # (long enough for the frontend to do a final poll and pick up the
            # completion status + final reasoning list).
            async def _cleanup_task_state(tid: str):
                await asyncio.sleep(60)
                _live_reasoning.pop(tid, None)
                _live_robot_reasoning.pop(tid, None)
                _live_comm_events.pop(tid, None)
                _task_start_ts.pop(tid, None)
                _task_results.pop(tid, None)
                logger.debug(f"Cleaned up task state: {tid}")
            asyncio.create_task(_cleanup_task_state(task_id))

    task = asyncio.create_task(_run_task())
    _active_tasks[task_id] = task

    return SearchResponse(
        task_id=task_id,
        status="running",
        summary=f"Task started: {request.prompt}",
        server_start_ts=_task_start_ts.get(task_id, 0.0),
    )


@app.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    """Get task status/result with live reasoning steps."""
    server_start_ts = _task_start_ts.get(task_id, 0.0)
    task_robot_reasoning = dict(_live_robot_reasoning.get(task_id, {}))
    if task_id in _task_results:
        resp = _task_results[task_id]
        return {
            **resp.dict(),
            "robot_reasoning": task_robot_reasoning,
            "comm_events": _live_comm_events.get(task_id, []),
            "server_start_ts": server_start_ts,
        }
    if task_id in _active_tasks:
        return {
            "task_id": task_id,
            "status": "running",
            "reasoning": _live_reasoning.get(task_id, []),
            "robot_reasoning": task_robot_reasoning,
            "comm_events": _live_comm_events.get(task_id, []),
            "server_start_ts": server_start_ts,
        }
    return {"task_id": task_id, "status": "not_found"}


@app.post("/api/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a running task and stop all active robot agents."""
    task = _active_tasks.get(task_id)
    if not task:
        return {"task_id": task_id, "status": "not_found"}

    # Cancel the planner asyncio task first
    task.cancel()
    logger.info(f"[{task_id}] Task cancelled by user")

    # Send NATS cancel only to robots involved in this task (fire-and-forget)
    from .planner.graph import _cancel_robot
    task_robots = _active_task_robots.get(task_id, set())
    if not task_robots:
        # Fallback: if dispatch hasn't been logged yet, cancel all online robots
        task_robots = {r.robot_id for r in robot_registry.get_online_robots()}
    for robot_id in task_robots:
        asyncio.create_task(_cancel_robot(robot_id))
        logger.info(f"[{task_id}] Sending cancel to {robot_id}")

    return {"task_id": task_id, "status": "cancelling"}


# ---------------------------------------------------------------------------
# Face Registry endpoints
# ---------------------------------------------------------------------------


@app.post("/api/faces/register")
async def register_face(
    name: str = Query(description="Name of the person"),
    agent_id: Optional[str] = Query(default=None, description="Agent to capture photo from (or upload)"),
):
    """Register a face by capturing from Pi camera. Send image as POST body,
    or omit body to auto-capture from the specified agent."""
    from fastapi import Request
    from starlette.requests import Request as StarletteRequest
    import starlette

    # We'll handle this via a separate upload endpoint
    # This endpoint captures from Pi camera
    if agent_id:
        robot = robot_registry.get_robot(agent_id)
        if not robot:
            return {"error": f"Unknown robot: {agent_id}"}
    else:
        online = robot_registry.get_online_robots()
        if not online:
            return {"error": "No robots online"}
        robot = online[0]
        agent_id = robot.robot_id

    # Capture frame from Pi
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://{robot.host}:{robot.camera_port}/camera/frame")
            resp.raise_for_status()
            image_data = resp.content
    except Exception as e:
        return {"error": f"Failed to capture frame: {e}"}

    result = face_registry.register(name, image_data)
    return result


@app.post("/api/faces/register/upload")
async def register_face_upload(
    request: FastAPIRequest,
    name: str = Query(description="Name of the person"),
):
    """Register a face by uploading raw JPEG in request body.
    Can be used by Pi agents or frontend to register a face directly."""
    image_data = await request.body()
    if not image_data or len(image_data) < 100:
        return {"error": "No valid image data in request body"}

    result = face_registry.register(name, image_data)
    return result


@app.delete("/api/faces/{name}")
async def unregister_face(name: str):
    """Remove a registered face."""
    return face_registry.unregister(name)


@app.get("/api/faces")
async def list_faces():
    """List all registered faces."""
    return {"faces": face_registry.list_faces()}


@app.post("/api/faces/identify")
async def identify_face(
    request: FastAPIRequest,
    threshold: float = Query(default=0.7, ge=0.3, le=0.99),
    agent_id: Optional[str] = Query(default=None, description="Which agent is sending this frame"),
):
    """Identify faces in a JPEG image sent by Pi agent.

    Pi detects a face locally (Vilib), then sends the full frame here.
    Central runs MTCNN + InceptionResnetV1 against the face registry.

    Request: raw JPEG body (Content-Type: image/jpeg)
    Response: {"matches": [...], "faces_detected": N, "faces_matched": N}
    """
    image_data = await request.body()
    if not image_data:
        return {"error": "No image data in request body"}

    if len(image_data) < 100:
        return {"error": f"Image data too small ({len(image_data)} bytes), expected JPEG"}

    # Run face detection + recognition against registry
    from .detection.face_registry import detect_and_embed_all
    all_faces = detect_and_embed_all(image_data)
    faces_detected = len(all_faces)

    matches = face_registry.match(image_data, threshold=threshold)

    # Log for debugging
    src = agent_id or "unknown"
    logger.info(f"Face identify from {src}: {faces_detected} faces detected, {len(matches)} matched, frame={len(image_data)} bytes")

    return {
        "matches": matches,
        "faces_detected": faces_detected,
        "faces_matched": len(matches),
        "registered_count": len(face_registry.list_faces()),
    }


@app.post("/api/faces/match")
async def match_faces(
    agent_id: Optional[str] = Query(default=None),
    threshold: float = Query(default=0.7, ge=0.3, le=0.99),
):
    """Capture a frame and match faces against the registry."""
    if agent_id:
        robot = robot_registry.get_robot(agent_id)
        if not robot:
            return {"error": f"Unknown robot: {agent_id}"}
    else:
        online = robot_registry.get_online_robots()
        if not online:
            return {"error": "No robots online"}
        robot = online[0]
        agent_id = robot.robot_id

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://{robot.host}:{robot.camera_port}/camera/frame")
            resp.raise_for_status()
            image_data = resp.content
    except Exception as e:
        return {"error": f"Failed to capture frame: {e}"}

    matches = face_registry.match(image_data, threshold=threshold)
    return {
        "agent_id": agent_id,
        "matches": matches,
        "registered_count": len(face_registry.list_faces()),
    }


# ---------------------------------------------------------------------------
# Search Mission endpoints (Central does all detection)
# ---------------------------------------------------------------------------


@app.post("/api/mission/start")
async def start_mission(
    target_type: str = Query(default="face", description="What to detect: face, object, color, qr_code, any"),
    target_name: str = Query(default="", description="For face: specific person name (optional)"),
    task_goal: str = Query(default="", description="Full task goal for Pi's LLM (e.g. 'find John and wave')"),
    agent_id: Optional[str] = Query(default=None),
):
    """Start a search mission: Pi patrols, Central watches stream and pushes observations."""
    # If searching for a specific person, check they're registered
    if target_type == "face" and target_name:
        registered = face_registry.list_faces()
        if not any(f["name"].lower() == target_name.lower() for f in registered):
            return {"error": f"'{target_name}' is not registered. Register their face first via /api/faces/register"}

    # Find robot
    if agent_id:
        robot = robot_registry.get_robot(agent_id)
        if not robot:
            return {"error": f"Unknown robot: {agent_id}"}
    else:
        online = robot_registry.get_online_robots()
        if not online:
            return {"error": "No robots online"}
        robot = online[0]
        agent_id = robot.robot_id

    mission = SearchMission(
        mission_id=str(uuid.uuid4())[:8],
        agent_id=agent_id,
        host=robot.host,
        camera_port=robot.camera_port,
        target_type=target_type,
        target_name=target_name,
        task_goal=task_goal or f"Search for {target_type}" + (f" ({target_name})" if target_name else ""),
    )

    # Run mission in background
    import asyncio
    asyncio.create_task(run_search_mission(mission))

    return {
        "status": "started",
        "mission_id": mission.mission_id,
        "target_type": target_type,
        "target_name": target_name,
        "task_goal": mission.task_goal,
        "agent_id": agent_id,
    }


@app.get("/api/mission/active")
async def active_missions():
    """Get all active search missions."""
    return {"missions": get_active_missions()}


@app.post("/api/mission/{mission_id}/cancel")
async def cancel_active_mission(mission_id: str):
    """Cancel an active search mission."""
    if cancel_mission(mission_id):
        return {"status": "cancelled", "mission_id": mission_id}
    return {"error": f"Mission {mission_id} not found or already completed"}


def _save_to_history(prompt: str, response: SearchResponse):
    """Save task to conversation history."""
    _conversation_history.append({
        "prompt": prompt,
        "response": response.model_dump(),
        "timestamp": datetime.utcnow().isoformat(),
    })
    # Keep only recent history
    while len(_conversation_history) > MAX_HISTORY:
        _conversation_history.pop(0)
