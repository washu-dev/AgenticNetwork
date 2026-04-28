from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from v2_agent_network.services.a2a.client import A2AClient
from v2_agent_network.services.common.models import (
    A2AMessage,
    A2APart,
    A2ATaskRequest,
    A2ATaskState,
    AgentInvocationRecord,
    Channel,
    PIContext,
    UserMessage,
)
from v2_agent_network.services.directory.card_oasf_converter import load_agent_card
from v2_agent_network.services.directory.client import DirectoryClient
from v2_agent_network.services.pi_agent.policy import evaluate_capability
from v2_agent_network.services.pi_agent.router import infer_skill, select_agent
from v2_agent_network.services.state.store import InMemoryStateStore


class IncomingMessage(BaseModel):
    user_id: str = Field(default="demo-user")
    channel: Channel = Channel.api
    text: str
    context_id: str | None = None


def create_app(
    *,
    directory_client: DirectoryClient | None = None,
    a2a_client: A2AClient | None = None,
    state_store: InMemoryStateStore | None = None,
    publish_weather_card: bool = True,
) -> FastAPI:
    directory = directory_client or DirectoryClient()
    client = a2a_client or A2AClient()
    store = state_store or InMemoryStateStore()
    app = FastAPI(title="V2 PI Agent", version="0.1.0")
    app.state.directory_client = directory
    app.state.a2a_client = client
    app.state.state_store = store

    if publish_weather_card:
        card_path = Path(__file__).parents[2] / "configs" / "agent_cards" / "weather_agent.card.json"
        directory.publish_agent_card(load_agent_card(card_path))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/messages")
    async def post_message(incoming: IncomingMessage) -> dict[str, object]:
        try:
            user_message = UserMessage(
                channel=incoming.channel,
                sender_id=incoming.user_id,
                text=incoming.text,
            )
            context = PIContext(
                context_id=incoming.context_id or str(uuid4()),
                user_id=incoming.user_id,
                channel=incoming.channel,
                original_message_id=user_message.id,
                current_goal=incoming.text,
                conversation_history=[incoming.text],
            )
            store.save_context(context)

            routing = select_agent(
                context_id=context.context_id,
                user_goal=incoming.text,
                directory_client=directory,
            )
            selected_agent = routing.selected_agents[0]
            selected_capability = selected_agent.skills[0]
            policy_decision = evaluate_capability(selected_capability)
            if policy_decision.status.value == "denied":
                raise HTTPException(status_code=403, detail=policy_decision.reason)

            task_request = A2ATaskRequest(
                task_id=str(uuid4()),
                context_id=context.context_id,
                target_agent_id=selected_agent.agent_id,
                skill=infer_skill(incoming.text),
                user_goal=incoming.text,
                messages=[
                    A2AMessage(
                        role="user",
                        parts=[A2APart(kind="text", text=incoming.text)],
                    )
                ],
                metadata={
                    "user_id": incoming.user_id,
                    "channel": incoming.channel.value,
                    "policy_decision": policy_decision.status.value,
                    "confirmation_status": policy_decision.confirmation_status.value,
                },
            )
            result = await client.send_task(selected_agent, task_request)
            invocation = AgentInvocationRecord(
                context_id=context.context_id,
                task_id=result.task_id,
                agent_id=selected_agent.agent_id,
                skill=task_request.skill,
                request_summary=incoming.text,
                status=result.state,
                artifacts=result.artifacts,
                policy_decision=policy_decision,
                selected_agent=selected_agent,
                completed_at=result.messages[-1].created_at if result.messages else None,
                error=result.error,
            )
            store.save_invocation(invocation)
            context.active_tasks.append(result.task_id)
            store.save_context(context)

            return {
                "context_id": context.context_id,
                "task_id": result.task_id,
                "state": result.state,
                "selected_agent": selected_agent.agent_id,
                "policy_decision": policy_decision.status,
                "confirmation_status": policy_decision.confirmation_status,
                "text": _last_text(result),
                "artifacts": [artifact.model_dump(mode="json") for artifact in result.artifacts],
                "structured_output": result.structured_output,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


def _last_text(result) -> str:
    for message in reversed(result.messages):
        for part in message.parts:
            if part.kind == "text" and part.text:
                return part.text
    if result.state != A2ATaskState.completed and result.error:
        return result.error
    return ""


app = create_app(
    publish_weather_card=os.getenv("AGENT_NETWORK_FAKE_ADS", "false").lower() == "true"
)
