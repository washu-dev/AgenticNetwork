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


def _crop_bbox(frame_jpeg: bytes, bbox: dict) -> Optional[bytes]:
    """Crop a bounding box region from a JPEG frame. Returns JPEG bytes or None."""
    import cv2
    import numpy as np
    try:
        nparr = np.frombuffer(frame_jpeg, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        x1 = max(0, int(bbox.get("x1", 0)))
        y1 = max(0, int(bbox.get("y1", 0)))
        x2 = min(img.shape[1], int(bbox.get("x2", img.shape[1])))
        y2 = min(img.shape[0], int(bbox.get("y2", img.shape[0])))
        cropped = img[y1:y2, x1:x2]
        if cropped.size == 0:
            return None
        _, buf = cv2.imencode(".jpg", cropped, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes()
    except Exception:
        return None


async def _vlm_dedup_objects(track_crops: dict, cooccurrences: set = None) -> dict:
    """Use VLM to deduplicate tracked objects and return accurate counts.

    Args:
        track_crops: dict of {label:track_id → JPEG bytes}
        cooccurrences: set of frozenset({key1, key2}) pairs that appeared in the same frame
                       (these are definitely different objects, no need to ask VLM)

    Returns:
        dict of {label: count} with deduplicated counts
    """
    import base64
    from collections import defaultdict

    cooccurrences = cooccurrences or set()

    # Group crops by label
    label_groups = defaultdict(list)
    for key, crop_bytes in track_crops.items():
        label = key.split(":")[0]
        label_groups[label].append((key, crop_bytes))

    result_counts = {}

    for label, crops in label_groups.items():
        if len(crops) <= 1:
            result_counts[label] = len(crops)
            continue

        # Check co-occurrences: find the largest group that ALL appeared in the same single frame.
        # cooccurrences stores pairwise co-occurrences, but we need the max clique.
        # For small N, check each frame's contribution directly.
        # We stored pairwise, so find the largest subset where every pair co-occurred.
        crop_keys = [key for key, _ in crops]
        min_unique = 1
        # Simple approach: for each pair that co-occurred, that's at least 2.
        # Then check if any triple all co-occurred, etc.
        from itertools import combinations
        for size in range(len(crop_keys), 1, -1):
            found = False
            for combo in combinations(crop_keys, size):
                # Check if every pair in this combo co-occurred
                if all(frozenset({a, b}) in cooccurrences for a, b in combinations(combo, 2)):
                    min_unique = size
                    found = True
                    break
            if found:
                break

        logger.info(f"VLM dedup: {label} — {len(crops)} tracks, min {min_unique} from co-occurrence")

        # If all tracks co-occurred, no need to ask VLM
        if min_unique >= len(crops):
            result_counts[label] = len(crops)
            logger.info(f"VLM dedup: {label} — all {len(crops)} co-occurred, skipping VLM")
            continue

        # Ask VLM, but tell it about the minimum
        from langchain_core.messages import HumanMessage
        vlm = create_vision_llm()

        cooccur_note = ""
        if min_unique > 1:
            cooccur_note = f" Note: at least {min_unique} of these were seen simultaneously, so there are at least {min_unique} unique ones."

        content = [{"type": "text", "text": (
            f"I have {len(crops)} cropped images of '{label}' detected by a robot camera during patrol. "
            f"Some may be the same {label} seen at different times/angles. "
            f"Look at each image and determine how many UNIQUE {label}(s) there are.{cooccur_note} "
            f"Reply with ONLY a number."
        )}]

        for i, (key, crop_bytes) in enumerate(crops):
            img_b64 = base64.b64encode(crop_bytes).decode()
            content.append({
                "type": "text",
                "text": f"Image {i+1} (track: {key}):"
            })
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
            })

        try:
            response = await vlm.ainvoke([HumanMessage(content=content)])
            answer = response.content.strip()
            import re
            nums = re.findall(r'\d+', answer)
            count = int(nums[0]) if nums else len(crops)
            # Ensure count is at least the co-occurrence minimum
            count = max(count, min_unique)
            result_counts[label] = count
            logger.info(f"VLM dedup: {label} — {len(crops)} tracks → {count} unique (min {min_unique})")
        except Exception as e:
            logger.warning(f"VLM dedup failed for {label}: {e}, falling back to track count")
            result_counts[label] = max(min_unique, len(crops))

    return result_counts


