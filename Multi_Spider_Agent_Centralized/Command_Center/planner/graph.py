"""
LangGraph-based Planner for Multi-Robot System.

Dispatches tasks to Robot Agents via A2A JSON-RPC over HTTP.

Architecture:
- Planner discovers Robot Agents via ADS (static config as fallback)
- Planner sends tasks to each Robot Agent's A2A HTTP endpoint
  (a2a-sdk Client → Robot Agent's A2AStarletteApplication)
- Each Robot Agent manages its own CentralPiAgent + Detection Engine
- NATS is still used (by Robot Agents → Gateway) for fire-and-forget
  reasoning/state pub/sub; the planner does not use NATS for tasks.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph


from ..infra.discovery import AgentDiscovery
from ..infra.robot_registry import robot_registry
from ..robot_a2a import a2a_http_port
from .a2a_client import RobotA2AError, get_a2a_client
from .schemas import PlannerState
from .brain import parse_intent, fallback_intent, build_coordinator_prompt

logger = logging.getLogger(__name__)

MAX_REPLANS = 3


# ---------------------------------------------------------------------------
# Node: Parse + Get Online Robots
# ---------------------------------------------------------------------------

async def parse_and_discover(state: PlannerState) -> dict:
    """Parse intent (1 LLM call) and discover online robots via ADS/static."""
    messages_log = state.get("messages", [])

    # Discover online robots via ADS or static config
    discovery = AgentDiscovery()
    discovered = await discovery.get_online_agents()

    # Register discovered robots into registry for state tracking
    for agent_info in discovered:
        robot_registry.register_from_discovery(agent_info)

    # Build online_agents list — capabilities come from registry (via heartbeat),
    # not from discovery. ADS provides id/host/port/tags for selection,
    # heartbeat provides detailed capabilities for execution.
    online_agents = []
    for agent_info in discovered:
        # Skip offline robots
        if agent_info.get("status") == "offline":
            continue
        robot_id = agent_info["id"]
        reg_robot = robot_registry.get_robot(robot_id)
        caps = reg_robot.capabilities if reg_robot else {}
        zone = (reg_robot.zone if reg_robot else "") or agent_info.get("zone", "")
        robot_type = (reg_robot.robot_type if reg_robot else agent_info.get("robot_type", "spider"))
        online_agents.append({
            "id": robot_id,
            "host": agent_info["host"],
            "port": agent_info["port"],
            "camera_port": reg_robot.camera_port if reg_robot else agent_info.get("camera_port", 9000),
            "robot_type": robot_type,
            "capabilities": caps,
            "actions": [a["name"] for a in caps.get("actions", [])],
            "zone": zone,
            "status": "online",
            "busy": agent_info.get("busy", False),
        })

    if state.get("iteration", 1) > 1:
        # On replan, only refresh robot list
        if not online_agents:
            messages_log.append(AIMessage(content="Discovery: no robots online"))
            return {"online_agents": [], "error": "No robots are online", "messages": messages_log}
        messages_log.append(AIMessage(content=f"Discovery: {len(online_agents)} robots online"))
        return {"online_agents": online_agents, "messages": messages_log}

    prompt = state["prompt"]

    # All commands go through LLM for intelligent routing.
    # parse_intent returns a TaskIntent (or None) — VLM flags now live on each
    # Phase directly, so there's no second tuple element to unpack.
    parse_result = await parse_intent(prompt, online_agents)

    if parse_result is None:
        intent = fallback_intent(prompt)
        phase_descs = [f"{p['task_type']}:{p.get('target_type','')}" for p in intent['phases']]
        messages_log.append(AIMessage(content=f"Parse (fallback): [{', '.join(phase_descs)}]"))
    else:
        if not parse_result.is_valid_task:
            reason = parse_result.rejection_reason or "Not a robot task"
            logger.info(f"Parse rejected: {reason}")
            messages_log.append(AIMessage(content=f"Rejected: {reason}"))
            return {"error": f"NOT_VALID_TASK: {reason}", "messages": messages_log}
        intent = parse_result.model_dump()
        phase_descs = []
        for i, ph in enumerate(intent["phases"]):
            desc = f"{ph['task_type']}"
            if ph.get("target_robot_ids"):
                desc += f"→{ph['target_robot_ids']}"
            phase_descs.append(desc)
        messages_log.append(AIMessage(content=f"Parse: {', '.join(phase_descs)}"))

        # Detailed intent for reasoning display
        ph = intent["phases"][0] if intent["phases"] else {}
        details = []
        if ph.get("task_type"): details.append(f"type={ph['task_type']}")
        if ph.get("target_type") and ph["target_type"] != "none": details.append(f"target={ph['target_type']}")
        if ph.get("target_name"): details.append(f"name='{ph['target_name']}'")
        if ph.get("target_zone"): details.append(f"zone={ph['target_zone']}")
        if ph.get("target_robot_ids"): details.append(f"robots={ph['target_robot_ids']}")
        if ph.get("report_all"): details.append("report_all=true")
        if ph.get("alarm_on_detection"): details.append("alarm_on_detection=true")
        if ph.get("vlm_verify"): details.append("vlm_verify=true")
        if ph.get("vlm_describe_objects"): details.append("vlm_describe_objects=true")
        if ph.get("vlm_describe_scene"): details.append("vlm_describe_scene=true")
        if ph.get("duration_s", 0) > 0: details.append(f"duration={ph['duration_s']}s")
        if ph.get("description"): details.append(f"desc='{ph['description']}'")
        messages_log.append(AIMessage(content=f"Intent: {', '.join(details)}"))

        # Surface which vision-LLM capabilities the Planner turned on, and
        # why (keyword-driven). Lets the reasoning panel show commander what
        # perception features were enabled for this request.
        vlm_enabled = []
        if ph.get("vlm_verify"):          vlm_enabled.append("verify (attribute check)")
        if ph.get("vlm_describe_objects"):vlm_enabled.append("describe objects")
        if ph.get("vlm_describe_scene"):  vlm_enabled.append("describe scene")
        if ph.get("alarm_on_detection"):  vlm_enabled.append("alarm watch")
        if vlm_enabled:
            messages_log.append(AIMessage(
                content=f"VLM: enabled → {', '.join(vlm_enabled)}"
            ))

        logger.info(f"Parsed intent: {intent}")

    if not online_agents:
        messages_log.append(AIMessage(content="Discovery: no robots online"))
        return {"intent": intent, "online_agents": [], "error": "No robots are online", "messages": messages_log}

    logger.info(f"Online robots: {[a['id'] for a in online_agents]}")
    messages_log.append(AIMessage(content=f"Discovery: {len(online_agents)} robots online"))
    return {"intent": intent, "online_agents": online_agents, "messages": messages_log}



# ---------------------------------------------------------------------------
# Helpers: resolve robots for a phase
# ---------------------------------------------------------------------------

def _resolve_phase_robots(phase: dict, online: list[dict]) -> list[dict]:
    """Resolve which robots a phase targets."""
    target_ids = phase.get("target_robot_ids", [])
    target_zone = phase.get("target_zone", "")

    if target_ids:
        id_set = set(target_ids)
        return [a for a in online if a["id"] in id_set]
    elif target_zone:
        zone_upper = target_zone.upper()
        return [a for a in online if a.get("zone", "").upper() == zone_upper]
    else:
        return list(online)


# ---------------------------------------------------------------------------
# Node: Execute Phases
# ---------------------------------------------------------------------------

async def execute_phases(state: PlannerState) -> dict:
    """Execute phases, choosing parallel or sequential based on robot overlap.

    Two dispatch strategies:

    1. **Parallel** — when no two phases share a target robot
       (typical for per-robot commands: "robot A wave, robot B sit").
       All phases run concurrently via asyncio.gather.

    2. **Sequential** — when at least one robot appears in multiple phases
       (typical for compound commands: "stand up and report", where all robots
       do phase 1 then phase 2).
       Phases run one-after-another so that "stand up" completes before "report".

    Each robot appears exactly once in the final result (later phases override
    earlier ones for that robot).
    """
    online = state.get("online_agents", [])
    intent = state.get("intent", {})
    prompt = state.get("prompt", "")
    messages_log = state.get("messages", [])

    if not online:
        return {"agent_results": [], "error": "No agents available", "messages": messages_log}

    phases = intent.get("phases", [])
    if not phases:
        phases = [{"target_robot_ids": [], "task_type": "agent", "target_type": "object",
                   "description": prompt}]

    is_multi = len(phases) > 1

    # Resolve robots for each phase once
    phase_robots: list[tuple[dict, list[dict]]] = []
    for phase in phases:
        phase_robots.append((phase, _resolve_phase_robots(phase, online)))

    if not any(r for _, r in phase_robots):
        return {"agent_results": [], "error": "No robots matched any phase", "messages": messages_log}

    # Decide parallel vs sequential: any robot shared between two phases?
    robot_sets = [{r["id"] for r in robs} for _, robs in phase_robots]
    phases_overlap = any(
        robot_sets[i] & robot_sets[j]
        for i in range(len(robot_sets))
        for j in range(i + 1, len(robot_sets))
    )

    strategy = "sequential" if (phases_overlap or len(phases) == 1) else "parallel"
    if is_multi:
        messages_log.append(AIMessage(content=f"Phase strategy: {strategy} ({len(phases)} phases)"))
        logger.info(f"Phase strategy: {strategy} ({len(phases)} phases)")

    async def _run_phase(idx: int, phase: dict, robots: list[dict]) -> dict:
        if not robots:
            target_desc = phase.get("target_robot_ids") or phase.get("target_zone") or "any"
            messages_log.append(AIMessage(
                content=f"Phase {idx+1}/{len(phases)}: no robots match ({target_desc}), skipping"
            ))
            return {"agent_results": []}
        robot_ids = [r["id"] for r in robots]
        report_all = phase.get("report_all", False)
        mode = "report" if report_all else ("coordinated" if len(robots) > 1 and phase["task_type"] == "agent" else "independent")
        label = f"Phase {idx+1}/{len(phases)}" if is_multi else "Execute"
        messages_log.append(AIMessage(content=f"{label}: {phase['task_type']} → {robot_ids} [{mode}]"))
        logger.info(f"{label}: {phase['task_type']} → {robot_ids} mode={mode}")
        phase_prompt = phase.get("description", prompt) if is_multi else prompt
        return await _execute_single_phase(phase, robots, phase_prompt, messages_log)

    results_by_robot: dict = {}

    if strategy == "parallel":
        phase_results = await asyncio.gather(
            *[_run_phase(i, p, r) for i, (p, r) in enumerate(phase_robots)],
            return_exceptions=True,
        )
        for res in phase_results:
            if isinstance(res, Exception):
                logger.warning(f"Parallel phase raised: {res}")
                continue
            for r in res.get("agent_results", []):
                rid = r.get("agent_id")
                if rid:
                    results_by_robot[rid] = r
    else:
        for i, (phase, robots) in enumerate(phase_robots):
            res = await _run_phase(i, phase, robots)
            for r in res.get("agent_results", []):
                rid = r.get("agent_id")
                if rid:
                    results_by_robot[rid] = r

    all_results = list(results_by_robot.values())
    if not all_results:
        return {"agent_results": [], "error": "No results", "messages": messages_log}

    success_count = sum(1 for r in all_results if r.get("status") == "success")
    done_suffix = f" ({len(phases)} phases, {strategy})" if is_multi else ""
    messages_log.append(AIMessage(content=f"Done: {success_count}/{len(all_results)} succeeded{done_suffix}"))

    error = "ALL_AGENTS_FAILED" if success_count == 0 else None
    return {"agent_results": all_results, "error": error, "messages": messages_log}


async def _execute_single_phase(phase: dict, robots: list[dict], prompt: str, messages_log: list) -> dict:
    """Execute one phase across its target robots.

    Agent mode auto-coordination:
      - report_all=true  → parallel (each robot reports independently)
      - report_all=false → first_to_detect (first detection stops others)
    """
    task_type = phase.get("task_type", "direct_control")
    report_all = phase.get("report_all", False)

    if task_type == "agent" and not report_all and len(robots) > 1:
        return await _execute_coordinated_patrol(phase, robots, prompt, messages_log)

    if task_type == "agent":
        # Agent mode: dispatch to all target robots in parallel.
        # Each _dispatch_agent_task appends directly to the SHARED messages_log
        # (a _LiveMessagesList injected by gateway.py that parses every
        # .append() into an immediate comm_event).  This means:
        #   • "Agent: X watching for Y" fires at dispatch time (t≈0 for all)
        #     — Agent Topology marks that robot as active instantly.
        #   • "Agent result: X: ..." fires at THAT robot's completion time
        #     (31s for fastest, 81s for slowest) — topology flips that
        #     specific node to done, one at a time, as they finish.
        # asyncio.gather still collects the final results; the incremental
        # updates happen through the side-effect of messages_log.append.
        if len(robots) == 1:
            return await _dispatch_agent_task(robots[0], phase, prompt, messages_log)
        import time
        _t0 = time.monotonic()
        tasks = [
            asyncio.create_task(
                _dispatch_agent_task(robot, phase, prompt, messages_log)
            )
            for robot in robots
        ]
        merged = []
        for coro in asyncio.as_completed(tasks):
            try:
                r = await coro
            except Exception as e:
                # Isolate failures — don't let one robot's crash lose the
                # already-completed results from other robots.
                logger.warning(f"Phase task raised: {e}")
                continue
            result_list = r.get("agent_results", [])
            merged.extend(result_list)
            rid = result_list[0].get("agent_id") if result_list else "?"
            elapsed = time.monotonic() - _t0
            # Timing marker (purely informational — does NOT match any
            # gateway-side startswith() patterns, so it won't emit a
            # spurious comm_event).
            messages_log.append(AIMessage(
                content=f"Timing: {rid} finished at t+{elapsed:.1f}s"
            ))
        return {"agent_results": merged, "messages": messages_log}
    else:
        # Direct control: dispatch to all target robots in parallel
        # Use original user prompt, not LLM-rewritten description, to avoid semantic drift
        robot_ids = [r["id"] for r in robots]
        messages_log.append(AIMessage(content=f"Execute: direct control → {robot_ids}"))
        coros = [_dispatch_direct_control(robot, prompt) for robot in robots]
        raw_results = await asyncio.gather(*coros, return_exceptions=True)

        results = []
        for robot, result in zip(robots, raw_results):
            if isinstance(result, Exception):
                results.append({"agent_id": robot["id"], "status": "error",
                                "response": str(result), "structured": {}})
                messages_log.append(AIMessage(content=f"Agent result: {robot['id']}: error — {result}"))
            else:
                summary = result.get("execution_result", {}).get("summary", "")
                results.append({
                    "agent_id": robot["id"],
                    "status": result.get("status", "error"),
                    "response": summary,
                    "structured": result.get("execution_result", {}),
                })
                messages_log.append(AIMessage(content=f"Agent result: {robot['id']}: {result.get('status', '?')} — {summary[:100]}"))
        return {"agent_results": results, "messages": messages_log}


async def _cancel_robot(robot_id: str):
    """Send a broadcast-cancel to a Robot Agent via A2A."""
    try:
        client = get_a2a_client()
        result = await client.send_task(
            base_url=_robot_a2a_url(robot_id),
            task_payload={"task_type": "cancel"},
            prompt="cancel",
            timeout=10,
        )
        logger.info(f"Cancel sent to {robot_id}: {result.get('status', '?')}")
    except Exception as e:
        logger.warning(f"Cancel to {robot_id} failed: {e}")


async def _execute_coordinated_patrol(phase: dict, robots: list[dict], prompt: str, messages_log: list) -> dict:
    """Coordinated patrol with ReAct-style LLM coordination.

    LLM controls the coordination loop via tool calls:
    - dispatch_patrol: start patrol on robots
    - wait_for_detection: wait for next robot to report results
    - execute_action: make a robot perform an action
    - cancel_robot: stop a robot's patrol
    - finish: end the coordination

    The LLM sees tool outputs and decides what to do next,
    just like a ReAct agent with tool-calling.
    """
    from .tools import CoordinationToolExecutor, OPENAI_TOOLS

    if len(robots) < 2:
        return await _dispatch_agent_task(robots[0], phase, prompt, messages_log)

    # Create tool executor with coordination state
    executor = CoordinationToolExecutor(
        robots=robots, phase=phase, prompt=prompt,
        dispatch_fn=_dispatch_to_robot_agent,
        cancel_fn=_cancel_robot,
        direct_control_fn=_dispatch_direct_control,
    )

    # ── ReAct loop ──
    system_msg = build_coordinator_prompt(robots)

    from ..config import create_llm
    llm_with_tools = create_llm().bind_tools(OPENAI_TOOLS)

    messages = [
        SystemMessage(content=system_msg),
        HumanMessage(content=f"User command: {prompt}"),
    ]

    MAX_TURNS = 15
    messages_log.append(AIMessage(content=f"Coordination: ReAct loop on {len(robots)} robots"))

    for turn in range(MAX_TURNS):
        response = await llm_with_tools.ainvoke(messages)

        # Add assistant message to history
        messages.append(response)

        # No tool calls → LLM is done (or confused)
        if not response.tool_calls:
            if response.content:
                messages_log.append(AIMessage(content=f"Coordinator: {response.content}"))
                logger.info(f"ReAct coordinator (turn {turn+1}): {response.content}")
            break

        # Execute tool calls
        for tc in response.tool_calls:
            fn_name = tc["name"]
            fn_args = tc["args"]

            logger.info(f"ReAct coordinator (turn {turn+1}): {fn_name}({fn_args})")
            messages_log.append(AIMessage(content=f"Tool: {fn_name}({fn_args})"))

            if fn_name == "finish":
                messages.append(ToolMessage(
                    content=fn_args.get("summary", "done"),
                    tool_call_id=tc["id"],
                ))
                # Build final results from executor state
                for evt in executor.detection_events:
                    zone_report = f"Target detected by {evt['robot_id']} in zone {evt['zone']}"
                    executor.agent_results.append({
                        "agent_id": evt["robot_id"],
                        "status": "success",
                        "response": zone_report,
                        "structured": {
                            "mission": evt["result"].get("mission_result", {}),
                            "detections": evt["detections"],
                            "zone": evt["zone"],
                            "zone_report": zone_report,
                        },
                    })
                if not executor.detection_events:
                    for robot in robots:
                        executor.agent_results.append({
                            "agent_id": robot["id"],
                            "status": "success",
                            "response": "Patrol completed, no target detected",
                            "structured": {"detections": []},
                        })
                return {"agent_results": executor.agent_results}

            # Execute the tool
            try:
                output = await executor.call_tool(fn_name, fn_args)
            except ValueError:
                output = f"Unknown tool: {fn_name}"

            logger.info(f"ReAct tool output: {output[:200]}")
            messages_log.append(AIMessage(content=f"Result: {output[:200]}"))

            messages.append(ToolMessage(
                content=output,
                tool_call_id=tc["id"],
            ))

    # Safety: if loop ended without finish, build results anyway
    logger.warning("ReAct coordination loop ended without finish call")
    for evt in executor.detection_events:
        executor.agent_results.append({
            "agent_id": evt["robot_id"],
            "status": "success",
            "response": f"Target detected in zone {evt['zone']}",
            "structured": {
                "mission": evt["result"].get("mission_result", {}),
                "detections": evt["detections"],
                "zone": evt["zone"],
            },
        })
    if not executor.agent_results:
        for robot in robots:
            executor.agent_results.append({
                "agent_id": robot["id"],
                "status": "success",
                "response": "Patrol completed",
                "structured": {},
            })
    return {"agent_results": executor.agent_results}


import os as _os

_DEFAULT_A2A_HOST = _os.getenv("PLANNER_A2A_HOST_DEFAULT", "localhost")


def _a2a_url_for(robot_id: str, override: Optional[str] = None) -> str:
    """Resolve the A2A HTTP base URL for a given robot.

    Priority:
      1. ``override`` (usually the ``a2a_url`` captured at discovery).
      2. ``http://{PLANNER_A2A_HOST_DEFAULT}:{hash(robot_id)}`` — matches
         the deterministic port scheme used by ``robot_agent.py`` when
         the robot runs on the same host as the planner.
    """
    if override:
        return override
    return f"http://{_DEFAULT_A2A_HOST}:{a2a_http_port(robot_id)}"


def _robot_a2a_url(robot_id: str) -> str:
    """Look up a robot's A2A URL, preferring the one captured at discovery."""
    reg = robot_registry.get_robot(robot_id)
    override = getattr(reg, "a2a_url", None) if reg else None
    return _a2a_url_for(robot_id, override=override)


