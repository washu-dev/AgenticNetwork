"""
Central Detection Engine — Central is BOTH the "eyes" AND the "brain".

Robot Agent receives UDP video frames from Pi, runs detection (YOLO / face recognition),
and passes detections to the CentralPiAgent which decides actions via LLM,
then sends commands to Pi via HTTP.

Video: Pi pushes UDP frames → Robot Agent receives
Commands: Robot Agent → HTTP POST → Pi
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .udp_receiver import UDPFrameReceiver
from .yolo_detector import detect_frame, detect_frame_with_tracking, reset_tracker, get_unique_count
from ..config import MISSION_TIMEOUT_STATIONARY, MISSION_TIMEOUT_PATROL, create_vision_llm

logger = logging.getLogger(__name__)


# ── VLM attribute verification ──

_vlm_cache: dict[str, bool] = {}  # cache to avoid repeated calls for same frame


async def _vlm_verify_attribute(frame: bytes, bbox: dict, task_goal: str) -> bool:
    """Use VLM to verify if a detected person matches the task description.

    Only called when task_goal contains attribute descriptions (color, clothing, etc.)
    that YOLO cannot determine.

    Args:
        frame: Full JPEG frame
        bbox: Bounding box {x1, y1, x2, y2}
        task_goal: User's natural language description (e.g. "person wearing blue shirt")

    Returns:
        True if the person matches the description
    """
    import base64
    import cv2
    import numpy as np

    try:
        # Crop the bbox region from the frame
        nparr = np.frombuffer(frame, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return True  # fallback: assume match

        x1 = max(0, int(bbox.get("x1", 0)))
        y1 = max(0, int(bbox.get("y1", 0)))
        x2 = min(img.shape[1], int(bbox.get("x2", img.shape[1])))
        y2 = min(img.shape[0], int(bbox.get("y2", img.shape[0])))
        cropped = img[y1:y2, x1:x2]

        if cropped.size == 0:
            return True

        _, buf = cv2.imencode(".jpg", cropped, [cv2.IMWRITE_JPEG_QUALITY, 70])
        img_b64 = base64.b64encode(buf.tobytes()).decode()

        from langchain_core.messages import HumanMessage
        vlm = create_vision_llm()

        msg = HumanMessage(content=[
            {"type": "text", "text": (
                f"Task: {task_goal}\n\n"
                "Does this person match the description in the task? "
                "Answer ONLY 'yes' or 'no'."
            )},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ])

        response = await vlm.ainvoke([msg])
        answer = response.content.strip().lower()
        matched = answer.startswith("yes")
        logger.info(f"VLM verify: '{task_goal}' → {answer} (matched={matched})")
        return matched

    except Exception as e:
        logger.warning(f"VLM verify failed: {e}, assuming match")
        return True


def _needs_vlm_verification(task_goal: str) -> bool:
    """Check if the task goal contains attribute descriptions that need VLM."""
    if not task_goal:
        return False
    keywords = ["wearing", "red", "blue", "green", "yellow", "white", "black",
                "orange", "pink", "purple", "brown", "gray", "grey",
                "shirt", "hat", "jacket", "dress", "glasses", "tall", "short",
                "color", "holding", "carrying", "mannequin", "dummy"]
    # Use word boundary matching to avoid false positives (e.g. "what" matching "hat")
    import re
    goal_lower = task_goal.lower()
    return any(re.search(rf'\b{kw}\b', goal_lower) for kw in keywords)


# How often to grab and analyze a frame (seconds)
FRAME_INTERVAL = 0.5
# MJPEG stream read timeout
STREAM_READ_TIMEOUT = 5
# Min interval between detection dispatches to agent (seconds)
DETECTION_DISPATCH_INTERVAL = 2.0


@dataclass
class SearchMission:
    """Represents a running search mission with Central-side detection."""
    mission_id: str
    agent_id: str
    host: str
    camera_port: int
    target_type: str = "any"
    target_name: str = ""
    task_goal: str = ""
    report_all: bool = False
    duration_s: int = 0  # 0 = use default timeout
    threshold: float = 0.7
    status: str = "pending"
    start_time: float = 0.0
    frames_analyzed: int = 0
    detections: list = field(default_factory=list)
    _seen_track_ids: set = field(default_factory=set)
    match_result: Optional[dict] = None
    error: Optional[str] = None
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def cancel(self):
        self._cancel_event.set()

    @property
    def is_cancelled(self):
        return self._cancel_event.is_set()

    @property
    def elapsed_s(self):
        return round(time.time() - self.start_time, 1) if self.start_time else 0

    def to_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "agent_id": self.agent_id,
            "target_type": self.target_type,
            "target_name": self.target_name,
            "task_goal": self.task_goal,
            "status": self.status,
            "elapsed_s": self.elapsed_s,
            "frames_analyzed": self.frames_analyzed,
            "detections": self.detections[-20:],
            "unique_targets": get_unique_count(self.agent_id),
            "match_result": self.match_result,
            "error": self.error,
        }


# Active missions tracker
_active_missions: dict[str, SearchMission] = {}
_active_tasks: dict[str, asyncio.Task] = {}

# Latest annotated JPEG per agent (for stream overlay)
_latest_annotated: dict[str, bytes] = {}
# Latest YOLO detection results per agent (for frontend canvas overlay)
_latest_detections: dict[str, dict] = {}
# Event to notify stream subscribers
_frame_events: dict[str, asyncio.Event] = {}


async def _grab_frame_from_stream(host: str, port: int) -> Optional[bytes]:
    """Grab a single JPEG frame from Pi's MJPEG stream."""
    url = f"http://{host}:{port}/mjpg"
    try:
        async with httpx.AsyncClient(timeout=STREAM_READ_TIMEOUT) as client:
            async with client.stream("GET", url) as resp:
                buffer = b""
                async for chunk in resp.aiter_bytes(chunk_size=4096):
                    buffer += chunk
                    frame = grab_mjpeg_frame(buffer)
                    if frame:
                        return frame
                    if len(buffer) > 500_000:
                        buffer = buffer[-100_000:]
    except Exception as e:
        logger.debug(f"Frame grab failed from {host}:{port}: {e}")
    return None