async def _vlm_resolve_label(crop_jpeg: bytes, candidate_labels: list) -> str:
    """Use VLM to resolve a label conflict for a single object.

    Args:
        crop_jpeg: JPEG bytes of the cropped object
        candidate_labels: list of candidate labels (e.g. ["dog", "horse"])

    Returns:
        The correct label chosen by VLM
    """
    import base64
    from langchain_core.messages import HumanMessage

    img_b64 = base64.b64encode(crop_jpeg).decode()
    vlm = create_vision_llm()

    labels_str = " or ".join(candidate_labels)
    msg = HumanMessage(content=[
        {"type": "text", "text": (
            f"The detection system is confused about this object. "
            f"Is this a {labels_str}? Reply with ONLY one of: {', '.join(candidate_labels)}"
        )},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
    ])

    response = await vlm.ainvoke([msg])
    answer = response.content.strip().lower()

    # Match to one of the candidate labels
    for label in candidate_labels:
        if label.lower() in answer:
            return label
    # Fallback: return first candidate
    return candidate_labels[0]


async def _vlm_describe_objects(track_crops: dict) -> dict:
    """Use VLM to describe the appearance of each detected object.

    Args:
        track_crops: dict of {label:track_id → JPEG bytes}

    Returns:
        dict of {label:track_id → description string}
    """
    import base64
    from langchain_core.messages import HumanMessage
    from collections import defaultdict

    # Group by label, pick one representative crop per label
    label_best = {}
    for key, crop_bytes in track_crops.items():
        label = key.split(":")[0]
        if label not in label_best:
            label_best[label] = (key, crop_bytes)

    descriptions = {}
    vlm = create_vision_llm()

    for label, (key, crop_bytes) in label_best.items():
        img_b64 = base64.b64encode(crop_bytes).decode()
        msg = HumanMessage(content=[
            {"type": "text", "text": (
                f"Briefly describe this {label}'s appearance in 1 sentence. "
                "Include colors, clothing, size, or distinguishing features. "
                "Be factual and concise."
            )},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ])

        try:
            response = await vlm.ainvoke([msg])
            desc = response.content.strip()
            descriptions[label] = desc
            logger.info(f"VLM describe: {label} → {desc[:80]}")
        except Exception as e:
            logger.warning(f"VLM describe failed for {label}: {e}")
            descriptions[label] = label

    return descriptions


class TrackRemapper:
    """Remaps new ByteTrack IDs to original IDs when an object briefly leaves and re-enters frame.

    Mitigates track ID drift: same physical object gets a new track_id after leaving frame,
    causing duplicate counting. Uses IoU overlap to match new tracks to recently disappeared ones.
    """

    def __init__(self, max_missing_frames: int = 10, min_iou: float = 0.3):
        self._active: dict = {}       # track_id → bbox
        self._disappeared: dict = {}  # track_id → (bbox, frames_missing)
        self._remapping: dict = {}    # new_id → original_id
        self._max_missing = max_missing_frames
        self._min_iou = min_iou

    @staticmethod
    def _iou(a: dict, b: dict) -> float:
        ix1 = max(a["x1"], b["x1"])
        iy1 = max(a["y1"], b["y1"])
        ix2 = min(a["x2"], b["x2"])
        iy2 = min(a["y2"], b["y2"])
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        if inter == 0:
            return 0.0
        area_a = (a["x2"] - a["x1"]) * (a["y2"] - a["y1"])
        area_b = (b["x2"] - b["x1"]) * (b["y2"] - b["y1"])
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    def remap(self, detections: list) -> list:
        """Apply track ID remapping to a frame's detections."""
        current = {
            d["track_id"]: d.get("bbox")
            for d in detections
            if d.get("track_id") is not None and d.get("bbox")
        }

        # Age disappeared tracks, evict stale ones
        stale = [tid for tid, (_, age) in self._disappeared.items() if age + 1 > self._max_missing]
        for tid in stale:
            self._disappeared.pop(tid)
            self._remapping = {k: v for k, v in self._remapping.items() if v != tid}
        for tid in [t for t in self._disappeared if t not in stale]:
            bbox, age = self._disappeared[tid]
            self._disappeared[tid] = (bbox, age + 1)

        # Mark tracks that disappeared this frame
        for tid, bbox in self._active.items():
            if tid not in current:
                self._disappeared[tid] = (bbox, 0)

        # Remap new track IDs to disappeared ones via IoU
        result = []
        for d in detections:
            tid = d.get("track_id")
            if tid is None:
                result.append(d)
                continue
            bbox = d.get("bbox")
            if tid not in self._active and tid not in self._remapping and bbox:
                best_tid, best_iou = None, self._min_iou
                for old_tid, (old_bbox, _) in self._disappeared.items():
                    iou = self._iou(bbox, old_bbox)
                    if iou > best_iou:
                        best_iou, best_tid = iou, old_tid
                if best_tid is not None:
                    self._remapping[tid] = best_tid
                    self._disappeared.pop(best_tid)
            original_id = self._remapping.get(tid, tid)
            if original_id != tid:
                d = dict(d)
                d["track_id"] = original_id
            result.append(d)

        # Update active set with remapped IDs
        self._active = {self._remapping.get(tid, tid): bbox for tid, bbox in current.items()}
        return result


