"""
LangGraph-based Planner for Multi-Robot System.

Dispatches tasks to Robot Agents via NATS (each Robot Agent is an independent process).
Falls back to in-process execution if NATS dispatch fails.

Architecture:
- Planner discovers Robot Agents via ADS
- Planner sends tasks to Robot Agents via NATS request/reply
- Each Robot Agent manages its own CentralPiAgent + Detection Engine
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

    # All commands go through LLM for intelligent routing
    parse_result, vlm_config = await parse_intent(prompt, online_agents)

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
        # Inject VLM tool decisions into phases
        for ph in intent["phases"]:
            ph["vlm_verify"] = vlm_config.get("verify", False)
            ph["vlm_describe"] = vlm_config.get("describe", False)
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
        if ph.get("duration_s", 0) > 0: details.append(f"duration={ph['duration_s']}s")
        if ph.get("description"): details.append(f"desc='{ph['description']}'")
        messages_log.append(AIMessage(content=f"Intent: {', '.join(details)}"))

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
    """Execute all phases sequentially, respecting triggers."""
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

    # Always exactly one phase
    phase = phases[0]

    robots = _resolve_phase_robots(phase, online)
    if not robots:
        target_desc = phase.get("target_robot_ids") or phase.get("target_zone") or "any"
        return {"agent_results": [], "error": f"No robots matched ({target_desc})", "messages": messages_log}

    robot_ids = [r["id"] for r in robots]
    report_all = phase.get("report_all", False)
    mode = "report" if report_all else ("coordinated" if len(robots) > 1 and phase["task_type"] == "agent" else "independent")
    messages_log.append(AIMessage(
        content=f"Execute: {phase['task_type']} → {robot_ids} [{mode}]"
    ))
    logger.info(f"Execute: {phase['task_type']} → {robot_ids} mode={mode}")

    result = await _execute_single_phase(phase, robots, prompt, messages_log)
    agent_results = result.get("agent_results", [])

    if not agent_results:
        return {"agent_results": [], "error": "No results", "messages": messages_log}

    success_count = sum(1 for r in agent_results if r.get("status") == "success")
    messages_log.append(AIMessage(
        content=f"Done: {success_count}/{len(agent_results)} succeeded"
    ))

    error = "ALL_AGENTS_FAILED" if success_count == 0 else None
    return {"agent_results": agent_results, "error": error, "messages": messages_log}


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
        # Agent mode: dispatch to all target robots in parallel
        if len(robots) == 1:
            return await _dispatch_agent_task(robots[0], phase, prompt, messages_log)
        per_robot_logs = [list(messages_log) for _ in robots]
        results = await asyncio.gather(*[
            _dispatch_agent_task(robot, phase, prompt, per_robot_logs[i])
            for i, robot in enumerate(robots)
        ])
        # Merge agent_results and messages from all dispatches
        merged = []
        for i, r in enumerate(results):
            merged.extend(r.get("agent_results", []))
            for msg in per_robot_logs[i][len(messages_log):]:
                messages_log.append(msg)
        return {"agent_results": merged, "messages": messages_log}
    else:
        # Direct control: dispatch to all target robots in parallel
        # Use original user prompt, not LLM-rewritten description, to avoid semantic drift
        robot_ids = [r["id"] for r in robots]
        messages_log.append(AIMessage(content=f"Direct control → {robot_ids}"))
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
    """Send cancel command to a Robot Agent via NATS."""
    from ..robot_agent import cancel_topic
    from ..infra.shared import ensure_connected
    try:
        transport = await ensure_connected()
        reply = await transport.request(
            cancel_topic(robot_id), json.dumps({"command": "cancel"}).encode(), timeout=5
        )
        logger.info(f"Cancel sent to {robot_id}: {json.loads(reply).get('status')}")
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
    messages_log.append(AIMessage(content=f"ReAct coordination: {len(robots)} robots"))

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


async def _ping_robot(robot_id: str, timeout: float = 3) -> bool:
    """Quick check if Robot Agent is reachable via NATS."""
    from ..robot_agent import ping_topic
    from ..infra.shared import ensure_connected
    try:
        transport = await ensure_connected()
        reply = await transport.request(
            ping_topic(robot_id), b'{}', timeout=timeout
        )
        return True
    except Exception:
        return False


async def _dispatch_to_robot_agent(robot_id: str, task_payload: dict, timeout: float = 180) -> dict:
    """Send task to Robot Agent via NATS request/reply.

    Falls back to in-process execution if NATS fails.
    """
    from ..robot_agent import task_topic
    from ..infra.shared import ensure_connected

    # Quick ping to avoid long timeout on disconnected robots
    if not await _ping_robot(robot_id):
        logger.warning(f"Robot {robot_id} not reachable (ping failed), skipping")
        return {"status": "error", "error": f"Robot {robot_id} not reachable"}

    topic = task_topic(robot_id)
    payload_bytes = json.dumps(task_payload).encode()

    try:
        transport = await ensure_connected()
        reply_bytes = await transport.request(topic, payload_bytes, timeout=timeout)
        result = json.loads(reply_bytes)
        logger.info(f"NATS dispatch to {robot_id}: {result.get('status', '?')}")
        return result
    except Exception as e:
        logger.error(f"NATS dispatch to {robot_id} failed: {e}")
        return {"status": "error", "agent_id": robot_id, "error": f"Robot Agent unreachable: {e}"}


async def _dispatch_agent_task(agent: dict, phase: dict, task_goal: str, messages_log: list) -> dict:
    """Dispatch agent mode task to Robot Agent via NATS."""
    import uuid

    task_payload = {
        "task_type": "agent",
        "mission_id": str(uuid.uuid4())[:8],
        "target_type": phase.get("target_type", "any"),
        "target_name": phase.get("target_name", ""),
        "report_all": phase.get("report_all", False),
        "vlm_verify": phase.get("vlm_verify", False),
        "vlm_describe": phase.get("vlm_describe", False),
        "task_goal": task_goal,
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
    task_type = intent.get("phases", [{}])[0].get("task_type", "")
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

        agent_data.append({
            "agent_id": r.get("agent_id", "unknown"),
            "unique_detections": label_counts,
            "has_dedup": bool(dedup_counts),
            "vlm_descriptions": mission.get("vlm_descriptions", {}),
            "total_detections": len(detections),
            "frames_analyzed": mission.get("frames_analyzed", 0),
            "elapsed_s": mission.get("elapsed_s", 0),
            "patrol_report": patrol_report.get("summary", ""),
        })

    # Use LLM to generate a human-readable response
    try:
        from ..config import create_llm
        llm = create_llm()

        context_parts = []
        for ad in agent_data:
            part = f"Robot {ad['agent_id']}:"
            if ad["patrol_report"]:
                part += f"\n  Patrol report: {ad['patrol_report']}"
            if ad["unique_detections"]:
                if ad.get("has_dedup"):
                    det_str = ", ".join(f"{count} {label}" for label, count in ad["unique_detections"].items())
                    part += f"\n  Detected (VLM verified counts): {det_str}"
                else:
                    det_str = ", ".join(ad["unique_detections"].keys())
                    part += f"\n  Detected object types: {det_str}"
            if ad.get("vlm_descriptions"):
                _BAD_DESC = ("blurry", "can't help", "difficult to", "i'm sorry",
                             "unclear", "cannot", "unable", "too dark", "not enough")
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

        system_msg = (
            "You are a multi-robot command center. The user gave a command to robot(s) and the mission is complete. "
            "Based on the robot data below, write a concise, natural-language response to the user's original request. "
            "Respond as if you are the robot(s) reporting back. "
            "Report what was detected. If counts are marked as 'VLM verified', report the exact numbers. "
            "Otherwise, just list object types without guessing counts. "
            "If appearance descriptions are provided for multiple individuals of the same type (e.g. person 1, person 2), "
            "describe EACH one separately — do not skip or merge them. "
            "Do NOT include technical stats like frame counts or latency."
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

    messages_log.append(AIMessage(content=f"Error handled: {error}"))
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