async def _ping_robot(robot_id: str, timeout: float = 3) -> bool:
    """Quick check that the Robot Agent's A2A endpoint is serving its card."""
    client = get_a2a_client()
    return await client.ping(_robot_a2a_url(robot_id), timeout=timeout)


async def _dispatch_to_robot_agent(robot_id: str, task_payload: dict, timeout: float = 180) -> dict:
    """Send task to Robot Agent via A2A JSON-RPC (HTTP)."""
    if not await _ping_robot(robot_id):
        logger.warning(f"Robot {robot_id} not reachable (A2A ping failed), skipping")
        return {"status": "error", "error": f"Robot {robot_id} not reachable"}

    base_url = _robot_a2a_url(robot_id)
    prompt = task_payload.get("task_goal") or task_payload.get("prompt") or ""

    try:
        client = get_a2a_client()
        result = await client.send_task(
            base_url=base_url,
            task_payload=task_payload,
            prompt=prompt,
            timeout=timeout,
        )
        logger.info(f"A2A dispatch to {robot_id}: {result.get('status', '?')}")
        # Ensure agent_id is present for downstream reflection logic.
        result.setdefault("agent_id", robot_id)
        return result
    except RobotA2AError as e:
        logger.error(f"A2A dispatch to {robot_id} failed: {e}")
        return {"status": "error", "agent_id": robot_id, "error": str(e)}
    except Exception as e:
        logger.exception(f"A2A dispatch to {robot_id} unexpected error: {e}")
        return {"status": "error", "agent_id": robot_id, "error": f"Unexpected: {e}"}


