from __future__ import annotations

from typing import Any
from uuid import uuid4

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    Artifact,
    DataPart,
    FilePart,
    FileWithBytes,
    FileWithUri,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    SendMessageResponse,
    Task,
    TaskState,
    TextPart,
)

from planner_agent.main_agent.models import (
    A2AArtifact,
    A2AMessage,
    A2APart,
    A2ATaskRequest,
    A2ATaskResult,
    A2ATaskState,
    DiscoveredAgent,
)


def _internal_part_to_sdk(part: A2APart) -> Part:
    if part.kind == "text":
        return Part(root=TextPart(text=part.text or "", metadata=part.metadata or None))
    if part.kind == "data":
        return Part(root=DataPart(data=part.data or {}, metadata=part.metadata or None))

    file_payload = (
        FileWithBytes(
            bytes=part.bytes,
            mimeType=part.content_type,
            name=part.metadata.get("name"),
        )
        if part.bytes
        else FileWithUri(
            uri=part.uri or "",
            mimeType=part.content_type,
            name=part.metadata.get("name"),
        )
    )
    return Part(
        root=FilePart(
            file=file_payload,
            metadata={"internal_kind": part.kind, **part.metadata} or None,
        )
    )


def _sdk_part_to_internal(part: Part) -> A2APart:
    root = part.root
    if isinstance(root, TextPart):
        return A2APart(
            kind="text",
            content_type="text/plain",
            text=root.text,
            metadata=root.metadata or {},
        )
    if isinstance(root, DataPart):
        return A2APart(
            kind="data",
            content_type="application/json",
            data=root.data,
            metadata=root.metadata or {},
        )
    if isinstance(root, FilePart):
        metadata = root.metadata or {}
        internal_kind = metadata.get("internal_kind", "file")
        file_obj = root.file
        return A2APart(
            kind=internal_kind if internal_kind in {"file", "image", "audio", "video"} else "file",
            content_type=file_obj.mime_type or "application/octet-stream",
            uri=getattr(file_obj, "uri", None),
            bytes=getattr(file_obj, "bytes", None),
            metadata=metadata,
        )
    raise TypeError(f"Unsupported A2A SDK part type: {type(root)!r}")


def _internal_message_to_sdk(message: A2AMessage, request: A2ATaskRequest) -> Message:
    role = Role.user if message.role == "user" else Role.agent
    metadata = {
        "target_agent_id": request.target_agent_id,
        "requested_task_id": request.task_id,
        "created_at": message.created_at.isoformat(),
    }
    if request.skill:
        metadata["skill"] = request.skill
        metadata["requested_skill"] = request.skill
        metadata["skill_hint"] = request.skill
    return Message(
        role=role,
        message_id=message.message_id,
        context_id=request.context_id,
        task_id=None,
        reference_task_ids=request.metadata.get("reference_task_ids"),
        parts=[_internal_part_to_sdk(part) for part in message.parts],
        metadata=metadata,
    )


def _sdk_message_to_internal(message: Message) -> A2AMessage:
    return A2AMessage(
        message_id=message.message_id,
        role="agent" if message.role == Role.agent else "user",
        parts=[_sdk_part_to_internal(part) for part in message.parts],
    )


def _sdk_state_to_internal(state: TaskState | str) -> A2ATaskState:
    value = state.value if isinstance(state, TaskState) else str(state)
    return A2ATaskState(value.replace("-", "_"))


def _sdk_artifact_to_internal(artifact: Artifact) -> A2AArtifact:
    return A2AArtifact(
        artifact_id=artifact.artifact_id or str(uuid4()),
        name=artifact.name or "artifact",
        parts=[_sdk_part_to_internal(part) for part in artifact.parts],
        metadata=artifact.metadata or {},
    )


def _structured_output_from_task(task: Task) -> dict[str, Any] | None:
    for artifact in task.artifacts or []:
        for part in artifact.parts:
            if isinstance(part.root, DataPart):
                return part.root.data
    if task.status.message and task.status.message.metadata:
        output = task.status.message.metadata.get("structured_output")
        if isinstance(output, dict):
            return output
    return None


def internal_task_request_to_a2a_request(request: A2ATaskRequest) -> SendMessageRequest:
    """Convert internal task state into the installed A2A SDK JSON-RPC request."""

    if not request.messages:
        raise ValueError("A2A task request requires at least one message")
    sdk_message = _internal_message_to_sdk(request.messages[-1], request)
    metadata = {
        **request.metadata,
        "target_agent_id": request.target_agent_id,
        "user_goal": request.user_goal,
    }
    if request.skill:
        metadata["skill"] = request.skill
        metadata.setdefault("requested_skill", request.skill)
        metadata.setdefault("skill_hint", request.skill)
    return SendMessageRequest(
        id=str(uuid4()),
        params=MessageSendParams(
            message=sdk_message,
            metadata=metadata,
        ),
    )


def a2a_response_to_internal_task_result(response: SendMessageResponse | Task | Message) -> A2ATaskResult:
    """Normalize an installed A2A SDK response into PI-agent task state."""

    result: Task | Message
    if isinstance(response, SendMessageResponse):
        root = response.root
        if hasattr(root, "error"):
            error = root.error
            raise RuntimeError(f"A2A error {error.code}: {error.message}")
        result = root.result
    else:
        result = response

    if isinstance(result, Message):
        return A2ATaskResult(
            task_id=result.task_id or str(uuid4()),
            context_id=result.context_id or str(uuid4()),
            state=A2ATaskState.completed,
            messages=[_sdk_message_to_internal(result)],
        )

    messages = []
    if result.history:
        messages.extend(_sdk_message_to_internal(message) for message in result.history)
    if result.status.message:
        messages.append(_sdk_message_to_internal(result.status.message))

    return A2ATaskResult(
        task_id=result.id,
        context_id=result.context_id,
        state=_sdk_state_to_internal(result.status.state),
        messages=messages,
        artifacts=[_sdk_artifact_to_internal(artifact) for artifact in result.artifacts or []],
        structured_output=_structured_output_from_task(result),
    )


def discovered_agent_to_a2a_agent_card(agent: DiscoveredAgent) -> AgentCard:
    """Reconstruct an A2A Agent Card from a discovered record for transport use."""

    if agent.raw_record and agent.raw_record.get("interaction", {}).get("agent_card"):
        return AgentCard.model_validate(agent.raw_record["interaction"]["agent_card"])

    skills = [
        AgentSkill(
            id=cap.skill_tags[0] if cap.skill_tags else cap.name.lower().replace(" ", "-"),
            name=cap.name,
            description=cap.description,
            tags=cap.skill_tags,
            examples=cap.examples,
            inputModes=cap.input_modes or agent.input_modes,
            outputModes=cap.output_modes or agent.output_modes,
        )
        for cap in agent.skills
    ]
    return AgentCard(
        name=agent.name,
        description=agent.description,
        url=agent.endpoint,
        provider=AgentProvider(organization=agent.organization, url=""),
        version="0.1.0",
        protocolVersion="0.3.0",
        preferredTransport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=agent.input_modes or ["text/plain"],
        defaultOutputModes=agent.output_modes or ["text/plain"],
        skills=skills,
        security=agent.auth.get("security", []),
        securitySchemes=agent.auth.get("security_schemes", {}),
    )
