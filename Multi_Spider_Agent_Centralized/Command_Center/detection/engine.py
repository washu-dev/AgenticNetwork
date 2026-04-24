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
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .udp_receiver import UDPFrameReceiver
from .yolo_detector import detect_frame, detect_frame_with_tracking, reset_tracker, get_unique_count
# LOCALIZATION: AprilTag-based position tracking is disabled — files moved to unused/.
# from .apriltag_detector import detect_tags, get_tag_info
from ..config import MISSION_TIMEOUT_STATIONARY, MISSION_TIMEOUT_PATROL, create_vision_llm
# LOCALIZATION: from ..infra.position_tracker import update_robot_position

# LOCALIZATION: Track unknown AprilTag IDs we've already warned about.
# _logged_unknown_tags: set = set()

logger = logging.getLogger(__name__)


# ── VLM attribute verification ──

_vlm_cache: dict[str, bool] = {}  # cache to avoid repeated calls for same frame


async def _vlm_verify_attribute(frame: bytes, bbox: dict, task_goal: str, label: str = "", emit_fn=None) -> bool:
    """Use VLM to verify if a detected object matches the task description.

    Works on any COCO class — person, apple, dog, car, etc. VLM is asked
    whether the cropped object matches the attributes in the task
    description (colors, sizes, clothing, state, etc.).

    Args:
        frame: Full JPEG frame
        bbox: Bounding box {x1, y1, x2, y2}
        task_goal: User's natural language description
            (e.g. "person wearing blue shirt", "red apple", "large dog")
        label: YOLO-detected class name (for the prompt). Defaults to "object".

    Returns:
        True if the object matches the description
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

        subject = label if label else "object"
        msg = HumanMessage(content=[
            {"type": "text", "text": (
                f"Task description: {task_goal}\n\n"
                f"The cropped image below shows a detected {subject}. "
                f"Does it match the description in the task? "
                f"Answer ONLY 'yes' or 'no'."
            )},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ])

        response = await vlm.ainvoke([msg])
        answer = response.content.strip().lower()
        matched = answer.startswith("yes")
        logger.info(f"VLM verify ({subject}): '{task_goal}' → {answer} (matched={matched})")
        if emit_fn:
            try:
                verdict = "MATCH" if matched else "no match"
                goal_preview = task_goal if len(task_goal) <= 80 else task_goal[:77].rstrip() + "..."
                await emit_fn(f"VLM verify: {subject} against '{goal_preview}' → {verdict}")
            except Exception:
                pass
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


async def _vlm_dedup_objects(
    track_crops: dict, cooccurrences=None, emit_fn=None,
    cooccur_persistence: int = 3,
) -> tuple[dict, dict]:
    """Use VLM to deduplicate tracked objects and return counts + groups.

    Args:
        track_crops: dict of {label:track_id → JPEG bytes}
        cooccurrences: set of frozenset({key1, key2}) pairs that appeared in the same frame
                       (these are definitely different objects, no need to ask VLM)

    Returns:
        (counts, groups) where:
          counts — dict of {label: unique_count}
          groups — dict of {label: list[list[key]]} — one inner list per
                   unique physical object.  E.g. {"person": [["person:5",
                   "person:18"], ["person:2"], ...]} means track 5 and 18
                   are the SAME person and should be collapsed to one
                   bullet in the final report.  Callers use the first key
                   of each inner list as the "representative" crop.
    """
    import base64
    from collections import defaultdict

    # Normalize cooccurrences: accept either a legacy set-of-pairs or the
    # new dict-of-pair→count.  Only pairs that co-occurred for
    # `cooccur_persistence` frames or more are trusted as "really different
    # objects" — one-off YOLO NMS slips don't count.
    if cooccurrences is None:
        trusted_cooccurs = set()
    elif isinstance(cooccurrences, dict):
        trusted_cooccurs = {
            p for p, cnt in cooccurrences.items()
            if cnt >= cooccur_persistence
        }
    else:
        trusted_cooccurs = set(cooccurrences)
    cooccurrences = trusted_cooccurs

    # Group crops by label
    label_groups = defaultdict(list)
    for key, crop_bytes in track_crops.items():
        label = key.split(":")[0]
        label_groups[label].append((key, crop_bytes))

    result_counts = {}
    result_groups: dict = {}  # label → list[list[key]]

    if emit_fn and label_groups:
        classes_str = ", ".join(f"{k}×{len(v)}" for k, v in label_groups.items())
        try:
            await emit_fn(f"VLM dedup: starting — {classes_str}")
        except Exception:
            pass

    # Fast path for trivial labels (0-1 crops, or all co-occurred) — no VLM
    # call, no gather needed.  Also pre-compute min_unique so the expensive
    # VLM branch below can run in parallel across all labels.
    from itertools import combinations as _combinations
    from langchain_core.messages import HumanMessage

    vlm_jobs = []  # [(label, crops, min_unique, cooccur_note)]
    for label, crops in label_groups.items():
        if len(crops) <= 1:
            result_counts[label] = len(crops)
            result_groups[label] = [[key] for key, _ in crops]
            continue

        crop_keys = [key for key, _ in crops]
        min_unique = 1
        for size in range(len(crop_keys), 1, -1):
            found = False
            for combo in _combinations(crop_keys, size):
                if all(frozenset({a, b}) in cooccurrences for a, b in _combinations(combo, 2)):
                    min_unique = size
                    found = True
                    break
            if found:
                break

        logger.info(f"VLM dedup: {label} — {len(crops)} tracks, min {min_unique} from co-occurrence")

        if min_unique >= len(crops):
            result_counts[label] = len(crops)
            result_groups[label] = [[key] for key, _ in crops]
            logger.info(f"VLM dedup: {label} — all {len(crops)} co-occurred, skipping VLM")
            continue

        if min_unique > 1:
            cooccur_note = (
                f" Note: at least {min_unique} of these were seen "
                f"simultaneously in one frame, so there are at least "
                f"{min_unique} unique ones."
            )
        else:
            # No crops co-occurred → strong prior that every crop is the
            # SAME physical object seen at different moments/angles/zoom.
            cooccur_note = (
                " None of these appeared in the same frame at the same "
                "time.  This is a STRONG prior that they are all the SAME "
                "physical object seen at different moments, angles, "
                "distances, or lighting — the tracker assigns a fresh ID "
                "each time an object briefly leaves and re-enters view.  "
                "DEFAULT answer: 1.  Only raise the count above 1 if you "
                "can point to a CONCRETE visual difference that could NOT "
                "be explained by angle/distance/lighting — e.g. distinctly "
                "different camouflage patterns, different colors, different "
                "turret/hull shapes, different vehicle classes (a tank vs a "
                "truck), clearly different clothing on a person.  "
                "Partial occlusion, angle change, or different zoom levels "
                "are NEVER enough to justify >1."
            )
        vlm_jobs.append((label, crops, min_unique, cooccur_note))

    async def _dedup_one_label(label, crops, min_unique, cooccur_note):
        vlm = create_vision_llm()
        content = [{"type": "text", "text": (
            f"I have {len(crops)} cropped images of '{label}' detected by a "
            f"robot camera during a short patrol sequence (often under a "
            f"minute). Some are very likely the same {label} seen at "
            f"different times/angles — the tracker frequently loses and "
            f"re-acquires the same object, creating multiple track IDs for "
            f"one physical {label}.\n\n"
            f"GROUP the images: each group must contain all images that "
            f"depict the SAME physical {label}.  Two images belong in the "
            f"same group if they could plausibly be the same {label} seen "
            f"at different moments/angles/distances — identical or very "
            f"similar colors, clothing, camouflage, vehicle shape, etc.  "
            f"Only place them in different groups when you can point to a "
            f"CONCRETE visual difference that could NOT be explained by "
            f"angle/distance/lighting (distinct clothing colors for people, "
            f"different vehicle classes, clearly different camouflage "
            f"patterns).\n\n"
            f"Partial occlusion, angle change, or different zoom are "
            f"NEVER enough to split into separate groups.{cooccur_note}\n\n"
            f"Respond with ONLY a JSON array of arrays of 1-based image "
            f"indices.  Example for 5 images where 1+3 are the same and "
            f"2+4+5 are the same: [[1,3],[2,4,5]].  If every image is "
            f"different: [[1],[2],[3],[4],[5]].  If all are the same one "
            f"object: [[1,2,3,4,5]].  No prose, just the JSON."
        )}]
        for i, (key, crop_bytes) in enumerate(crops):
            img_b64 = base64.b64encode(crop_bytes).decode()
            content.append({"type": "text", "text": f"Image {i+1} (track: {key}):"})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
            })
        try:
            response = await vlm.ainvoke([HumanMessage(content=content)])
            answer = response.content.strip()
            import json as _json
            import re as _re
            m = _re.search(r"\[\s*\[.*?\]\s*\]", answer, _re.DOTALL)
            groups: list = []
            if m:
                try:
                    raw_groups = _json.loads(m.group(0))
                    if isinstance(raw_groups, list):
                        seen_idx = set()
                        for g in raw_groups:
                            if not isinstance(g, list):
                                continue
                            cluster = []
                            for idx in g:
                                try:
                                    i0 = int(idx) - 1
                                except (TypeError, ValueError):
                                    continue
                                if 0 <= i0 < len(crops) and i0 not in seen_idx:
                                    seen_idx.add(i0)
                                    cluster.append(crops[i0][0])
                            if cluster:
                                groups.append(cluster)
                        for i0, (k, _) in enumerate(crops):
                            if i0 not in seen_idx:
                                groups.append([k])
                except Exception as e:
                    logger.warning(f"VLM dedup: JSON parse failed for {label}: {e}")
            if not groups:
                nums = _re.findall(r'\d+', answer)
                count = int(nums[0]) if nums else len(crops)
                count = max(count, min_unique)
                crop_keys_only = [k for k, _ in crops]
                groups = [[k] for k in crop_keys_only[:count]]
                if len(crop_keys_only) > count and groups:
                    groups[0].extend(crop_keys_only[count:])
            while len(groups) < min_unique and any(len(g) > 1 for g in groups):
                largest_idx = max(range(len(groups)), key=lambda i: len(groups[i]))
                largest = groups[largest_idx]
                groups[largest_idx] = largest[:1]
                for k in largest[1:]:
                    groups.append([k])
                if len(groups) >= min_unique:
                    break
            count = len(groups)
            logger.info(
                f"VLM dedup: {label} — {len(crops)} tracks → {count} unique "
                f"(min {min_unique}); groups={groups}"
            )
            return label, count, groups
        except Exception as e:
            logger.warning(f"VLM dedup failed for {label}: {e}, falling back to track count")
            return label, max(min_unique, len(crops)), [[k] for k, _ in crops]

    if vlm_jobs:
        # One VLM call per label in parallel — all jobs are independent.
        vlm_results = await asyncio.gather(*[_dedup_one_label(*j) for j in vlm_jobs])
        for label, count, groups in vlm_results:
            result_counts[label] = count
            result_groups[label] = groups

    if emit_fn and result_counts:
        summary = ", ".join(f"{k}={v}" for k, v in result_counts.items())
        try:
            await emit_fn(f"VLM dedup: result — {summary}")
        except Exception:
            pass
    return result_counts, result_groups


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

    # Extra guidance when the candidates include vehicle-class ambiguities
    # that the COCO-style tracker routinely gets wrong — a wheeled Humvee
    # with a roof-mounted gun keeps flip-flopping between 'truck' and 'tank'.
    vehicle_labels = {"tank", "truck", "car", "bus"}
    has_vehicle_conflict = (
        sum(1 for l in candidate_labels if l.lower() in vehicle_labels) >= 2
    )
    vehicle_guidance = ""
    if has_vehicle_conflict:
        vehicle_guidance = (
            "\n\nIMPORTANT — vehicle disambiguation rules:\n"
            "• TANK = continuous metal TRACKS/TREADS on both sides, NO wheels visible, "
            "large rotating TURRET with a long main cannon.\n"
            "• TRUCK = WHEELS (four or more, rubber tires), boxy cargo/cabin. "
            "A Humvee, armored personnel transport, or any military-style vehicle with "
            "a ROOF-MOUNTED gun but NO turret-with-cannon is still a TRUCK, NOT a tank.\n"
            "• CAR = WHEELS, smaller passenger vehicle (sedan, hatchback, coupe).\n"
            "If the vehicle has WHEELS, it is NEVER a tank. When in doubt between tank "
            "and truck, answer TRUCK — false positives on 'tank' are more costly.\n"
        )

    msg = HumanMessage(content=[
        {"type": "text", "text": (
            f"The detection system is confused about this object. "
            f"Is this a {labels_str}? Reply with ONLY one of: "
            f"{', '.join(candidate_labels)}."
            f"{vehicle_guidance}"
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


async def _vlm_describe_scene(frame_bytes: bytes, task_goal: str) -> str:
    """Use the VLM to describe the overall scene / environment in a full frame.

    Input is a FULL JPEG frame (not a crop) so the VLM can reason about the
    setting as a whole — room type, indoor/outdoor, layout, lighting, general
    context — rather than individual object appearance.
    """
    import base64
    from langchain_core.messages import HumanMessage

    vlm = create_vision_llm()
    img_b64 = base64.b64encode(frame_bytes).decode()
    # Prompt is deliberately scene-centric. The task_goal is included so a user
    # prompt like "describe where you are briefly" can tune length/tone.
    prompt_text = (
        "Describe the ENVIRONMENT / SCENE shown in this image in 2-3 sentences. "
        "Focus on: location type (indoor/outdoor, street/room/corridor/lab/etc.), "
        "overall layout, lighting, notable features of the setting, and general "
        "context. Do NOT enumerate or describe individual objects' clothing or "
        "appearance — that is handled separately. Be factual and concise."
    )
    if task_goal:
        prompt_text += f"\n\nUser's original request: {task_goal!r}"
    msg = HumanMessage(content=[
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
    ])
    try:
        response = await vlm.ainvoke([msg])
        return response.content.strip()
    except Exception as e:
        logger.warning(f"VLM describe_scene failed: {e}")
        return ""


async def _vlm_group_cross_label(
    crops: dict, emit_fn=None
) -> list[list[str]]:
    """Cross-label grouping pass — given a mixed-label dict of crops, return
    groups of keys the VLM believes represent the SAME physical object.

    Used specifically for the vehicle family (tank/truck/car/bus) where
    YOLO's inconsistent labeling can split one physical Humvee across two
    or more YOLO classes.  The per-label dedup can't merge these because
    it only compares within one label.

    Returns list[list[key]] — one inner list per physical object.  On any
    parse failure, falls back to each key being its own group (no merge).
    """
    import base64
    import json as _json
    import re as _re
    from langchain_core.messages import HumanMessage

    if not crops:
        return []
    if len(crops) == 1:
        return [[next(iter(crops))]]

    items = list(crops.items())  # [(key, bytes), ...]
    vlm = create_vision_llm()

    content = [{"type": "text", "text": (
        f"I have {len(items)} cropped images of vehicles detected by a "
        f"robot camera during a short patrol sequence.  The object "
        f"detector is inconsistent and may label the same physical "
        f"vehicle as different classes (tank/truck/car/bus) across frames "
        f"— e.g. a Humvee might appear as 'truck' from one angle and "
        f"'bus' from another.\n\n"
        f"GROUP the images: each group contains all crops that depict "
        f"the SAME physical vehicle, regardless of what label the "
        f"detector assigned.  Two crops belong in the same group if they "
        f"could plausibly be the same vehicle at different moments/"
        f"angles/distances — same colors, same camouflage, same general "
        f"silhouette.  Only split into different groups when you can "
        f"point to a CONCRETE visual difference: different camouflage "
        f"patterns, clearly different colors, different chassis type "
        f"(tracked vs wheeled), different vehicle classes (e.g. a tank "
        f"vs a Humvee).\n\n"
        f"Partial occlusion, angle change, or different zoom are NEVER "
        f"enough to split into separate groups.\n\n"
        f"Respond with ONLY a JSON array of arrays of 1-based image "
        f"indices.  Example: [[1,3,5],[2,4]].  No prose."
    )}]
    for i, (key, crop_bytes) in enumerate(items):
        img_b64 = base64.b64encode(crop_bytes).decode()
        content.append({
            "type": "text",
            "text": f"Image {i+1} (track: {key}):"
        })
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
        })
    try:
        response = await vlm.ainvoke([HumanMessage(content=content)])
        answer = response.content.strip()
        m = _re.search(r"\[\s*\[.*?\]\s*\]", answer, _re.DOTALL)
        if not m:
            logger.info(f"VLM vehicle group: no JSON in answer → {answer[:200]}")
            return [[k] for k, _ in items]
        raw = _json.loads(m.group(0))
        if not isinstance(raw, list):
            return [[k] for k, _ in items]
        groups: list[list[str]] = []
        seen = set()
        for g in raw:
            if not isinstance(g, list):
                continue
            cluster = []
            for idx in g:
                try:
                    i0 = int(idx) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= i0 < len(items) and i0 not in seen:
                    seen.add(i0)
                    cluster.append(items[i0][0])
            if cluster:
                groups.append(cluster)
        # Anything not grouped → singleton
        for i0 in range(len(items)):
            if i0 not in seen:
                groups.append([items[i0][0]])
        logger.info(f"VLM vehicle group: {len(items)} reps → {len(groups)} physical vehicles; groups={groups}")
        return groups
    except Exception as e:
        logger.warning(f"VLM cross-label group failed: {e}")
        return [[k] for k, _ in items]


async def _vlm_casualty_sweep(
    frame_bytes: bytes, known_person_descriptions: list[str]
) -> list[dict]:
    """Full-frame VLM pass looking for casualties the object detector missed.

    YOLO/COCO's 'person' class was trained almost exclusively on upright
    subjects — a body lying flat, severed torsos, dismembered limbs, or
    partial bodies on the ground often fall outside its distribution and
    go undetected.  VLM, being a general vision model, has no such
    constraint: it can see a pink half-torso on the floor the same way a
    human would.

    We hand the VLM the full frame plus a list of persons the object
    detector DID find (so it can skip those) and ask it to enumerate any
    additional downed / severed / partial / bloodied human figures.

    Returns a list of dicts with keys:
      description    — free-form 1-2 sentence description (for display)
      body_integrity — 'INCOMPLETE — <what>' or 'intact'
      posture        — 'lying/prone', 'slumped', etc.
      injuries       — 'none' or short phrase
      colors         — short color phrase
    """
    import base64
    import json
    import re
    from langchain_core.messages import HumanMessage

    vlm = create_vision_llm()
    img_b64 = base64.b64encode(frame_bytes).decode()

    known_block = ""
    if known_person_descriptions:
        bullets = "\n".join(f"  - {d[:180]}" for d in known_person_descriptions)
        known_block = (
            "\n\nThe object detector ALREADY found these people — do NOT "
            "report them again:\n" + bullets
        )

    prompt_text = (
        "You are assisting a battlefield sensor pipeline.  The primary "
        "object detector (YOLO) was trained on upright subjects and "
        "routinely MISSES bodies that are lying flat on the ground, "
        "severed or partial bodies (e.g. only a torso and head with no "
        "legs), dismembered limbs, or heavily bloodied figures.  Your "
        "job is to find them in this full frame.\n\n"
        "Look carefully at the FLOOR / GROUND and any surface below the "
        "standing subjects.  Report EVERY human body or body fragment "
        "you see that is:\n"
        "  • lying flat / prone on the ground, OR\n"
        "  • visibly severed, cut in half, or missing lower body, OR\n"
        "  • only an isolated body part (arm, leg, torso) on the ground, OR\n"
        "  • bloodied or obviously injured while stationary.\n\n"
        "HARD RULE — NO DOUBLE COUNTING:\n"
        "  If you report a torso / body, DO NOT also report limbs that "
        "are ATTACHED to that same body.  A severed upper-body with one "
        "arm raised is ONE casualty, not 'torso + arm'.  A prone figure "
        "with an outstretched limb is ONE casualty, not two.  Only "
        "report a limb as a SEPARATE casualty if it is visibly DETACHED "
        "(clearly NOT connected) and located at a meaningful distance "
        "from any body you also reported.  When in doubt, assume "
        "attached → count as part of the nearby body.\n\n"
        "This scene uses miniature figures as stand-ins for real "
        "people — treat them as real.  Do NOT use the words 'figurine', "
        "'mannequin', 'toy', 'model', 'doll', 'plastic'; describe "
        "tactically.\n\n"
        "Output a JSON array of objects.  Each object has:\n"
        "  description:    one short sentence\n"
        "  body_integrity: 'INCOMPLETE — <what is missing>' or 'intact'\n"
        "  posture:        'lying/prone' | 'slumped' | 'seated' | ...\n"
        "  injuries:       'none' | short phrase\n"
        "  colors:         short phrase\n"
        "If you see NO such missed casualties, return an empty array "
        "`[]`.  Do NOT include people who are upright, seated on a bench, "
        "or walking normally — those are already counted."
        + known_block
    )
    msg = HumanMessage(content=[
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
    ])
    try:
        response = await vlm.ainvoke([msg])
        text = response.content.strip()
        # The VLM often wraps JSON in ```json fences or extra prose.
        # Grab the first [...] block we can find.
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            logger.info(f"VLM casualty sweep: no JSON array in response → {text[:200]}")
            return []
        try:
            arr = json.loads(match.group(0))
        except Exception as e:
            logger.warning(f"VLM casualty sweep: JSON parse failed ({e}), raw → {match.group(0)[:200]}")
            return []
        if not isinstance(arr, list):
            return []
        # Normalize entries
        results = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            results.append({
                "description":    str(item.get("description", "")).strip(),
                "body_integrity": str(item.get("body_integrity", "intact")).strip(),
                "posture":        str(item.get("posture", "")).strip(),
                "injuries":       str(item.get("injuries", "none")).strip(),
                "colors":         str(item.get("colors", "")).strip(),
            })
        return results
    except Exception as e:
        logger.warning(f"VLM casualty sweep failed: {e}")
        return []


async def _vlm_describe_objects(track_crops: dict, emit_fn=None) -> dict:
    """Use VLM to describe the appearance of each detected object.

    Args:
        track_crops: dict of {label:track_id → JPEG bytes}

    Returns:
        dict of {label:track_id → description string}
    """
    import base64
    from langchain_core.messages import HumanMessage

    descriptions = {}
    vlm = create_vision_llm()

    if emit_fn and track_crops:
        try:
            await emit_fn(f"VLM describe: analyzing {len(track_crops)} unique object(s) in parallel...")
        except Exception:
            pass

    # ── Per-object coroutine — gets gather()'d so all VLMs run concurrently ──
    async def _describe_one(key: str, crop_bytes: bytes):
        label = key.split(":")[0]
        img_b64 = base64.b64encode(crop_bytes).decode()
        posture_hint = ""
        if label == "person":
            posture_hint = (
                "\n\n"
                "MANDATORY STRUCTURED OUTPUT — answer EVERY line below, in "
                "this exact order, each on its own line. Do NOT skip a line "
                "even if the answer is 'intact' or 'none':\n"
                "  Body parts visible: <list: head / torso / arms (left/right) / "
                "legs (left/right)>\n"
                "  Body integrity: <intact | INCOMPLETE — describe what is "
                "missing, e.g. 'no lower body / severed at waist', 'left arm "
                "missing', 'only upper torso and head'. Distinguish truly "
                "missing parts from three NON-missing cases — only call it "
                "INCOMPLETE when the body ITSELF visibly ends:\n"
                "      (1) parts merely occluded by an object (wall/table/"
                "floor/bench hiding them) → intact\n"
                "      (2) parts outside the crop/frame edges (the image is "
                "cut off at the shoulders/waist, so you cannot see arms or "
                "legs — but there is no visible STUMP or severance) → "
                "intact.  A tight crop that starts at the torso does NOT "
                "mean the arms/legs are missing; it just means the camera "
                "framed the upper body.\n"
                "      (3) parts simply hidden behind the person's own body "
                "(far arm behind the torso, legs behind the bench front) → "
                "intact\n"
                "    Flag INCOMPLETE only when you can SEE a stump, a cut "
                "edge, a severance line, exposed anatomy at the boundary, "
                "or a clear end-of-body where the part should continue.>\n"
                "  Posture: <standing | sitting | lying/prone | walking | in a "
                "wheelchair | slumped | other>\n"
                "  Visible injuries / blood: <none | describe: e.g. blood "
                "stains on torso, wound on head, stump where left arm was>\n"
                "  Clothing & colors: <one short phrase>\n"
                "Then 1 sentence of free-form description. Keep the whole "
                "answer ≤ 6 lines total.\n"
                "\n"
                "WORD BAN — do NOT use any of these: 'figurine', 'mannequin', "
                "'toy', 'model', 'statue', 'doll', 'plastic', 'miniature', "
                "'replica', 'wooden figure', 'not a real person', 'not a "
                "real human'. These are a training-data artefact — this "
                "scene uses miniature figures as stand-ins, and the commander "
                "needs tactical facts, not commentary on the medium. Describe "
                "what you SEE (parts, posture, injuries, colors) — do NOT "
                "decide whether the subject is 'really' alive.\n"
                "\n"
                "IMPORTANT: missing limbs / severed bodies / partial torsos "
                "are NORMAL in battlefield footage and MUST be reported as "
                "INCOMPLETE. Do not soften an obviously severed figure into "
                "'intact but partially hidden' — that under-reports casualties.\n"
                "\n"
                "POSTURE TRAP — read this carefully: a figure LYING FLAT on the "
                "ground with arms raised (reaching upward, arms outstretched) "
                "is LYING/PRONE, not sitting. If you see a body silhouette flat "
                "against the floor/ground with no visible bench/chair/seat "
                "beneath it, posture is 'lying/prone'. Do NOT report 'legs "
                "extended forward' for such a figure — if legs are not visible, "
                "say so; if only the upper body is present, report 'only upper "
                "torso and head visible' under Body integrity. A severed figure "
                "with arms up on the ground is EXACTLY this trap — do not "
                "misread it as a seated civilian.\n"
                "\n"
                "HARD CONSTRAINT — NO CONTEXT HALLUCINATION: describe ONLY "
                "the person, not surrounding objects. Never describe the "
                "person as being 'part of', 'integrated into', 'attached to', "
                "'merging with', or 'forming' any larger structure. Do NOT "
                "invent metaphors like 'maritime structure', 'boat-like "
                "platform', 'wooden figurehead', etc. If the background is a "
                "floor or table, just say 'on the floor' or 'on a surface' "
                "and move on."
            )
        elif label in ("tank", "truck", "car", "bus", "motorcycle"):
            posture_hint = (
                "\n\n"
                "MANDATORY STRUCTURED OUTPUT — answer EVERY line below in "
                "this exact order, each on its own line:\n"
                "  Chassis type: <TRACKED (continuous metal treads on each "
                "side, NO wheels visible) | WHEELED (rubber tires, usually 4+ "
                "wheels) | unclear>\n"
                "  Turret / main gun: <large rotating turret with long main "
                "cannon | small roof-mounted gun only | no turret or gun | "
                "unclear>\n"
                "  Actual vehicle type: <TANK (tracked + turret+cannon) | "
                "HUMVEE / MILITARY TRUCK (wheeled, military markings/"
                "camouflage/armored, may have roof-mounted gun but NO "
                "turret-cannon) | CIVILIAN TRUCK / PICKUP | CIVILIAN CAR / "
                "SEDAN / SUV | OTHER>\n"
                "  Colors / markings: <short phrase, e.g. 'olive-green "
                "camouflage', 'tan sand-color, military star marking', "
                "'white civilian sedan'>\n"
                "  Orientation / state: <short phrase, e.g. 'turret facing "
                "west, stationary', 'parked facing road'>\n"
                "Then 1 sentence of free-form description. Keep total ≤ 6 "
                "lines.\n"
                "\n"
                "DISAMBIGUATION RULES — READ CAREFULLY:\n"
                "  • If WHEELS are visible, the vehicle is NEVER a tank. A "
                "Humvee / armored transport / military truck with a ROOF-"
                "MOUNTED gun is NOT a tank — it is a HUMVEE / MILITARY TRUCK.\n"
                "  • A TANK requires BOTH continuous tracks AND a turret with "
                "a long main cannon. Missing either → not a tank.\n"
                "  • When uncertain between TANK and MILITARY TRUCK, answer "
                "MILITARY TRUCK — false positives on 'tank' inflate hostile-"
                "armor counts and cost the commander credibility.\n"
            )
        # De-bias the subject noun for vehicle classes — YOLO/COCO has no
        # Humvee / APC class so wheeled military vehicles land in the 'tank'
        # bucket, and priming the VLM with "describe this tank" causes it to
        # confirm 'tracked tank' on a clear Humvee.  For vehicle labels we
        # hand the VLM the generic word 'vehicle' and let the structured
        # output force a Chassis type / Actual vehicle type verdict.  Other
        # classes keep the specific label since it helps accuracy.
        subject = "vehicle" if label in ("tank", "truck", "car", "bus", "motorcycle") else label
        msg = HumanMessage(content=[
            {"type": "text", "text": (
                f"Briefly describe this {subject}'s appearance in 1-2 sentences. "
                f"Include colors, size, distinguishing features, "
                f"and any notable state or posture. Only flag this as 'not a "
                f"{subject}' when the detector has boxed something that clearly "
                f"has no {subject}-like form at all (e.g. an actual object such "
                f"as a traffic light or bush, not a miniature version of a "
                f"{subject}).{posture_hint} Be factual and concise."
            )},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ])

        try:
            response = await vlm.ainvoke([msg])
            desc = response.content.strip()
            # Log full description so diagnostics (e.g. "Body integrity:
            # INCOMPLETE" for a severed body, or "Chassis type: WHEELED" for
            # a mis-IDed Humvee) are visible in the server log. Use explicit
            # line markers so multi-line structured output is easy to read.
            logger.info(
                f"VLM describe: {key} →\n"
                f"----- begin description -----\n"
                f"{desc}\n"
                f"----- end description -----"
            )
            if emit_fn:
                try:
                    preview = desc if len(desc) <= 220 else desc[:217].rstrip() + "..."
                    await emit_fn(f"VLM describe: {key} → {preview}")
                except Exception:
                    pass
            return key, desc
        except Exception as e:
            logger.warning(f"VLM describe failed for {key}: {e}")
            return key, label

    # Kick every VLM call off in parallel, wait for them all.
    # Total latency drops from N × T (sequential) to ~T (max of N calls).
    results = await asyncio.gather(
        *[_describe_one(k, c) for k, c in track_crops.items()]
    )
    for key, desc in results:
        descriptions[key] = desc

    return descriptions


# ── Post-processing: reconcile YOLO counts with VLM descriptions ──


def _is_misclassified(description: str, yolo_label: str) -> bool:
    """True if the VLM description explicitly OR softly rejects the YOLO class.

    - Hard rejections: 'not a bird', 'is not a person', 'does not appear to be
      a car', 'figurine rather than a bird', etc.
    - Soft rejections for high-stakes labels (tank): 'resembling a tank',
      'tank-like', 'looks like a tank', 'similar to a tank'.  The VLM uses
      these phrasings when it sees something that isn't clearly the class
      — for "tank" specifically we take soft doubt as a rejection because
      false positives on armor are expensive (inflates ARMOR count).

    Label is escaped so multi-word classes ('sports ball') work too.
    """
    if not description or not yolo_label:
        return False
    label = yolo_label.strip().lower()
    label_esc = re.escape(label)
    text = description.lower()

    # Hard rejections — description contradicts the label outright.
    hard = [
        rf"\b(?:not|is\s+not|not\s+actually)\s+an?\s+{label_esc}\b",
        rf"\bdoes(?:n'?t|\s+not)\s+appear\s+to\s+be\s+an?\s+{label_esc}\b",
        rf"\bdoes(?:n'?t|\s+not)\s+show\s+an?\s+{label_esc}\b",
        rf"\bdoes(?:n'?t|\s+not)\s+contain\s+an?\s+{label_esc}\b",
        rf"\brather\s+than\s+an?\s+{label_esc}\b",
        rf"\binstead\s+of\s+an?\s+{label_esc}\b",
        rf"\bno\s+{label_esc}\s+(?:is\s+)?(?:visible|shown|present)\b",
    ]
    if any(re.search(p, text) for p in hard):
        return True

    # Soft rejections — only for classes where over-counting is very costly.
    # "tank" is the canonical case: a Humvee with a roof-mounted gun often
    # comes back as "dark-colored armored vehicle resembling a tank".
    HIGH_STAKES = {"tank"}
    if label in HIGH_STAKES:
        soft = [
            rf"\bresembling\s+an?\s+{label_esc}\b",   # "resembling a tank"
            rf"\b{label_esc}-like\b",                 # "tank-like"
            rf"\bsimilar\s+to\s+an?\s+{label_esc}\b", # "similar to a tank"
            rf"\blooks?\s+like\s+an?\s+{label_esc}\b",# "looks like a tank"
            rf"\bappears?\s+like\s+an?\s+{label_esc}\b",
            # "armored vehicle resembling a tank" — catch armored-vehicle phrasing
            rf"\barmored\s+vehicle\s+(?:resembling|similar|that\s+looks)",
        ]
        if any(re.search(p, text) for p in soft):
            return True

    # NOTE: we deliberately do NOT treat "not a real person / mannequin /
    # figurine" as a rejection.  This mini-city uses miniature figures to
    # represent real subjects — dropping those counts would systematically
    # under-count valid targets.  The describe prompt asks the VLM to stay
    # in character and not use that phrasing; residual slips are handled
    # at the Summary LLM layer, which has its own "REMAIN IN CHARACTER"
    # rule and rewrites any medium-meta-commentary into tactical language.
    return False


def _vehicle_relabel_target(description: str, yolo_label: str) -> Optional[str]:
    """If a vehicle-labeled track's VLM describe clearly identifies it as a
    different vehicle class, return the correct label. Otherwise None.

    Reads the structured 'Actual vehicle type: <X>' line produced by the
    describe prompt, maps it to a target YOLO-compatible bucket, and
    returns that if it differs from the YOLO label.  Handles all common
    mislabel directions:
      tank ↔ truck  (most frequent: COCO has no Humvee/APC)
      car  → tank   (small tanks/APCs sometimes collapse into 'car')
      bus  → truck  (Humvee can look like a short bus from the rear)
      truck → tank  (turreted tracked vehicles occasionally land in 'truck')
    """
    if not description or not yolo_label:
        return None
    text = description.lower()
    yolo = yolo_label.strip().lower()

    VEHICLE_LABELS = {"tank", "truck", "car", "bus"}
    if yolo not in VEHICLE_LABELS:
        return None

    # Extract the VLM's verdict from the structured line it's asked to produce.
    actual = None
    m = re.search(r"actual\s+vehicle\s+type\s*:\s*([^\n]+)", text)
    if m:
        actual = m.group(1).strip()
    chassis = None
    m2 = re.search(r"chassis\s+type\s*:\s*([^\n]+)", text)
    if m2:
        chassis = m2.group(1).strip()

    # Map VLM verdict → target bucket.
    target: Optional[str] = None
    if actual:
        if "tank" in actual and "not" not in actual and "no tank" not in actual:
            target = "tank"
        elif "humvee" in actual or "military truck" in actual or "military-style" in actual:
            target = "truck"
        elif "civilian car" in actual or "sedan" in actual or "suv" in actual or "hatchback" in actual:
            target = "car"
        elif "civilian truck" in actual or "pickup" in actual:
            target = "truck"
    # Fallback: prose signals.  Only use when the structured line is missing
    # or ambiguous so we don't override the VLM's own verdict.
    if target is None:
        if re.search(r"\bhumvee\b", text) or re.search(r"\bmilitary[-\s]+truck\b", text):
            target = "truck"
        elif (
            chassis and "tracked" in chassis and
            re.search(r"(turret|main\s+cannon|main\s+gun)", text)
        ):
            target = "tank"

    if target and target != yolo:
        return target
    return None


def apply_vlm_corrections_to_counts(
    descriptions: dict, dedup_counts: dict
) -> tuple[dict, list, dict]:
    """Post-process VLM describe output to clean up counts.

    Two kinds of corrections, both applied PROPORTIONALLY — the describe
    output is per-crop (multiple crops can represent one unique vehicle),
    but dedup_counts is per-unique-vehicle.  So for N crops of label L
    where K of them are rejected/relabeled, we migrate round(K/N * count)
    units, not K.  Otherwise multiple crops of the same Humvee would
    decrement 'tank' once per crop and wipe the bucket.

      1. Hard rejection — VLM said 'not a <label>' for a crop.
      2. Vehicle re-label — VLM says 'tank' crop is actually wheeled/Humvee.

    Returns:
      (corrected_counts, corrections_log, key_renames)
        corrections_log — list of (example_keys_str, reason_string) entries,
          one per aggregated migration pair, for display.
        key_renames — dict of {old_key: new_key} for vlm_descriptions so
          the Summary LLM renders bullets under the correct class.  ALL
          migrated crops get renamed (not just the example subset) so
          the label prefix stays consistent; Summary caps bullet count
          at dedup_counts anyway.
    """
    if not descriptions or not dedup_counts:
        return dict(dedup_counts), [], {}

    from collections import defaultdict

    # Tally per-label: how many crops total, how many want to migrate or reject.
    crops_per_label: dict = defaultdict(int)
    reject_per_label: dict = defaultdict(list)   # label → [track_key, ...]
    migrate_per_pair: dict = defaultdict(list)   # (src, dst) → [track_key, ...]

    for key, desc in descriptions.items():
        label = key.split(":")[0]
        crops_per_label[label] += 1
        if _is_misclassified(desc, label):
            reject_per_label[label].append(key)
            continue
        new_label = _vehicle_relabel_target(desc, label)
        if new_label and new_label != label:
            migrate_per_pair[(label, new_label)].append(key)

    corrected = dict(dedup_counts)
    corrections = []
    key_renames: dict = {}

    # Apply rejections first, proportionally.
    for label, bad_keys in reject_per_label.items():
        total = crops_per_label.get(label, 0)
        base = dedup_counts.get(label, 0)
        if total <= 0 or base <= 0:
            continue
        drop = round(len(bad_keys) / total * base)
        if drop <= 0:
            continue
        drop = min(drop, corrected.get(label, 0))
        if drop <= 0:
            continue
        corrected[label] -= drop
        if corrected[label] <= 0:
            del corrected[label]
        example_keys = ", ".join(bad_keys[:3]) + (" …" if len(bad_keys) > 3 else "")
        corrections.append((example_keys, f"not a '{label}' ×{drop}"))

    # Apply re-labels, proportionally.
    for (src, dst), migrate_keys in migrate_per_pair.items():
        total = crops_per_label.get(src, 0)
        base = dedup_counts.get(src, 0)
        if total <= 0 or base <= 0:
            continue
        move = round(len(migrate_keys) / total * base)
        if move <= 0:
            continue
        move = min(move, corrected.get(src, 0))
        if move <= 0:
            continue
        corrected[src] -= move
        corrected[dst] = corrected.get(dst, 0) + move
        if corrected[src] <= 0:
            del corrected[src]
        example_keys = ", ".join(migrate_keys[:3]) + (" …" if len(migrate_keys) > 3 else "")
        corrections.append((example_keys, f"'{src}' → '{dst}' ×{move}"))
        # Rename ALL migrated crops so Summary iterates them under the new
        # label.  Dedup cap (handled in graph.py) still limits bullet count.
        for old_k in migrate_keys:
            _, _, tid_str = old_k.partition(":")
            key_renames[old_k] = f"{dst}:{tid_str}"

    return corrected, corrections, key_renames


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
    vlm_verify: bool = False             # VLM verifies attributes (e.g. "wearing green shirt")
    vlm_describe_objects: bool = False   # VLM describes each detected object's appearance
    vlm_describe_scene: bool = False     # VLM describes the overall scene/environment from a full frame
    alarm_on_detection: bool = False  # Watch/alarm mode: emit Alarm: events, don't approach
    duration_s: int = 0  # 0 = use default timeout
    threshold: float = 0.7
    status: str = "pending"
    start_time: float = 0.0
    frames_analyzed: int = 0
    detections: list = field(default_factory=list)
    _seen_track_ids: set = field(default_factory=set)
    _track_crops: dict = field(default_factory=dict)  # track_key → JPEG bytes of cropped detection
    _tid_label_counts: dict = field(default_factory=dict)  # track_id → Counter of labels
    # Counter of frames where two track keys appeared in the SAME frame.
    # Was a set (any co-occurrence → definitely different), but one spurious
    # YOLO double-detection (two overlapping boxes on the same physical
    # object) could permanently mark a pair as different — wrongly forcing
    # dedup's min_unique floor up.  Counting requires a persistence
    # threshold before a pair is trusted as "truly different objects".
    _cooccurrences: dict = field(default_factory=dict)  # frozenset({k1,k2}) → frame_count
    _track_bbox_areas: dict = field(default_factory=dict)  # track_key → bbox area of saved crop
    _track_remapper: TrackRemapper = field(default_factory=TrackRemapper)
    _alarmed_track_keys: set = field(default_factory=set)   # every track that has been alarmed (for dedup)
    _alarm_last_seen: dict = field(default_factory=dict)    # alarm_key → last-seen unix ts (to detect "gone")
    _active_alarm_keys: set = field(default_factory=set)    # currently-visible alarmed tracks
    _scene_frame: Optional[bytes] = None                     # most recent full JPEG frame, for vlm_describe_scene
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
        # For alarm/watch tasks, collect the distinct target LABELS that ever
        # fired an alarm. We strip the :track_id suffix so "apple:1 / apple:3"
        # → "apple" once. This is what the summary LLM should focus on.
        alarm_fired_labels = sorted({
            k.rsplit(":", 1)[0] for k in self._alarmed_track_keys
        }) if self.alarm_on_detection else []

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
            "alarm_on_detection": self.alarm_on_detection,
            "alarm_fired_labels": alarm_fired_labels,
        }
        if hasattr(self, "dedup_counts") and self.dedup_counts:
            result["dedup_counts"] = self.dedup_counts
        if hasattr(self, "vlm_descriptions") and self.vlm_descriptions:
            result["vlm_descriptions"] = self.vlm_descriptions
        if hasattr(self, "scene_description") and self.scene_description:
            result["scene_description"] = self.scene_description
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
                           task_goal: str = "", use_vlm: bool = False,
                           emit_fn=None) -> list[dict]:
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

            # VLM verification: a second-pass attribute filter on any YOLO-matched
            # detection. Runs when use_vlm=True and YOLO already flagged the
            # detection as a target (is_target=True). Works for any COCO class —
            # apple, person, dog, car, etc. — the VLM is asked whether the crop
            # matches the user's task_goal description (colors, clothing, state).
            # If the detection already failed YOLO's class match, skip VLM entirely.
            track_id = d.get("track_id")
            if use_vlm and is_target and d.get("bbox"):
                cache_key = f"{task_goal}:{track_id}" if track_id is not None else None
                if cache_key and cache_key in _vlm_cache:
                    is_target = _vlm_cache[cache_key]
                else:
                    is_target = await _vlm_verify_attribute(frame, d["bbox"], task_goal, d["label"], emit_fn=emit_fn)
                    if cache_key:
                        _vlm_cache[cache_key] = is_target
                # Record VLM confirmation as a separate flag (don't mutate d["label"] —
                # that would leak "(VLM confirmed)" into alarm_key and counting logic).
                if is_target:
                    d["vlm_confirmed"] = True

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
        agent.set_mission(mission.mission_id, mission.task_goal, mission.report_all, mission.duration_s)

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
                # Distinguish user-specified duration (normal completion) from
                # the default-timeout case (ambiguous — could be "ran out").
                if mission.duration_s > 0:
                    mission.status = "duration_reached"
                    mission.error = None
                    logger.info(f"Mission {mission.mission_id}: {timeout}s duration reached")
                else:
                    mission.status = "timeout"
                    mission.error = "Mission timeout"
                    logger.warning(f"Mission {mission.mission_id}: default timeout after {timeout}s")
                break

            frame = await udp_receiver.wait_for_frame(timeout=2.0)
            if frame is None:
                continue

            mission.frames_analyzed += 1
            # Keep the latest full JPEG frame around — used by (a) scene-
            # level VLM description when requested, and (b) the casualty
            # sweep that runs whenever object-describe is on, to catch
            # downed/severed bodies YOLO missed.  Always overwriting gives
            # us a frame from after detections have stabilized rather than
            # the instant the mission started.
            if mission.vlm_describe_scene or mission.vlm_describe_objects:
                mission._scene_frame = frame

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
                # When using a lowered person threshold, apply a higher bar for
                # non-person classes to suppress false positives (e.g. bicycle
                # wheels misclassified as skateboard).
                _GENERAL_THRESHOLD = 0.55
                if mission.threshold < _GENERAL_THRESHOLD:
                    yolo_result["detections"] = [
                        d for d in yolo_result["detections"]
                        if d["label"] == "person"
                        or d["confidence"] >= _GENERAL_THRESHOLD
                    ]
                annotated = yolo_result.get("annotated_jpeg")
                if annotated:
                    _latest_annotated[mission.agent_id] = annotated
                _latest_detections[mission.agent_id] = {
                    "detections": yolo_result.get("detections", []),
                    "image_width": yolo_result.get("image_width", 640),
                    "image_height": yolo_result.get("image_height", 480),
                }

            # LOCALIZATION: AprilTag position update block — disabled.
            # See unused/detection/apriltag_detector.py + unused/infra/position_tracker.py.
            # try:
            #     tags = await asyncio.to_thread(detect_tags, frame)
            # except Exception as e:
            #     logger.warning(f"AprilTag detection failed: {type(e).__name__}: {e}")
            #     tags = []
            #
            # if tags:
            #     best = max(tags, key=lambda t: t.get("decision_margin", 0.0))
            #     tag_id = best["tag_id"]
            #     info = get_tag_info(tag_id)
            #     if info and info.get("pos"):
            #         try:
            #             update_robot_position(
            #                 mission.agent_id,
            #                 tag_id=tag_id,
            #                 pos=tuple(info["pos"]),
            #                 zone=info.get("zone"),
            #             )
            #         except Exception as e:
            #             logger.error(f"Failed to update robot position from tag#{tag_id}: {e}")
            #     elif tag_id not in _logged_unknown_tags:
            #         logger.warning(
            #             f"AprilTag #{tag_id} detected but not in tag_positions.json; "
            #             "add it to enable localization"
            #         )
            #         _logged_unknown_tags.add(tag_id)

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
                emit_fn=agent._emit,
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

            # Record co-occurrences: track keys that appear in same frame →
            # definitely different objects.  EXCLUDE pairs whose bboxes
            # overlap heavily (IoU > 0.3) — that's YOLO NMS failing and
            # emitting two boxes on the same physical object, not two
            # different objects.  Counting those as co-occurrence would
            # force the dedup min_unique floor up incorrectly.
            from collections import defaultdict as _dd
            frame_keys_by_label = _dd(list)
            frame_bbox_by_key: dict = {}
            for d in detections:
                tid = d.get("track_id")
                if tid is None:
                    continue
                lbl = d.get("label", "unknown")
                k = f"{lbl}:{tid}"
                frame_keys_by_label[lbl].append(k)
                if d.get("bbox"):
                    frame_bbox_by_key[k] = d["bbox"]

            def _bbox_iou(a, b):
                ix1 = max(a["x1"], b["x1"]); iy1 = max(a["y1"], b["y1"])
                ix2 = min(a["x2"], b["x2"]); iy2 = min(a["y2"], b["y2"])
                inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                if inter == 0:
                    return 0.0
                area_a = (a["x2"] - a["x1"]) * (a["y2"] - a["y1"])
                area_b = (b["x2"] - b["x1"]) * (b["y2"] - b["y1"])
                union = area_a + area_b - inter
                return inter / union if union > 0 else 0.0

            for label, keys in frame_keys_by_label.items():
                if len(keys) > 1:
                    for i in range(len(keys)):
                        for j in range(i + 1, len(keys)):
                            ki, kj = keys[i], keys[j]
                            bi = frame_bbox_by_key.get(ki)
                            bj = frame_bbox_by_key.get(kj)
                            if bi and bj and _bbox_iou(bi, bj) > 0.3:
                                continue  # same physical object, NMS slip
                            pair = frozenset({ki, kj})
                            mission._cooccurrences[pair] = (
                                mission._cooccurrences.get(pair, 0) + 1
                            )

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

            # Alarm mode: emit an Alarm: reasoning step for each NEW target track,
            # update "last seen" timestamps, and emit AlarmCleared when a previously
            # alarmed track has been gone from view for more than ALARM_STALE_SEC.
            if mission.alarm_on_detection:
                _alarm_now = time.time()
                ALARM_STALE_SEC = 2.0
                current_frame_keys = set()
                for d in detections:
                    if not d.get("is_target"):
                        continue
                    tid = d.get("track_id")
                    label = d.get("label", "unknown")
                    # track_id == -1 means ByteTrack could not assign a track
                    # (e.g. after a brief disappearance). Treat it as unknown so
                    # the user-visible key stays clean.
                    tid_part = str(tid) if tid is not None and tid >= 0 else "?"
                    alarm_key = f"{label}:{tid_part}"
                    current_frame_keys.add(alarm_key)
                    mission._alarm_last_seen[alarm_key] = _alarm_now
                    conf = d.get("confidence", 0)
                    target_desc = mission.target_name or label

                    if alarm_key not in mission._alarmed_track_keys:
                        # First-ever sighting for this track
                        mission._alarmed_track_keys.add(alarm_key)
                        mission._active_alarm_keys.add(alarm_key)
                        alarm_msg = (
                            f"Alarm: {target_desc} detected "
                            f"[key={alarm_key}] (confidence={conf:.0%})"
                        )
                        logger.warning(f"[{mission.mission_id}] {alarm_msg}")
                        try:
                            await agent._emit(alarm_msg)
                        except Exception as e:
                            logger.debug(f"Alarm emit failed: {e}")
                    elif alarm_key not in mission._active_alarm_keys:
                        # Previously alarmed, had cleared, re-appeared
                        mission._active_alarm_keys.add(alarm_key)
                        alarm_msg = (
                            f"Alarm: {target_desc} re-appeared "
                            f"[key={alarm_key}] (confidence={conf:.0%})"
                        )
                        logger.warning(f"[{mission.mission_id}] {alarm_msg}")
                        try:
                            await agent._emit(alarm_msg)
                        except Exception as e:
                            logger.debug(f"Alarm emit failed: {e}")

                # Emit AlarmCleared for any active alarms that haven't been seen recently
                stale = [k for k in list(mission._active_alarm_keys)
                         if _alarm_now - mission._alarm_last_seen.get(k, 0) > ALARM_STALE_SEC]
                for k in stale:
                    mission._active_alarm_keys.discard(k)
                    try:
                        await agent._emit(f"AlarmCleared: [key={k}] out of view")
                    except Exception as e:
                        logger.debug(f"AlarmCleared emit failed: {e}")

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

        # Resolve label conflicts: same track_id with different labels → use
        # VLM to decide.  Runs in parallel (asyncio.gather) — each conflict
        # is an independent VLM call, serializing them wastes wall time.
        if mission._tid_label_counts:
            conflicts = []  # [(tid, labels, best_crop_key_or_None, label_freq)]
            for tid, label_freq in mission._tid_label_counts.items():
                if len(label_freq) <= 1:
                    continue
                labels = list(label_freq.keys())
                best_crop_key = None
                for label in labels:
                    k = f"{label}:{tid}"
                    if k in mission._track_crops:
                        best_crop_key = k
                        break
                conflicts.append((tid, labels, best_crop_key, label_freq))

            async def _resolve_one(tid, labels, best_crop_key, label_freq):
                if not best_crop_key:
                    return tid, max(label_freq, key=label_freq.get)
                try:
                    r = await _vlm_resolve_label(mission._track_crops[best_crop_key], labels)
                    logger.info(f"Mission {mission.mission_id}: VLM resolved track#{tid}: {labels} → {r}")
                    return tid, r
                except Exception as e:
                    r = max(label_freq, key=label_freq.get)
                    logger.warning(f"Mission {mission.mission_id}: VLM resolve failed for track#{tid}: {e}, using {r}")
                    return tid, r

            if conflicts:
                resolved_pairs = await asyncio.gather(
                    *[_resolve_one(*c) for c in conflicts]
                )
                resolution_map = dict(resolved_pairs)  # tid → resolved_label
                for tid, labels, _best, _freq in conflicts:
                    resolved = resolution_map[tid]
                    for label in labels:
                        if label != resolved:
                            bad_key = f"{label}:{tid}"
                            mission._track_crops.pop(bad_key, None)
                            mission._seen_track_ids.discard(bad_key)
                            mission.detections = [
                                d for d in mission.detections
                                if not (d.get("track_id") == tid and d.get("label") == label)
                            ]

        # Diagnostic: how many track crops survived label-conflict resolution?
        # Helps answer "did YOLO even see it?" before we blame VLM.
        if mission._track_crops:
            from collections import Counter as _C
            _label_counter = _C(k.split(":", 1)[0] for k in mission._track_crops)
            logger.info(
                f"Mission {mission.mission_id}: track crops before VLM dedup — "
                f"{dict(_label_counter)} | keys={sorted(mission._track_crops.keys())}"
            )
            try:
                await agent._emit(
                    f"Tracks before VLM: {dict(_label_counter)}"
                )
            except Exception:
                pass

        # VLM dedup: get accurate object counts by comparing cropped images.
        # Skip in alarm/watch mode — we care whether the target was ever seen,
        # not how many unique instances there were, and VLM dedup is noisy
        # when the same object enters/leaves the frame several times.
        if mission._track_crops and not mission.alarm_on_detection:
            try:
                dedup_counts, dedup_groups = await _vlm_dedup_objects(
                    mission._track_crops, mission._cooccurrences, emit_fn=agent._emit
                )
                mission.dedup_counts = dedup_counts
                mission.dedup_groups = dedup_groups
                logger.info(f"Mission {mission.mission_id}: VLM dedup counts: {dedup_counts}")
            except Exception as e:
                logger.warning(f"Mission {mission.mission_id}: VLM dedup failed: {e}")

        # VLM describe: describe each detected UNIQUE object's appearance.
        # Describe is expensive (one VLM call per crop), so we only run it on
        # the representative crops — one per dedup GROUP, where each group
        # is a set of track IDs VLM confirmed as the same physical object.
        # Within each group we pick the biggest-bbox crop as the rep (closest
        # / clearest view).
        if mission._track_crops and mission.vlm_describe_objects:
            try:
                describe_input = mission._track_crops
                groups = getattr(mission, "dedup_groups", None)
                if groups:
                    representatives: dict = {}
                    for label, grp_list in groups.items():
                        for grp in grp_list:
                            if not grp:
                                continue
                            # Pick rep by largest bbox area within the group.
                            rep = max(
                                grp,
                                key=lambda kk: mission._track_bbox_areas.get(kk, 0),
                            )
                            if rep in mission._track_crops:
                                representatives[rep] = mission._track_crops[rep]
                    describe_input = representatives
                    logger.info(
                        f"Mission {mission.mission_id}: describe on "
                        f"{len(representatives)} representative crop(s) "
                        f"(vs {len(mission._track_crops)} raw tracks, "
                        f"1 per dedup group)"
                    )
                    try:
                        await agent._emit(
                            f"VLM describe: reduced to {len(representatives)} "
                            f"representative crop(s) from {len(mission._track_crops)} raw tracks"
                        )
                    except Exception:
                        pass
                descriptions = await _vlm_describe_objects(describe_input, emit_fn=agent._emit)
                mission.vlm_descriptions = descriptions
                logger.info(f"Mission {mission.mission_id}: VLM descriptions: {list(descriptions.keys())}")

                # If VLM describe flagged a track as 'not a <YOLO-label>' (e.g.
                # a lying mannequin misread as 'bird'), decrement that label's
                # count so the Summary LLM and Tactical dashboard see clean
                # numbers. Surface the correction in the reasoning trace so the
                # commander can see the self-correction happen.
                if hasattr(mission, "dedup_counts") and mission.dedup_counts:
                    corrected, corrections, key_renames = apply_vlm_corrections_to_counts(
                        descriptions, mission.dedup_counts
                    )
                    if corrections:
                        mission.dedup_counts = corrected
                        # Apply key renames so Summary iterates re-labeled
                        # crops under the correct class prefix.  dedup cap
                        # in graph.py limits bullet count to corrected[dst].
                        for old_k, new_k in key_renames.items():
                            desc = mission.vlm_descriptions.pop(old_k, None)
                            if desc is not None:
                                mission.vlm_descriptions[new_k] = desc
                        corrected_tracks = "; ".join(f"{r}" for _, r in corrections)
                        logger.info(
                            f"Mission {mission.mission_id}: VLM corrected counts: "
                            f"{corrected_tracks} → counts now {corrected}"
                        )
                        try:
                            await agent._emit(
                                f"VLM correction: {corrected_tracks}"
                            )
                            await agent._emit(
                                f"VLM correction: counts now → "
                                + ", ".join(f"{k}={v}" for k, v in corrected.items())
                            )
                        except Exception:
                            pass

                # ── Cross-label vehicle family dedup ──
                # Per-label dedup can leave one physical Humvee counted twice:
                # once under 'truck' (tracks YOLO called truck) and once under
                # 'bus' or 'car' (tracks YOLO called bus/car).  The earlier
                # relabel migrations moved those into the correct bucket, but
                # they're still separate GROUPS inside that bucket.  Run a
                # final cross-label VLM pass on all vehicle reps and merge
                # groups the VLM confirms as the same physical vehicle.
                VEHICLE_FAMILY = {"tank", "truck", "car", "bus", "motorcycle"}
                vehicle_reps = {
                    k: mission._track_crops[k]
                    for k in list(mission.vlm_descriptions.keys())
                    if k.split(":", 1)[0] in VEHICLE_FAMILY
                    and k in mission._track_crops
                }
                if len(vehicle_reps) >= 2:
                    try:
                        merge_groups = await _vlm_group_cross_label(
                            vehicle_reps, emit_fn=agent._emit
                        )
                        # merge_groups: list[list[key]] — each inner list is
                        # one physical vehicle, collapsing across labels.
                        # Rebuild dedup_counts for the vehicle family.
                        if merge_groups:
                            from collections import Counter as _Cnt
                            new_vehicle_counts: dict = {}
                            keep_reps: set = set()
                            drop_descs: list = []
                            for grp in merge_groups:
                                if not grp:
                                    continue
                                # Pick the rep whose current label matches
                                # the majority verdict, else the first key.
                                label_votes = _Cnt(k.split(":", 1)[0] for k in grp)
                                # Prefer tank if any member is tank (highest
                                # tactical severity); otherwise majority.
                                if "tank" in label_votes:
                                    chosen_label = "tank"
                                else:
                                    chosen_label = label_votes.most_common(1)[0][0]
                                # Keep the member whose label == chosen_label.
                                keeper = next(
                                    (k for k in grp if k.split(":", 1)[0] == chosen_label),
                                    grp[0],
                                )
                                keep_reps.add(keeper)
                                new_vehicle_counts[chosen_label] = (
                                    new_vehicle_counts.get(chosen_label, 0) + 1
                                )
                                # Any non-keeper members' descriptions get
                                # dropped so Summary doesn't render ghost bullets.
                                for k in grp:
                                    if k != keeper:
                                        drop_descs.append(k)
                            # Replace vehicle-family counts in dedup_counts.
                            for fam_label in list(mission.dedup_counts.keys()):
                                if fam_label in VEHICLE_FAMILY:
                                    mission.dedup_counts.pop(fam_label, None)
                            for fam_label, cnt in new_vehicle_counts.items():
                                mission.dedup_counts[fam_label] = cnt
                            # Drop ghost descriptions.
                            for k in drop_descs:
                                mission.vlm_descriptions.pop(k, None)
                            logger.info(
                                f"Mission {mission.mission_id}: vehicle family "
                                f"merge → kept {len(keep_reps)} rep(s), "
                                f"dropped {len(drop_descs)} duplicate(s); "
                                f"new vehicle counts={new_vehicle_counts}"
                            )
                            try:
                                await agent._emit(
                                    f"VLM vehicle merge: {len(vehicle_reps)} reps → "
                                    f"{len(keep_reps)} unique vehicles; "
                                    f"{', '.join(f'{k}={v}' for k,v in new_vehicle_counts.items())}"
                                )
                            except Exception:
                                pass
                    except Exception as e:
                        logger.warning(
                            f"Mission {mission.mission_id}: vehicle family merge failed: {e}"
                        )
            except Exception as e:
                logger.warning(f"Mission {mission.mission_id}: VLM describe_objects failed: {e}")

        # Casualty sweep: YOLO misses bodies lying flat / severed / dismembered
        # because its 'person' class was trained on upright subjects.  Run a
        # full-frame VLM pass with the list of already-found persons so VLM
        # doesn't double-count, and append any missed casualties as synthetic
        # person tracks — they flow through the Summary LLM's normal KIA /
        # wounded routing via the 'Body integrity: INCOMPLETE' marker.
        if (
            mission.vlm_describe_objects
            and mission._scene_frame is not None
        ):
            try:
                known_person_descs = [
                    d for k, d in getattr(mission, "vlm_descriptions", {}).items()
                    if k.split(":", 1)[0] == "person"
                ]
                sweep = await _vlm_casualty_sweep(
                    mission._scene_frame, known_person_descs
                )
                if sweep:
                    logger.info(
                        f"Mission {mission.mission_id}: casualty sweep found "
                        f"{len(sweep)} missed casualty(ies)"
                    )
                    try:
                        await agent._emit(
                            f"VLM casualty sweep: {len(sweep)} missed casualty(ies) found"
                        )
                    except Exception:
                        pass
                    if not hasattr(mission, "vlm_descriptions") or mission.vlm_descriptions is None:
                        mission.vlm_descriptions = {}
                    if not hasattr(mission, "dedup_counts") or mission.dedup_counts is None:
                        mission.dedup_counts = {}
                    for i, item in enumerate(sweep):
                        synthetic_key = f"person:sweep{i}"
                        # Format structurally so the Summary LLM reads it the
                        # same way as per-object describes.
                        struct_desc = (
                            f"Body parts visible: (full frame - not cropped)\n"
                            f"Body integrity: {item['body_integrity']}\n"
                            f"Posture: {item['posture']}\n"
                            f"Visible injuries / blood: {item['injuries']}\n"
                            f"Clothing & colors: {item['colors']}\n"
                            f"\n{item['description']} (detected via full-frame VLM sweep — YOLO missed)"
                        )
                        mission.vlm_descriptions[synthetic_key] = struct_desc
                        logger.info(
                            f"  sweep add: {synthetic_key} →\n"
                            f"----- begin description -----\n"
                            f"{struct_desc}\n"
                            f"----- end description -----"
                        )
                        try:
                            await agent._emit(
                                f"VLM sweep add: {synthetic_key} — "
                                f"{item['body_integrity']} / {item['posture']}"
                            )
                        except Exception:
                            pass
                    mission.dedup_counts["person"] = (
                        mission.dedup_counts.get("person", 0) + len(sweep)
                    )
            except Exception as e:
                logger.warning(f"Mission {mission.mission_id}: casualty sweep failed: {e}")

        # VLM scene description: describe the overall environment from a full
        # frame. Runs independently of vlm_describe_objects and does NOT require
        # detections — even an empty hallway gets described.
        if mission.vlm_describe_scene and mission._scene_frame:
            try:
                scene_desc = await _vlm_describe_scene(mission._scene_frame, mission.task_goal)
                if scene_desc:
                    mission.scene_description = scene_desc
                    logger.info(f"Mission {mission.mission_id}: scene description: {scene_desc[:100]}")
            except Exception as e:
                logger.warning(f"Mission {mission.mission_id}: VLM describe_scene failed: {e}")

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