async def _dispatch_agent_task(agent: dict, phase: dict, task_goal: str, messages_log: list) -> dict:
    """Dispatch agent mode task to Robot Agent via NATS."""
    import uuid

    # Safety net: if the user's prompt clearly asks for a watch/alarm task
    # (keywords below) but the LLM forgot to set alarm_on_detection, force it.
    # Also force report_all so the robot stays in observe mode.
    alarm_flag = phase.get("alarm_on_detection", False)
    report_all_flag = phase.get("report_all", False)
    describe_flag = phase.get("vlm_describe_objects", False)
    _ALARM_KW = ("watch", "alarm", "alert", "notify", "warn", "guard", "guardian")
    goal_lower = (task_goal or "").lower()
    if not alarm_flag and any(kw in goal_lower for kw in _ALARM_KW):
        logger.info(f"[{agent['id']}] Prompt keywords → forcing alarm_on_detection=true")
        alarm_flag = True
        report_all_flag = True

    # If the user asks for a report (report_all=true), the result must
    # always be per-object detailed — no "report" vs "describe" split.
    # Force vlm_describe_objects=true so Summary has per-person appearance
    # data to render detailed bullets.  Skip when alarm_on_detection is on
    # (watch mode cares about presence, not appearance).
    if report_all_flag and not alarm_flag and not describe_flag:
        logger.info(f"[{agent['id']}] report_all=true → forcing vlm_describe_objects=true")
        describe_flag = True

    # Reverse rule: for alarm/watch tasks, VLM describe is unnecessary and
    # just adds latency + cost.  LLM sometimes enables it anyway for a pure
    # 'watch for X' prompt with no describe verb — override it off here,
    # unless the user explicitly asked to describe (e.g. 'watch for tank
    # and describe it').  Keyword check is cheap and conservative.
    _DESCRIBE_KW = ("describe", "detail", "appearance", "what does it look",
                    "characterize", "what it looks like")
    if alarm_flag and describe_flag and not any(
        kw in goal_lower for kw in _DESCRIBE_KW
    ):
        logger.info(
            f"[{agent['id']}] alarm_on_detection=true + no describe verb → "
            f"forcing vlm_describe_objects=false"
        )
        describe_flag = False

    task_payload = {
        "task_type": "agent",
        "mission_id": str(uuid.uuid4())[:8],
        "target_type": phase.get("target_type", "any"),
        "target_name": phase.get("target_name", ""),
        "report_all": report_all_flag,
        "vlm_verify": phase.get("vlm_verify", False),
        "vlm_describe_objects": describe_flag,
        "vlm_describe_scene": phase.get("vlm_describe_scene", False),
        "alarm_on_detection": alarm_flag,
        "task_goal": task_goal,
        # User-dictated walking route, e.g. "forward 5 steps, turn left 90".
        # When set, the patrol pattern becomes 'custom' and the Robot Agent
        # walks the route while detection runs in parallel.
        "route_prompt": phase.get("route_prompt"),
    }
    duration = phase.get("duration_s", 0)
    if duration > 0:
        task_payload["duration_s"] = duration

    mode_desc = phase.get("task_type", "agent")
    target_desc = f"'{task_payload['target_name']}'" if task_payload['target_name'] else task_payload['target_type']
    messages_log.append(AIMessage(
        content=f"Agent: {agent['id']} watching for {target_desc}"
    ))
    logger.info(f"Dispatching agent task to {agent['id']}: {task_payload['mission_id']}")

    result = await _dispatch_to_robot_agent(agent["id"], task_payload)

    mission_result = result.get("mission_result", {})
    agent_result = {
        "agent_id": agent["id"],
        "status": result.get("status", "error"),
        "response": (
            f"Agent {mission_result.get('status', '?')}: mode={mode_desc}, target={target_desc}, "
            f"frames={mission_result.get('frames_analyzed', 0)}, elapsed={mission_result.get('elapsed_s', 0)}s, "
            f"detections={len(mission_result.get('detections', []))}"
        ),
        "structured": {"mission": mission_result, "detections": mission_result.get("detections", [])},
    }

    if mission_result.get("match_result"):
        agent_result["response"] += f", match={mission_result['match_result']}"

    # Detailed agent result for reasoning
    det_count = len(mission_result.get("detections", []))
    elapsed = mission_result.get("elapsed_s", 0)
    frames = mission_result.get("frames_analyzed", 0)
    agent_status = mission_result.get("status", "unknown")
    detail = f"{agent['id']}: {agent_status}, {frames} frames, {elapsed:.0f}s, {det_count} detections"
    if mission_result.get("match_result"):
        match = mission_result["match_result"]
        detail += f", found {match.get('label', '?')} ({match.get('confidence', 0):.0%})"
    if mission_result.get("patrol_report", {}).get("summary"):
        detail += f" | Report: {mission_result['patrol_report']['summary'][:100]}"
    messages_log.append(AIMessage(content=f"Agent result: {detail}"))
    return {"agent_results": [agent_result], "messages": messages_log}