async def grab_single_frame(host: str, port: int) -> Optional[bytes]:
    """Grab a single JPEG frame from Pi's camera /photo endpoint."""
    url = f"http://{host}:{port}/photo"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        logger.debug(f"Single frame grab failed from {host}:{port}: {e}")
    return None


async def _detect_in_frame(frame: bytes, target_type: str, target_name: str = "",
                           threshold: float = 0.7, yolo_result: dict = None,
                           task_goal: str = "") -> list[dict]:
    """Run detection on a single frame. Returns list of detections.

    If task_goal contains attribute descriptions (e.g. 'wearing blue shirt'),
    VLM is used to verify person detections beyond what YOLO can determine.
    """
    results = []
    use_vlm = _needs_vlm_verification(task_goal)

    if target_type == "face":
        from .face_registry import face_registry
        if target_name:
            matches = face_registry.match(frame, threshold=threshold)
            for m in matches:
                results.append({
                    "label": f"face:{m['name']}",
                    "confidence": m["similarity"],
                    "bbox": m.get("bbox"),
                    "source": "central_face_id",
                    "is_target": m["name"].lower() == target_name.lower(),
                })
        else:
            matches = face_registry.match(frame, threshold=threshold)
            if matches:
                for m in matches:
                    results.append({
                        "label": f"face:{m['name']}",
                        "confidence": m["similarity"],
                        "bbox": m.get("bbox"),
                        "source": "central_face_id",
                        "is_target": True,
                    })
            else:
                from .face_registry import detect_and_embed_all
                all_faces = detect_and_embed_all(frame)
                for f in all_faces:
                    results.append({
                        "label": "face:unknown",
                        "confidence": f["confidence"],
                        "bbox": f["bbox"],
                        "source": "central_face_detect",
                        "is_target": True,
                    })

    elif target_type in ("object", "any"):
        yr = yolo_result or detect_frame(frame, conf_threshold=threshold)
        for d in yr.get("detections", []):
            if target_name:
                is_target = target_name.lower() in d["label"].lower()
            else:
                is_target = True

            # VLM verification: if YOLO says "person" and task has attribute description
            # Cache by track_id to avoid repeated VLM calls for the same tracked person
            track_id = d.get("track_id")
            if use_vlm and is_target and d["label"] == "person" and d.get("bbox"):
                cache_key = f"{task_goal}:{track_id}" if track_id is not None else None
                if cache_key and cache_key in _vlm_cache:
                    is_target = _vlm_cache[cache_key]
                else:
                    is_target = await _vlm_verify_attribute(frame, d["bbox"], task_goal)
                    if cache_key:
                        _vlm_cache[cache_key] = is_target
                if is_target:
                    d["label"] = f"person (VLM confirmed: matches description)"

            results.append({
                "label": d["label"],
                "confidence": d["confidence"],
                "bbox": d.get("bbox"),
                "track_id": d.get("track_id"),
                "source": "central_yolo" + ("+vlm" if use_vlm else ""),
                "is_target": is_target,
            })

    elif target_type == "color":
        yr = yolo_result or detect_frame(frame, conf_threshold=threshold)
        for d in yr.get("detections", []):
            if target_name:
                is_target = target_name.lower() in d["label"].lower()
            else:
                is_target = True
            results.append({
                "label": d["label"],
                "confidence": d["confidence"],
                "bbox": d.get("bbox"),
                "source": "central_yolo",
                "is_target": is_target,
            })

    elif target_type == "qr_code":
        try:
            import cv2
            import numpy as np
            nparr = np.frombuffer(frame, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                detector = cv2.QRCodeDetector()
                data, points, _ = detector.detectAndDecode(img)
                if data:
                    results.append({
                        "label": f"qr_code:{data}",
                        "confidence": 1.0,
                        "bbox": points.tolist() if points is not None else None,
                        "source": "central_qr",
                        "is_target": True,
                    })
        except Exception as e:
            logger.debug(f"QR detection failed: {e}")

    else:
        yr = yolo_result or detect_frame(frame, conf_threshold=threshold)
        for d in yr.get("detections", []):
            if target_name:
                is_target = target_name.lower() in d["label"].lower()
            else:
                is_target = True
            results.append({
                "label": d["label"],
                "confidence": d["confidence"],
                "bbox": d.get("bbox"),
                "source": "central_yolo",
                "is_target": is_target,
            })

    return results



async def run_search_mission(mission: SearchMission, existing_udp_receiver=None) -> dict:
    """Execute a search mission with Central-side detection and Central-side decision.

    1. Get CentralPiAgent for this robot
    2. Set mission on agent, start patrol if needed
    3. Continuously pull frames and run detection
    4. Pass detections in-process to CentralPiAgent
    5. Agent's LLM decides actions, sends commands to robot
    6. Loop until mission_ended, timeout, or cancel

    Args:
        existing_udp_receiver: If provided, reuse this UDP receiver instead of creating a new one.
                               Pi video stream will not be started/stopped by the mission.
    """
    from ..pi_agents.base_agent import pi_agent_manager
    from ..infra.robot_registry import robot_registry

    _active_missions[mission.mission_id] = mission
    _active_tasks[mission.mission_id] = asyncio.current_task()
    mission.start_time = time.time()
    mission.status = "searching"
    reset_tracker(mission.agent_id)  # Reset ByteTrack state for fresh counting
    _vlm_cache.clear()  # Reset VLM cache for fresh verification

    try:
        # Step 1: Get CentralPiAgent
        robot = robot_registry.get_robot(mission.agent_id)
        if not robot:
            mission.status = "failed"
            mission.error = f"Robot {mission.agent_id} not found in registry"
            return mission.to_dict()

        agent = pi_agent_manager.get_or_create(robot)
        agent.set_mission(mission.mission_id, mission.task_goal, mission.report_all)

        # Step 2: Start patrol (Robot Agent LLM decides movement pattern)
        logger.info(f"Mission {mission.mission_id}: starting on {mission.agent_id}")
        patrol_task = asyncio.create_task(agent.patrol())
        await asyncio.sleep(1.0)

        # Step 3: Start UDP video receiver and tell Pi to start streaming
        timeout = mission.duration_s if mission.duration_s > 0 else MISSION_TIMEOUT_PATROL
        mission_ended = False
        last_dispatch_time = 0.0

        if existing_udp_receiver is None:
            raise ValueError("UDP receiver must be provided by Robot Agent")
        udp_receiver = existing_udp_receiver
        logger.info(f"Mission {mission.mission_id}: using UDP receiver on port {udp_receiver.port}")

        try:
          while not mission.is_cancelled and not mission_ended:
            # End mission when patrol finishes in report mode (no target to wait for)
            if patrol_task and patrol_task.done() and not mission_ended and mission.report_all:
                logger.info(f"Mission {mission.mission_id}: patrol finished, ending mission")
                mission.status = "completed"
                break

            if time.time() - mission.start_time > timeout:
                mission.status = "timeout"
                mission.error = "Mission timeout"
                logger.warning(f"Mission {mission.mission_id}: timeout after {timeout}s")
                break

            frame = await udp_receiver.wait_for_frame(timeout=2.0)
            if frame is None:
                continue

            mission.frames_analyzed += 1

            # Only run YOLO for non-face tasks (with ByteTrack for counting)
            yolo_result = None
            if mission.target_type != "face":
                yolo_result = await asyncio.to_thread(
                    detect_frame_with_tracking, frame, mission.agent_id,
                    conf_threshold=mission.threshold,
                )
                annotated = yolo_result.get("annotated_jpeg")
                if annotated:
                    _latest_annotated[mission.agent_id] = annotated
                _latest_detections[mission.agent_id] = {
                    "detections": yolo_result.get("detections", []),
                    "image_width": yolo_result.get("image_width", 640),
                    "image_height": yolo_result.get("image_height", 480),
                }

            # Notify stream subscribers
            evt = _frame_events.get(mission.agent_id)
            if evt:
                evt.set()

            # Run detection (async because VLM verification may be needed)
            detections = await _detect_in_frame(
                frame, mission.target_type, mission.target_name,
                mission.threshold, yolo_result,
                task_goal=mission.task_goal,
            )

            # Accumulate detections (deduplicate by track_id)
            for d in detections:
                track_id = d.get("track_id")
                if track_id is not None:
                    key = f"{d.get('label', '')}:{track_id}"
                    if key in mission._seen_track_ids:
                        continue
                    mission._seen_track_ids.add(key)
                mission.detections.append(d)

            # Record first target match
            if mission.match_result is None:
                target_hit = next(
                    (d for d in detections if d.get("is_target")), None
                )
                if target_hit:
                    mission.match_result = target_hit
                    logger.info(
                        f"Mission {mission.mission_id}: first target match: "
                        f"'{target_hit['label']}' "
                        f"(confidence={target_hit['confidence']})"
                    )

            # Dispatch to CentralPiAgent (throttled)
            if mission.report_all:
                push_detections = detections
            else:
                push_detections = [d for d in detections if d.get("is_target")]

            if push_detections:
                now = time.time()
                if now - last_dispatch_time >= DETECTION_DISPATCH_INTERVAL:
                    result = await agent.handle_detection({
                        "mission_id": mission.mission_id,
                        "task_goal": mission.task_goal,
                        "target_type": mission.target_type,
                        "target_name": mission.target_name,
                        "status": "searching",
                        "detections": push_detections,
                        "elapsed_s": mission.elapsed_s,
                        "report_all": mission.report_all,
                    })
                    last_dispatch_time = now

                    if result.get("mission_ended"):
                        mission.status = "completed"
                        mission_ended = True
                        break

            # Log progress
            if mission.frames_analyzed % 10 == 0:
                logger.info(
                    f"Mission {mission.mission_id}: "
                    f"{mission.frames_analyzed} frames, "
                    f"{mission.elapsed_s}s, "
                    f"{len(mission.detections)} detections"
                )
        finally:
            pass  # UDP receiver owned by Robot Agent, not stopped here

        # Mission ended — handle finish
        if mission.is_cancelled:
            mission.status = "cancelled"
            logger.info(f"Mission {mission.mission_id}: cancelled")
        elif not mission_ended and mission.status == "timeout":
            pass  # already set above

        # Send "finished" to agent for fallback action (if not already ended)
        if not mission_ended:
            finish_reason = mission.status  # "timeout" or "cancelled"
            await agent.handle_detection({
                "mission_id": mission.mission_id,
                "task_goal": mission.task_goal,
                "status": "finished",
                "reason": finish_reason,
                "detections": mission.detections[-10:],
                "elapsed_s": mission.elapsed_s,
                "report_all": mission.report_all,
            })

        # Stop patrol if running
        agent.stop_patrol()
        if patrol_task and not patrol_task.done():
            patrol_task.cancel()

        agent.clear_mission()

        return mission.to_dict()

    except asyncio.CancelledError:
        mission.status = "cancelled"
        mission.error = "Server shutting down"
        logger.info(f"Mission {mission.mission_id}: cancelled (server shutdown)")
        return mission.to_dict()

    except Exception as e:
        mission.status = "failed"
        mission.error = str(e)
        logger.error(f"Mission {mission.mission_id} failed: {e}")
        return mission.to_dict()

    finally:
        _active_missions.pop(mission.mission_id, None)
        _active_tasks.pop(mission.mission_id, None)
        _latest_annotated.pop(mission.agent_id, None)


def get_active_missions() -> list[dict]:
    return [m.to_dict() for m in _active_missions.values()]


def has_active_mission(agent_id: str) -> bool:
    return any(m.agent_id == agent_id for m in _active_missions.values())


def get_latest_annotated_frame(agent_id: str) -> Optional[bytes]:
    return _latest_annotated.get(agent_id)


async def wait_for_new_frame(agent_id: str, timeout: float = 1.0) -> Optional[bytes]:
    if agent_id not in _frame_events:
        _frame_events[agent_id] = asyncio.Event()
    evt = _frame_events[agent_id]
    evt.clear()
    try:
        await asyncio.wait_for(evt.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return _latest_annotated.get(agent_id)


async def wait_for_new_detections(agent_id: str, timeout: float = 1.0) -> Optional[dict]:
    if agent_id not in _frame_events:
        _frame_events[agent_id] = asyncio.Event()
    evt = _frame_events[agent_id]
    evt.clear()
    try:
        await asyncio.wait_for(evt.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return _latest_detections.get(agent_id)


def cancel_mission(mission_id: str) -> bool:
    mission = _active_missions.get(mission_id)
    if mission:
        mission.cancel()
        return True
    return False


def cancel_all_missions():
    count = len(_active_tasks)
    for task in _active_tasks.values():
        task.cancel()
    logger.info(f"Cancelled {count} active mission tasks")