def _resolve_detection_labels(detections: list, tid_label_counts: dict) -> list:
    """Replace each detection's label with the most frequent label seen for that track_id so far.

    Provides real-time label correction: if YOLO is confused between 'dog' and 'horse',
    the historically dominant label wins before the detection is dispatched to the agent.
    """
    result = []
    for d in detections:
        tid = d.get("track_id")
        counts = tid_label_counts.get(tid) if tid is not None else None
        if counts and len(counts) > 1:
            best = max(counts, key=counts.get)
            if best != d.get("label"):
                d = dict(d)
                d["label"] = best
        result.append(d)
    return result


def _needs_vlm_verification(task_goal: str) -> bool:
    """Legacy fallback: check if the task goal contains attribute descriptions that need VLM.
    Prefer using mission.use_vlm (set by planner LLM) instead of this keyword matching.
    """
    if not task_goal:
        return False
    keywords = ["wearing", "red", "blue", "green", "yellow", "white", "black",
                "orange", "pink", "purple", "brown", "gray", "grey",
                "shirt", "hat", "jacket", "dress", "glasses", "tall", "short",
                "color", "holding", "carrying", "mannequin", "dummy"]
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
    vlm_verify: bool = False    # VLM verifies attributes (e.g. "wearing green shirt")
    vlm_describe: bool = False  # VLM describes detected objects in detail
    duration_s: int = 0  # 0 = use default timeout
    threshold: float = 0.7
    status: str = "pending"
    start_time: float = 0.0
    frames_analyzed: int = 0
    detections: list = field(default_factory=list)
    _seen_track_ids: set = field(default_factory=set)
    _track_crops: dict = field(default_factory=dict)  # track_key → JPEG bytes of cropped detection
    _tid_label_counts: dict = field(default_factory=dict)  # track_id → Counter of labels
    _cooccurrences: set = field(default_factory=set)  # set of frozenset({key1, key2}) that appeared in same frame
    _track_bbox_areas: dict = field(default_factory=dict)  # track_key → bbox area of saved crop
    _track_remapper: TrackRemapper = field(default_factory=TrackRemapper)
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
        result = {
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
        if hasattr(self, "dedup_counts") and self.dedup_counts:
            result["dedup_counts"] = self.dedup_counts
        if hasattr(self, "vlm_descriptions") and self.vlm_descriptions:
            result["vlm_descriptions"] = self.vlm_descriptions
        return result


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
                           task_goal: str = "", use_vlm: bool = False) -> list[dict]:
    """Run detection on a single frame. Returns list of detections.

    If use_vlm=True (decided by planner LLM), VLM is used to verify
    person detections beyond what YOLO can determine.
    """
    results = []

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

            # VLM verification: if task requires specific attributes (e.g. "person with green shirt")
            # Only person detections go through VLM; non-person objects are NOT targets
            # when VLM mode is active (task is looking for a specific person, not any object)
            track_id = d.get("track_id")
            if use_vlm:
                if d["label"] == "person" and d.get("bbox"):
                    cache_key = f"{task_goal}:{track_id}" if track_id is not None else None
                    if cache_key and cache_key in _vlm_cache:
                        is_target = _vlm_cache[cache_key]
                    else:
                        is_target = await _vlm_verify_attribute(frame, d["bbox"], task_goal)
                        if cache_key:
                            _vlm_cache[cache_key] = is_target
                    if is_target:
                        d["label"] = f"person (VLM confirmed: matches description)"
                else:
                    # Non-person objects are not targets when looking for a specific person
                    is_target = False

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
            # End mission when patrol finishes
            if patrol_task and patrol_task.done() and not mission_ended:
                if mission.report_all:
                    logger.info(f"Mission {mission.mission_id}: patrol finished, ending mission")
                    mission.status = "completed"
                    break
                elif not mission.match_result:
                    # Find+act mode: patrol done but target never found → end mission
                    logger.info(f"Mission {mission.mission_id}: patrol finished, target not found")
                    mission.status = "completed"
                    break
                elif time.time() - last_dispatch_time > 10:
                    # Find+act mode: patrol done, target was found but lost for >10s → end mission
                    logger.info(f"Mission {mission.mission_id}: patrol finished, target lost during approach")
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
                # Remap track IDs to recover identity after objects briefly leave frame
                yolo_result["detections"] = mission._track_remapper.remap(
                    yolo_result.get("detections", [])
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
                use_vlm=mission.vlm_verify,
            )

            # Accumulate detections (deduplicate by track_id)
            for d in detections:
                track_id = d.get("track_id")
                label = d.get("label", "unknown")
                if track_id is not None:
                    # Track label frequency per track_id (for resolving conflicts later)
                    if track_id not in mission._tid_label_counts:
                        mission._tid_label_counts[track_id] = {}
                    mission._tid_label_counts[track_id][label] = mission._tid_label_counts[track_id].get(label, 0) + 1

                    key = f"{label}:{track_id}"
                    bbox = d.get("bbox")
                    crop = _crop_bbox(frame, bbox) if bbox and frame is not None else None
                    if key in mission._seen_track_ids:
                        # Update crop if this bbox is significantly larger (closer, clearer view)
                        if crop is not None and bbox:
                            new_area = (bbox["x2"] - bbox["x1"]) * (bbox["y2"] - bbox["y1"])
                            if new_area > mission._track_bbox_areas.get(key, 0) * 1.2:
                                mission._track_crops[key] = crop
                                mission._track_bbox_areas[key] = new_area
                        continue
                    mission._seen_track_ids.add(key)
                    if crop is not None:
                        mission._track_crops[key] = crop
                        if bbox:
                            mission._track_bbox_areas[key] = (bbox["x2"] - bbox["x1"]) * (bbox["y2"] - bbox["y1"])
                mission.detections.append(d)

            # Record co-occurrences: track keys that appear in same frame → definitely different objects
            from collections import defaultdict as _dd
            frame_keys_by_label = _dd(list)
            for d in detections:
                tid = d.get("track_id")
                if tid is not None:
                    frame_keys_by_label[d.get("label", "unknown")].append(f"{d.get('label', 'unknown')}:{tid}")
            for label, keys in frame_keys_by_label.items():
                if len(keys) > 1:
                    for i in range(len(keys)):
                        for j in range(i + 1, len(keys)):
                            mission._cooccurrences.add(frozenset({keys[i], keys[j]}))

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

            # Real-time label correction: use majority-vote label per track_id before dispatch
            push_detections = _resolve_detection_labels(push_detections, mission._tid_label_counts)

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

        # Resolve label conflicts: same track_id with different labels → use VLM to decide
        if mission._tid_label_counts:
            for tid, label_freq in mission._tid_label_counts.items():
                if len(label_freq) <= 1:
                    continue
                labels = list(label_freq.keys())
                # Pick the crop with highest confidence to ask VLM
                best_crop_key = None
                for label in labels:
                    key = f"{label}:{tid}"
                    if key in mission._track_crops:
                        best_crop_key = key
                        break
                if best_crop_key:
                    try:
                        resolved = await _vlm_resolve_label(mission._track_crops[best_crop_key], labels)
                        logger.info(f"Mission {mission.mission_id}: VLM resolved track#{tid}: {labels} → {resolved}")
                    except Exception as e:
                        # Fallback to most frequent
                        resolved = max(label_freq, key=label_freq.get)
                        logger.warning(f"Mission {mission.mission_id}: VLM resolve failed for track#{tid}: {e}, using {resolved}")
                else:
                    resolved = max(label_freq, key=label_freq.get)

                for label in labels:
                    if label != resolved:
                        bad_key = f"{label}:{tid}"
                        mission._track_crops.pop(bad_key, None)
                        mission._seen_track_ids.discard(bad_key)
                        mission.detections = [
                            d for d in mission.detections
                            if not (d.get("track_id") == tid and d.get("label") == label)
                        ]

        # VLM dedup: get accurate object counts by comparing cropped images
        if mission._track_crops:
            try:
                dedup_counts = await _vlm_dedup_objects(mission._track_crops, mission._cooccurrences)
                mission.dedup_counts = dedup_counts
                logger.info(f"Mission {mission.mission_id}: VLM dedup counts: {dedup_counts}")
            except Exception as e:
                logger.warning(f"Mission {mission.mission_id}: VLM dedup failed: {e}")

        # VLM describe: describe each detected object's appearance
        if mission._track_crops and mission.vlm_describe:
            try:
                descriptions = await _vlm_describe_objects(mission._track_crops)
                mission.vlm_descriptions = descriptions
                logger.info(f"Mission {mission.mission_id}: VLM descriptions: {list(descriptions.keys())}")
            except Exception as e:
                logger.warning(f"Mission {mission.mission_id}: VLM describe failed: {e}")

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