async def _dispatch_direct_control(agent_info: dict, prompt: str) -> dict:
    """Dispatch direct control task to Robot Agent via NATS."""
    task_payload = {"task_type": "direct_control", "prompt": prompt}
    return await _dispatch_to_robot_agent(agent_info["id"], task_payload, timeout=60)


# ---------------------------------------------------------------------------
# Nodes: Reflection, Check Status, Summarize, Handle Error
# ---------------------------------------------------------------------------

async def reflection(state: PlannerState) -> dict:
    results = state.get("agent_results", [])
    messages_log = state.get("messages", [])

    if not results or all(r.get("status") == "error" for r in results):
        messages_log.append(AIMessage(content="Reflection: all failed, replan"))
        return {"verification_status": "FAIL", "verification_recommendation": "replan", "messages": messages_log}

    messages_log.append(AIMessage(content="Reflection: success"))
    return {"verification_status": "PASS", "verification_recommendation": "success", "messages": messages_log}


async def check_status(state: PlannerState) -> dict:
    status = state.get("verification_status", "FAIL")
    iteration = state.get("iteration", 1)
    messages_log = state.get("messages", [])

    if status == "PASS":
        messages_log.append(AIMessage(content="Status: SUCCESS"))
    elif iteration >= MAX_REPLANS:
        messages_log.append(AIMessage(content=f"Status: MAX_REPLANS ({MAX_REPLANS}) reached"))
    else:
        messages_log.append(AIMessage(content=f"Status: REPLAN (iteration {iteration} -> {iteration + 1})"))

    return {"iteration": iteration + 1, "messages": messages_log}


async def summarize(state: PlannerState) -> dict:
    results = state.get("agent_results", [])
    messages_log = state.get("messages", [])
    iteration = state.get("iteration", 1)
    intent = state.get("intent", {})
    prompt = state.get("prompt", "")
    start_time = state.get("start_time", 0)
    total_latency = int((datetime.utcnow().timestamp() - start_time) * 1000) if start_time else 0

    if not results:
        return {"summary": "No results received from any agent."}

    success_results = [r for r in results if r.get("status") == "success"]
    error_results = [r for r in results if r.get("status") == "error"]

    if not success_results:
        error_msgs = "; ".join(r.get("error", "unknown") for r in error_results)
        return {"summary": f"All agents failed: {error_msgs}"}

    # Direct control tasks (sit down, turn, etc.) don't involve observation —
    # skip LLM summarization and return a simple completion message.
    # For compound commands (e.g. "stand up and report"), any agent phase means
    # the user cares about observations → use the LLM summarization path.
    phases_list = intent.get("phases", [])
    has_agent_phase = any(p.get("task_type") == "agent" for p in phases_list)
    task_type = "agent" if has_agent_phase else (phases_list[0].get("task_type", "") if phases_list else "")
    if task_type == "direct_control":
        agent_ids = ", ".join(r.get("agent_id", "unknown") for r in success_results)
        summary = f"completed\n[Iterations: {iteration}, Total latency: {total_latency}ms]"
        messages_log.append(AIMessage(content=f"Summary: direct_control completed ({agent_ids})"))
        return {"summary": summary, "messages": messages_log}

    # Collect structured data for LLM summarization
    agent_data = []
    for r in success_results:
        structured = r.get("structured", {})
        mission = structured.get("mission", {})
        detections = structured.get("detections", [])
        patrol_report = mission.get("patrol_report", {})

        # Use VLM-deduped counts if available, otherwise list types only
        dedup_counts = mission.get("dedup_counts")
        if dedup_counts:
            label_counts = dedup_counts
        else:
            label_counts = {d.get("label", "unknown"): 1 for d in detections}

        # Look up zone from registry for labeling in the report
        from ..infra.robot_registry import robot_registry
        zone_info = ""
        robot_entry = robot_registry.get_robot(r.get("agent_id", ""))
        if robot_entry and robot_entry.zone:
            zone_info = robot_entry.zone

        agent_data.append({
            "agent_id": r.get("agent_id", "unknown"),
            "zone": zone_info,
            "unique_detections": label_counts,
            "has_dedup": bool(dedup_counts),
            "vlm_descriptions": mission.get("vlm_descriptions", {}),
            "scene_description": mission.get("scene_description", ""),
            "total_detections": len(detections),
            "frames_analyzed": mission.get("frames_analyzed", 0),
            "elapsed_s": mission.get("elapsed_s", 0),
            "patrol_report": patrol_report.get("summary", ""),
            "alarm_on_detection": mission.get("alarm_on_detection", False),
            "alarm_fired_labels": mission.get("alarm_fired_labels", []),
            "task_goal": r.get("task_goal") or mission.get("task_goal", ""),
        })

    # Use LLM to generate a human-readable response
    try:
        from ..config import create_llm
        llm = create_llm()

        context_parts = []
        for ad in agent_data:
            zone_label = f" (Zone {ad['zone']})" if ad.get('zone') else ""
            part = f"Robot {ad['agent_id']}{zone_label}:"
            # Alarm/watch missions: report presence of the target, not counts.
            # If the target appeared even once, alarm_fired_labels lists its
            # YOLO label. For attribute-described targets (e.g. "person wearing
            # yellow"), the fact that a label was alarmed ALSO means the VLM
            # confirmed the description — otherwise the alarm would not have
            # fired. The summary LLM should trust this.
            if ad.get("alarm_on_detection"):
                part += f"\n  Mode: watch/alarm"
                part += f"\n  Task: {ad.get('task_goal', '')}"
                fired = ad.get("alarm_fired_labels") or []
                if fired:
                    part += (
                        f"\n  Target(s) detected and alarm fired: {', '.join(fired)}"
                        f"\n  (VLM already verified these match the task description if attributes were specified.)"
                    )
                else:
                    part += "\n  Target NOT detected during the watch period."
            elif ad["patrol_report"]:
                part += f"\n  Patrol report: {ad['patrol_report']}"
            if not ad.get("alarm_on_detection") and ad["unique_detections"]:
                if ad.get("has_dedup"):
                    det_str = ", ".join(f"{count} {label}" for label, count in ad["unique_detections"].items())
                    part += f"\n  Detected (VLM verified counts): {det_str}"
                else:
                    det_str = ", ".join(ad["unique_detections"].keys())
                    part += f"\n  Detected object types: {det_str}"
            if ad.get("scene_description"):
                # Scene-level VLM output — describes the environment as a
                # whole, independent of detected objects.
                part += f"\n  Scene: {ad['scene_description']}"
            if ad.get("vlm_descriptions"):
                # Only filter out descriptions that REFUSE / FAIL to describe.
                # 'blurry' / 'unclear' used to be here but VLM uses those as
                # FACTUAL adjectives ("a blurry person in a blue dress") that
                # otherwise carry a valid description — filtering those drops
                # real subjects (e.g. the light-blue-dress civilian in Zone B).
                _BAD_DESC = ("can't help", "i'm sorry", "cannot describe",
                             "unable to describe", "too dark to see",
                             "not enough detail to")
                label_indices = {}
                for key, desc in ad["vlm_descriptions"].items():
                    label = key.split(":")[0]
                    label_indices.setdefault(label, []).append((key, desc))
                dedup_counts = ad.get("unique_detections", {})
                for label, entries in label_indices.items():
                    # Prefer clear descriptions; fall back to all if none are clear
                    good = [(k, d) for k, d in entries
                            if not any(b in d.lower() for b in _BAD_DESC)]
                    if not good:
                        good = entries
                    # Cap at VLM-verified unique count to avoid showing duplicates
                    max_n = dedup_counts.get(label, len(good))
                    good = good[:max_n]
                    if len(good) == 1:
                        part += f"\n  {label} appearance: {good[0][1]}"
                    else:
                        for i, (key, desc) in enumerate(good, 1):
                            part += f"\n  {label} {i} appearance: {desc}"
            part += f"\n  Stats: {ad['frames_analyzed']} frames analyzed in {ad['elapsed_s']:.0f}s"
            context_parts.append(part)
        context = "\n\n".join(context_parts)

        # Default scenario: battlefield / tactical framing is ON.
        # Opt out only when the user's prompt explicitly requests a
        # neutral / civilian / non-tactical voice.
        _neutral_kws = (
            "non-tactical", "non tactical", "civilian setting",
            "neutral voice", "neutral tone", "plain description",
            "urban setting", "no tactical", "no military",
        )
        _count_kws = ("count", "how many", "number of", "tally", "total")
        _prompt_lc = (prompt or "").lower()
        is_neutral = any(k in _prompt_lc for k in _neutral_kws)
        is_tactical = not is_neutral  # battlefield is the default
        is_count = any(k in _prompt_lc for k in _count_kws)
        # Find mode: phase has a specific target_name but is NOT an alarm/watch
        # task (alarm also has target_name but different semantics).  Find
        # exits on first match → scene observation is inherently incomplete
        # → computing a zone THREAT LEVEL from such limited data is misleading
        # (e.g. "🟢 SECURE" reported when a tank was literally in the video,
        # just unseen because mission ended before it entered frame).
        # Replace the threat badge with a TARGET CONFIRMED / NOT FOUND badge
        # so the main dashboard signal is the target outcome itself.
        is_find = any(
            p.get("task_type") == "agent"
            and p.get("target_name", "")
            and not p.get("report_all", False)
            and not p.get("alarm_on_detection", False)
            for p in phases_list
        )
        mode_bits = []
        mode_bits.append("tactical SITREP (default)" if is_tactical else "neutral prose (opt-out)")
        if is_find:
            mode_bits.append("find-target (no threat level)")
        if is_count:
            mode_bits.append("count aggregation")
        messages_log.append(AIMessage(
            content=f"Summary: drafting — mode={' + '.join(mode_bits)}, "
                    f"aggregating {len(success_results)} robot(s)"
        ))

        system_msg = (
            "You are a multi-robot command center. The user gave a command to several robots and the mission is complete. "
            "Write a structured report with ONE section per robot, clearly separated. Use this exact format:\n\n"
            "### Robot <robot_id> (Zone <zone>)\n"
            "<what that robot observed, first person, 1-3 sentences>\n\n"
            "Rules:\n"
            "- Preserve the order of robots as given in the data.\n"
            "- Do NOT merge multiple robots into one section.\n"
            "- If counts are marked 'VLM verified', report the exact numbers; otherwise just list object types.\n"
            "- If multiple individuals of the same type have descriptions (e.g. person 1, person 2), describe EACH separately.\n"
            "- Do NOT include technical stats like frame counts or latency.\n"
            "- If a robot saw nothing notable, say 'Nothing of interest to report.'\n"
            "- If a robot's data has a 'Scene:' line, that is a description of the environment/setting the\n"
            "  robot is in. Lead that robot's section with it, in first person (e.g. \"I am in a …\"). Keep\n"
            "  the scene description separate from object listings — do NOT merge them into one run-on\n"
            "  sentence. If the user's command was scene-only ('describe the environment'), keep the\n"
            "  section focused on the scene and omit per-object details unless they are also requested.\n\n"
            "COUNT / QUANTIFY INTENT:\n"
            "If the user's command contains 'count', 'how many', 'number of', 'tally', or 'total',\n"
            "PREPEND a single aggregate line BEFORE the per-robot sections:\n"
            "  ### Total: <N> <thing> detected across <M> zones\n"
            "Compute N by summing VLM-verified counts across all robots (fallback to label\n"
            "diversity if dedup is unavailable). Map semantic requests back to their COCO label:\n"
            "'civilians'/'combatants' aggregate as 'person', 'hostile armor' as 'vehicle', etc.\n"
            "If the user specified attributes (e.g. 'count civilians in plain clothes') and VLM\n"
            "verification ran, the counts already exclude non-matches — trust them, do not\n"
            "hedge with 'of these, X appeared to be...'.\n\n"
            "TACTICAL / MILITARY FRAMING — DEFAULT SCENARIO IS BATTLEFIELD:\n"
            "This command center operates on a battlefield by default.  ALWAYS\n"
            "use a military situational-awareness voice and TRANSLATE literal\n"
            "observations into tactical language.  The only exception is if\n"
            "the user's prompt explicitly contains one of the opt-out phrases\n"
            "'non-tactical', 'civilian setting', 'neutral voice', 'neutral\n"
            "tone', 'plain description', 'urban setting', 'no tactical', or\n"
            "'no military' — in that case, fall back to neutral prose.\n"
            "Otherwise (including for generic prompts like 'describe the\n"
            "scene', 'report what you see', 'count the people'), stay in\n"
            "tactical mode.  Mapping guide:\n"
            "  Posture → status:\n"
            "    'person standing' / 'person walking' → 'civilian' / 'non-combatant'\n"
            "    'person in wheelchair' → 'civilian with limited mobility, evacuation priority'\n"
            "    'person lying/prone/slumped' (in ANY condition, even 'intact') →\n"
            "        'downed personnel, medevac priority' — ALWAYS a Casualty,\n"
            "         never a civilian. A body on the ground in battlefield\n"
            "         footage is presumed wounded until cleared by medics.\n"
            "    'person with arms raised overhead / hands up / both hands raised' →\n"
            "        'non-combatant in surrender posture / detainee — secure and\n"
            "         process'. Counts as a Casualty row, not Civilian. Applies\n"
            "         equally to adults and children.\n"
            "    'person with visible wounds or blood' → 'wounded personnel, urgent medevac'\n"
            "  Body integrity → casualty severity (IMPORTANT, don't flatten):\n"
            "    Each person's VLM description contains a structured line\n"
            "    'Body integrity: <intact | INCOMPLETE — ...>'.  Trust it.\n"
            "    'INCOMPLETE' / 'partial body' / 'only torso and arms visible' /\n"
            "    'no lower body' / 'severed at waist' / 'dismembered' /\n"
            "    'missing both legs' / 'only upper torso and head' (and NOT\n"
            "    merely occluded by an object) → 'severe casualty, presumed KIA\n"
            "    (killed in action); body recovery required, not medevac' —\n"
            "    this is a MUCH more serious status than a simple 'downed\n"
            "    personnel' and must be reported distinctly.\n"
            "    'left arm missing' / 'missing an arm' / 'stump where left arm\n"
            "    was' / 'one arm missing' (i.e. limb-loss but body otherwise\n"
            "    upright and ambulatory) → 'wounded personnel — limb loss,\n"
            "    urgent medevac'. Counts as a CASUALTY, never a civilian,\n"
            "    even if the figure is walking or standing.\n"
            "    'Visible injuries / blood' line shows anything other than\n"
            "    'none' (blood stains, wound on head, etc.) → wounded,\n"
            "    counts as a casualty.\n"
            "  Vehicles — hostile classification is STRICT:\n"
            "    Tanks (tracked, turret+gun):\n"
            "      'tank' / 'toy tank' / 'model tank' / 'tracked vehicle with\n"
            "       turret' → hostile armor.  Label bullets as\n"
            "       'Tracked tank, <colour/pattern/orientation>'.\n"
            "    Wheeled military vehicles (WHEELS, NOT tracks):\n"
            "      'Humvee' / 'military-style truck' / 'military truck' /\n"
            "       'tactical vehicle' / 'armored transport' / 'truck with\n"
            "       military markings' / 'beige/tan truck with boxy front' →\n"
            "       hostile logistics vehicle.  Label bullets as\n"
            "       'Wheeled military vehicle / Humvee, <colour/description>'.\n"
            "       Do NOT call these 'tracked' — they have wheels.\n"
            "    Civilian vehicles:\n"
            "      'car' / 'sedan' / 'SUV' / 'pickup' / 'passenger vehicle' →\n"
            "       civilian vehicle → scene context ONLY, never 'Hostile Armor'.\n"
            "    Promotion trigger (unambiguous): a 'truck' track qualifies as\n"
            "    hostile ONLY when that track's VLM description (from the raw\n"
            "    observation data) contains one of: 'military-style', 'Humvee',\n"
            "    'camouflage', 'camo', 'tactical', 'armored', or 'military\n"
            "    vehicle'.  A plain 'truck' or 'beige truck/SUV' without those\n"
            "    keywords stays civilian.\n"
            "    MANDATORY: when the promotion trigger fires, the truck MUST\n"
            "    appear in the 'Hostile Armor' row with its own bullet.  DO\n"
            "    NOT describe it only in the Scene context line — 'military-\n"
            "    style truck / Humvee' in Scene with no corresponding Armor\n"
            "    bullet is a reporting failure.  One physical military vehicle\n"
            "    counts in exactly ONE place: Armor, never Scene.\n"
            "  COUNTING DISCIPLINE — READ CAREFULLY:\n"
            "    The 'Hostile Armor' count = (# tank tracks promoted above) +\n"
            "    (# wheeled-military-vehicle tracks promoted above).\n"
            "    DO NOT synthesise an 'additional tracked hostile armor'\n"
            "    entry from a 'car' track, a civilian 'truck' track, or any\n"
            "    object you describe as 'misclassified as X earlier'.  Every\n"
            "    bullet under 'Hostile Armor' MUST correspond 1-to-1 to a\n"
            "    specific track that the mission's detection data contains —\n"
            "    no invented duplicates, no inferred extras.\n"
            "  Scene context (mention only if relevant, never lead with):\n"
            "    'traffic light' / 'stop sign' / 'bicycle' / 'dog' → urban context\n"
            "\n"
            "NO DETECTOR META-COMMENTARY: describe each subject as what it IS,\n"
            "never as what the detector got wrong. NEVER use phrasings like\n"
            "'misclassified as X', 'detected as X but actually Y', 'originally\n"
            "labeled X', '(mislabeled as X)', 'classified as X in error', etc.\n"
            "If the VLM description contradicts the YOLO prefix, TRUST the VLM\n"
            "description and report the VLM's verdict directly — the commander\n"
            "does not need to see the internal detection debate. Just write\n"
            "'Tracked tank, olive-green, ...' — never 'Tracked tank (misclassified\n"
            "as car)'.\n"
            "\n"
            "CRITICAL — REMAIN IN CHARACTER: the mini-city uses figurines and toy props\n"
            "to stand in for real battlefield subjects. Your report MUST commit to the\n"
            "tactical framing and treat those subjects as real. DO NOT use any of these\n"
            "words or phrases in tactical mode: 'figurine', 'toy', 'model', 'mannequin',\n"
            "'wooden figure', 'plastic', 'doll', 'miniature', 'scaled-down', 'replica',\n"
            "'representing', 'appears to be a X representing Y', 'seems to be a small Y'.\n"
            "If you observed 'a wooden figure lying on its side', report 'downed personnel\n"
            "in prone position'. If you observed 'a toy tank', report 'hostile armor' /\n"
            "'enemy armored vehicle'. Colors and clothing descriptions ARE fine (e.g.\n"
            "'wearing light blue top and dark blue pants' — tactical analysts describe\n"
            "attire all the time); what is NOT fine is meta-commentary about the\n"
            "subject's physical medium.\n"
            "\n"
            "STRUCTURED OUTPUT FORMAT (tactical mode ONLY):\n"
            "Don't write a prose paragraph. Use the compact structure below —\n"
            "commanders scan left-to-right for level + three counts.\n"
            "Plain text (no markdown), exact emoji prefixes for consistent rendering:\n"
            "\n"
            "  ### Robot <id> (Zone <X>) — <overall threat emoji> <OVERALL LABEL>\n"
            "  \n"
            "  ▌ THREAT LEVEL:   <🟢 SECURE | 🟡 ELEVATED | 🔴 CRITICAL>\n"
            "  \n"
            "  <armor emoji> Hostile Armor: <N> tank(s)   |or|   ✓ None\n"
            "    • <one short bullet per armor unit: type + distinguishing detail\n"
            "       (color, orientation, posture — e.g. 'Tracked tank, olive-green,\n"
            "       turret facing west', 'Wheeled APC, sand-colored, stationary').\n"
            "       Skip bullet entirely when N=0.>\n"
            "  <cas   emoji> Casualties:    <N> wounded/downed/KIA   |or|   ✓ None\n"
            "    • <one short bullet per casualty: state + distinguishing detail>\n"
            "  🟢 Civilians:     <N>\n"
            "    • <ONE short bullet PER civilian — never collapse multiple\n"
            "       civilians into a single summary bullet, no matter how many\n"
            "       there are (if N=8, render 8 bullets). Mention distinguishing\n"
            "       appearance, clothing color, activity, and — if applicable —\n"
            "       vulnerability (wheelchair, mobility limitation, etc.).\n"
            "       Examples: 'Individual in teal top and blue pants, standing',\n"
            "       'Adult in red shirt beside vehicle', 'Person in wheelchair —\n"
            "       evacuation priority'. NEVER mention the words 'mannequin',\n"
            "       'figurine', 'wooden figure', 'toy' — remain in tactical character.>\n"
            "  \n"
            "  ▌ RECOMMENDATION: <one line action, e.g. 'Dispatch recovery team'>\n"
            "\n"
            "Emoji rules per row:\n"
            "  Hostile Armor:  🔴 if N>0,  ✓ if 0\n"
            "  Casualties:     🔴 if N>0,  ✓ if 0\n"
            "  Civilians:      🟢 always (count of friendly non-combatants)\n"
            "\n"
            "Threat-level rule (MECHANICAL — compute from the three counts\n"
            "in this exact order, stop at first match, do NOT second-guess):\n"
            "  🔴 CRITICAL — Hostile Armor >0.  Only active hostile threat\n"
            "                (tanks, Humvees, other armor) triggers CRITICAL.\n"
            "                This is the 'engagement or evade' level.\n"
            "  🟡 ELEVATED — Casualties >0  OR  at least one civilian bullet\n"
            "                EXPLICITLY mentions vulnerability:\n"
            "                'wheelchair', 'limited mobility', 'mobility\n"
            "                impairment', 'elderly', 'child' (as standalone\n"
            "                person, not referring to a toy/prop nearby),\n"
            "                'stroller being pushed' (infant in stroller\n"
            "                counts as child), 'injured but walking',\n"
            "                'assisted', 'cane', 'crutches'.\n"
            "                ELEVATED means a CIVILIAN-RESPONSE situation\n"
            "                (medevac, body-recovery, evacuation of\n"
            "                vulnerable) but NO active hostile present.\n"
            "                DO NOT infer vulnerability from urban-scene\n"
            "                context like 'dog present' or 'bicycle near'\n"
            "                or 'civilian car parked' — those are scene\n"
            "                furniture, NOT vulnerability signals.\n"
            "  🟢 SECURE   — 0 armor + 0 casualties + no vulnerability keyword\n"
            "                in any civilian bullet.  DEFAULT when in doubt.\n"
            "                A zone with only healthy walking/standing\n"
            "                civilians is SECURE, period.\n"
            "\n"
            "RATIONALE — CRITICAL is reserved for active combat threat so\n"
            "the commander can scan the dashboard and instantly separate\n"
            "'need to engage/evade' zones (🔴) from 'need to respond\n"
            "humanitarian' zones (🟡).  If both armor AND casualties are\n"
            "present in the same zone, that's still CRITICAL (the armor\n"
            "dominates) — but the casualty bullets stay visible in the\n"
            "Casualty row for situational awareness.\n"
            "\n"
            "ALWAYS show all three rows (Hostile Armor, Casualties, Civilians), even when 0.\n"
            "That's the commander's quick-scan dashboard — blanks would be misread.\n"
            "\n"
            "SCENE CONTEXT (one-line, ALWAYS add when possible):\n"
            "Right before RECOMMENDATION, add a '▌ Scene context:' line describing\n"
            "the environment and ANY non-tactical objects/features the sensors saw —\n"
            "walls, flooring, lighting, furniture, windows, urban props (bicycles,\n"
            "dogs, civilian vehicles, traffic lights, traffic signs, backpacks), etc.\n"
            "Keep it to one concise line (≤ 90 chars), describing the setting with a\n"
            "brief qualitative tag. Only OMIT the line if the scene is a featureless\n"
            "empty void with literally nothing visible.\n"
            "  Examples:\n"
            "    ▌ Scene context: indoor room, wooden flooring, moderate light — quiet\n"
            "    ▌ Scene context: 1 dog, 1 traffic light — residential street\n"
            "    ▌ Scene context: 2 bicycles, 1 civilian car, traffic signs — urban block\n"
            "    ▌ Scene context: narrow corridor, uncluttered — indoor sector\n"
            "NEVER put tanks, casualties, or civilians in this line — they belong\n"
            "to the three main rows above. Use urban / environmental vocabulary;\n"
            "don't mention the subject-matter props directly (no 'wooden figures' or\n"
            "'figurines').\n"
            "\n"
            "HARD RULE — NO DOUBLE COUNTING: each person is counted in EXACTLY one of\n"
            "Casualties OR Civilians. Routing rule:\n"
            "  Casualties = any one of the following is true for that person:\n"
            "    • Body integrity is INCOMPLETE (severed, partial body, missing\n"
            "      legs, only upper torso, etc.) — KIA/recovery.\n"
            "    • Any limb is missing OR visible injuries/blood reported —\n"
            "      wounded, even if the figure is upright/walking.\n"
            "    • Posture is 'lying', 'lying/prone', 'prone', or 'slumped' —\n"
            "      UNCONDITIONALLY routed to Casualties.  Do NOT require\n"
            "      visible injuries; a body on the ground in a battlefield\n"
            "      scene is presumed wounded/KIA until cleared.  'intact'\n"
            "      body integrity + 'lying/prone' posture = 'downed personnel,\n"
            "      medevac priority' bullet — still a Casualty, NOT a civilian.\n"
            "    • Posture includes 'arms raised overhead' / 'hands raised' /\n"
            "      'hands up' / 'both arms up in surrender' — person is either\n"
            "      surrendering, a detainee, or in distress (esp. a child).\n"
            "      Route to Casualties with bullet phrasing 'Non-combatant in\n"
            "      surrender posture / detainee — secure and process'.  This\n"
            "      is distinct from normal walking/standing.\n"
            "  Civilians = upright/walking/sitting AND body integrity intact AND\n"
            "              no visible injuries AND hands NOT raised in surrender.\n"
            "Example: 1 severed figure + 1 walking figure with an arm missing +\n"
            "6 standing intact figures → Casualties: 2, Civilians: 6 (not 0+8).\n"
            "The Total (for count queries) is still the UNION so 'count persons'\n"
            "returns 8 overall.\n"
            "\n"
            "Example — tank + civilians + urban props:\n"
            "  ### Robot 006 (Zone E) — 🔴 CRITICAL\n"
            "  \n"
            "  ▌ THREAT LEVEL:   🔴 CRITICAL\n"
            "  \n"
            "  🔴 Hostile Armor: 1 tank\n"
            "    • Tracked tank, olive-green, turret facing west — hull down on open ground\n"
            "  ✓ Casualties:    None\n"
            "  🟢 Civilians:     2\n"
            "    • Two adults in the open, in close proximity to the armor — collateral risk\n"
            "  \n"
            "  ▌ Scene context: 1 bicycle, 1 traffic light — urban street, good visibility\n"
            "  ▌ RECOMMENDATION: Evacuate civilians from Sector E; track armor movement.\n"
            "\n"
            "Example — casualty present, no armor (ELEVATED for medevac/recovery):\n"
            "  ### Robot 004 (Zone C) — 🟡 ELEVATED\n"
            "  \n"
            "  ▌ THREAT LEVEL:   🟡 ELEVATED\n"
            "  \n"
            "  ✓ Hostile Armor: None\n"
            "  🔴 Casualties:    1 KIA presumed\n"
            "    • Partial body, no lower limbs visible — body recovery required\n"
            "  🟢 Civilians:     0\n"
            "  \n"
            "  ▌ Scene context: indoor room, wooden flooring, moderate light\n"
            "  ▌ RECOMMENDATION: Dispatch recovery team to Sector C.\n"
            "\n"
            "Example — quiet civilian scene:\n"
            "  ### Robot 001 (Zone A) — 🟢 SECURE\n"
            "  \n"
            "  ▌ THREAT LEVEL:   🟢 SECURE\n"
            "  \n"
            "  ✓ Hostile Armor: None\n"
            "  ✓ Casualties:    None\n"
            "  🟢 Civilians:     3\n"
            "    • Adult in red shirt standing near a bicycle\n"
            "    • Adult in dark jacket, walking\n"
            "    • Adult in light-colored top, stationary\n"
            "  \n"
            "  ▌ Scene context: 1 dog, 1 bicycle — residential block, clear sightlines\n"
            "  ▌ RECOMMENDATION: Continue ISR; maintain watch.\n"
            "\n"
            "Example — wheelchair civilian, no threat:\n"
            "  ### Robot 005 (Zone D) — 🟡 ELEVATED\n"
            "  \n"
            "  ▌ THREAT LEVEL:   🟡 ELEVATED\n"
            "  \n"
            "  ✓ Hostile Armor: None\n"
            "  ✓ Casualties:    None\n"
            "  🟢 Civilians:     2\n"
            "    • Adult in a wheelchair, wearing blue jacket — evacuation priority\n"
            "    • Standing adult in beige top nearby — likely caregiver\n"
            "  \n"
            "  ▌ Scene context: indoor hall, smooth flooring — limited exits\n"
            "  ▌ RECOMMENDATION: Prepare assisted-evac route for mobility-limited civilian.\n"
            "\n"
            "If NO tactical keyword appears, use neutral descriptive voice (paragraph format, default).\n\n"
            "WATCH/ALARM MODE SPECIAL RULES:\n"
            "- If a robot's data says 'Mode: watch/alarm', focus on WHETHER the target was detected, NOT how many.\n"
            "- If 'Target(s) detected and alarm fired' lists any labels, the target WAS detected and (for "
            "  attribute-described tasks like 'person wearing yellow') the VLM already confirmed the match — "
            "  report it positively. Do NOT say 'I saw person but didn't confirm yellow' — if an alarm fired, "
            "  the description was verified.\n"
            "- If the task said 'Target NOT detected', say plainly the target did not appear during the watch.\n"
            "- Do NOT speculate about counts of distinct instances (e.g. 'two apples') in watch mode — the same "
            "  target appearing/disappearing multiple times is NOT multiple targets.\n\n"
            + (
                "FIND-MODE OVERRIDE — APPLIES TO THIS REPORT:\n"
                "The user asked the robot to LOCATE a specific target; observation\n"
                "ended as soon as the target was matched, so the zone was NOT\n"
                "fully surveyed.  Do NOT render a THREAT LEVEL line — any value\n"
                "would be misleading given the partial view.  Replace the entire\n"
                "'▌ THREAT LEVEL: ...' line with EXACTLY ONE of:\n"
                "  ▌ TARGET: 🎯 CONFIRMED — <short target descriptor>\n"
                "  ▌ TARGET: ✗ NOT FOUND\n"
                "Use 🎯 CONFIRMED when any matching person/object bullet exists\n"
                "in this robot's data (the VLM verified the attribute match).\n"
                "Use ✗ NOT FOUND otherwise.  Also change the line at the very\n"
                "top of the section from '### Robot <id> (Zone <X>) — <emoji>\n"
                "<LABEL>' to '### Robot <id> (Zone <X>) — 🎯 TARGET CONFIRMED'\n"
                "or '### Robot <id> (Zone <X>) — ✗ TARGET NOT FOUND', matching\n"
                "the body.  Keep the three-count dashboard rows (Armor/Casualty/\n"
                "Civilian) so any other objects the robot happened to notice\n"
                "still appear — but they are secondary context, not a threat\n"
                "assessment.\n"
                if is_find else ""
            )
        )
        user_msg = f"User command: {prompt}\n\nRobot data:\n{context}"

        from langchain_core.messages import SystemMessage
        response = await llm.ainvoke([
            SystemMessage(content=system_msg),
            HumanMessage(content=user_msg),
        ])
        summary = response.content.strip()
    except Exception as e:
        logger.warning(f"LLM summarization failed: {e}, using fallback")
        # Fallback to structured summary
        agents_text = []
        for r in success_results:
            agents_text.append(f"Agent {r.get('agent_id', 'unknown')}:\n{r.get('response', '')}")
        summary = "\n\n---\n\n".join(agents_text)

    summary += f"\n[Iterations: {iteration}, Total latency: {total_latency}ms]"
    messages_log.append(AIMessage(content=f"Summary: generated ({len(success_results)} agents)"))

    return {"summary": summary, "messages": messages_log}


async def handle_error(state: PlannerState) -> dict:
    error = state.get("error", "Unknown")
    messages_log = state.get("messages", [])

    if error.startswith("NOT_VALID_TASK:"):
        reason = error.split(":", 1)[1].strip()
        summary = f"This system is designed for robot patrol and search tasks only. {reason}"
    else:
        summary = f"Task failed: {error}"

    messages_log.append(AIMessage(content=f"Error: {error}"))
    return {"summary": summary, "messages": messages_log}


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------

def after_intent_guard(state: PlannerState) -> Literal["continue", "rejected"]:
    error = state.get("error", "")
    if error.startswith("NOT_VALID_TASK"):
        return "rejected"
    return "continue"


def after_execute(state: PlannerState) -> Literal["summarize", "reflect"]:
    results = state.get("agent_results", [])
    if results and any(r.get("status") == "success" for r in results):
        return "summarize"
    return "reflect"


def after_check_status(state: PlannerState) -> Literal["success", "replan", "max_replans", "error"]:
    if state.get("error") and not state.get("agent_results"):
        return "error"
    status = state.get("verification_status", "FAIL")
    iteration = state.get("iteration", 1)
    if status == "PASS":
        return "success"
    elif iteration > MAX_REPLANS:
        return "max_replans"
    else:
        return "replan"


# ---------------------------------------------------------------------------
# Build Graph
# ---------------------------------------------------------------------------

def build_planner_graph():
    graph = StateGraph(PlannerState)

    graph.add_node("parse_and_discover", parse_and_discover)
    graph.add_node("execute_phases", execute_phases)
    graph.add_node("reflection", reflection)
    graph.add_node("check_status", check_status)
    graph.add_node("summarize", summarize)
    graph.add_node("handle_error", handle_error)

    graph.add_edge(START, "parse_and_discover")
    graph.add_conditional_edges("parse_and_discover", after_intent_guard, {
        "continue": "execute_phases",
        "rejected": "handle_error",
    })
    graph.add_conditional_edges("execute_phases", after_execute, {
        "summarize": "summarize",
        "reflect": "reflection",
    })
    graph.add_edge("reflection", "check_status")
    graph.add_conditional_edges("check_status", after_check_status, {
        "success": "summarize",
        "replan": "parse_and_discover",
        "max_replans": "summarize",
        "error": "handle_error",
    })
    graph.add_edge("summarize", END)
    graph.add_edge("handle_error", END)

    return graph.compile()


planner_graph = build_planner_graph()


async def run_planner(
    prompt: str,
    strategy: str = "",
    agents: Optional[list[str]] = None,
) -> dict:
    initial_state: PlannerState = {
        "prompt": prompt,
        "strategy": strategy,
        "iteration": 1,
        "start_time": datetime.utcnow().timestamp(),
        "messages": [HumanMessage(content=prompt)],
    }
    return await planner_graph.ainvoke(initial_state)
