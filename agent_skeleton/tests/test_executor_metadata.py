"""Regression tests for the credential-metadata leak fix in executor.py.

Based on washu-dev/agent-skeleton PR #29 by raneemousa3. These verify that
reserved private keys in request/message/part metadata never reach the
model-visible payload built by _payload_from_context(), and that a tool which
authenticates upstream still gets its own credential. Uses sentinel secrets
only, never a real credential, and no network (the LLM client and the upstream
HTTP opener are both faked).

executor.py degrades gracefully without a2a-sdk (its base class becomes object),
so the helper tests import and run without the SDK installed; the end-to-end
executor test skips without it.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_skeleton import executor as executor_mod
from agent_skeleton import llm_loop
from agent_skeleton.executor import _model_visible_metadata, _payload_from_context
from agent_skeleton.spec import endpoint_wrapper_spec
from agent_skeleton.system_tools import call_endpoint as call_endpoint_mod
from agent_skeleton.system_tools.call_endpoint import EndpointConfig

SENTINEL = "TEST_SECRET_DO_NOT_LEAK"
UPSTREAM_TOKEN = "TEST_UPSTREAM_TOKEN_DO_NOT_LEAK"


def _ctx(*, metadata=None, message=None):
    return type("Ctx", (), {"metadata": metadata, "message": message})()


def _msg(*, metadata=None, parts=None):
    return type("Msg", (), {"metadata": metadata, "parts": parts or []})()


def test_helper_strips_reserved_keys_case_insensitive():
    out = _model_visible_metadata(
        {"credentials": {"api_key": SENTINEL}, "Authorization": SENTINEL, "prompt": "q"}
    )
    assert out == {"prompt": "q"}


def test_helper_handles_non_dict():
    assert _model_visible_metadata(None) == {}
    assert _model_visible_metadata("nope") == {}


def test_context_metadata_credentials_not_in_payload():
    ctx = _ctx(metadata={"credentials": {"ncbi_api_key": {"api_key": SENTINEL}}, "prompt": "q"})
    payload = _payload_from_context(ctx)
    assert "credentials" not in payload
    assert SENTINEL not in json.dumps(payload)
    assert payload["prompt"] == "q"


def test_ordinary_metadata_still_flows():
    ctx = _ctx(metadata={"prompt": "hello", "skill": "evidence-trail/build-map"})
    payload = _payload_from_context(ctx)
    assert payload["prompt"] == "hello"
    assert payload["skill"] == "evidence-trail/build-map"


def test_message_and_datapart_metadata_are_filtered():
    # Use the real a2a DataPart so is_data_part() recognizes it; skip if the SDK
    # is not installed (the executor still imports without it).
    a2a_types = pytest.importorskip("a2a.types")
    root = a2a_types.DataPart(
        data={"credentials": {"api_key": SENTINEL}, "topic": "obesity"},
        metadata={"secrets": SENTINEL},
    )
    part = a2a_types.Part(root=root)
    msg = _msg(metadata={"authorization": SENTINEL, "prompt": "q"}, parts=[part])
    ctx = _ctx(metadata={"credentials": SENTINEL}, message=msg)

    payload = _payload_from_context(ctx)
    assert SENTINEL not in json.dumps(payload)
    # legitimate structured input from the DataPart still survives
    assert payload.get("topic") == "obesity"
    assert payload.get("prompt") == "q"


# --- End to end: SkeletonAgentExecutor -> tool loop -> call_endpoint ------
#
# How a skeleton-engine tool authenticates today: call_endpoint / typed
# operation tools read an operator token from the env var NAMED by
# EndpointConfig.auth_env, at call time, outside the model's view. No tool reads
# request metadata, so stripping "credentials" from the payload removes nothing
# a tool uses. This test pins both halves.


class _FakeCompletions:
    """Records every chat.completions.create call; scripts a tool call then an answer."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            call = SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(
                    name="call_endpoint", arguments=json.dumps({"request": "look it up"})
                ),
            )
            message = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
        else:
            message = SimpleNamespace(
                role="assistant",
                content=json.dumps({"answer": "done", "tools_used": ["call_endpoint"]}),
                tool_calls=None,
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _n):
        return b'{"result": "ok"}'


class _FakeOpener:
    """Stands in for call_endpoint's urllib opener; records the outbound request."""

    def __init__(self) -> None:
        self.requests: list = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return _FakeResponse()


class _FakeUpdater:
    def __init__(self) -> None:
        self.artifacts: list = []
        self.final: str | None = None

    async def start_work(self):
        pass

    async def add_artifact(self, parts, **_kw):
        self.artifacts.extend(parts)

    async def complete(self, message=None):
        self.final = "completed"

    async def failed(self, message=None):
        self.final = "failed"

    async def requires_input(self, message=None, final=True):
        self.final = "input_required"

    async def update_status(self, *_a, **_kw):
        pass

    def new_agent_message(self, parts, metadata=None):
        return {"parts": parts, "metadata": metadata}


def _llm_visible_text(calls: list[dict]) -> str:
    # Everything the model provider would receive, including the assistant
    # turns and tool results the loop resends each step.
    return json.dumps(calls, default=lambda o: vars(o) if hasattr(o, "__dict__") else str(o))


def test_executor_keeps_credentials_out_of_llm_but_tool_still_authenticates(monkeypatch):
    a2a_types = pytest.importorskip("a2a.types")
    from a2a.server.agent_execution import RequestContext

    completions = _FakeCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(llm_loop, "build_openai_client", lambda: fake_client)
    opener = _FakeOpener()
    monkeypatch.setattr(call_endpoint_mod, "_OPENER", opener)
    monkeypatch.setenv("SKELETON_TEST_UPSTREAM_TOKEN", UPSTREAM_TOKEN)
    updater = _FakeUpdater()
    monkeypatch.setattr(executor_mod, "task_updater", lambda *_a: updater)

    spec = endpoint_wrapper_spec(
        name="Lookup",
        endpoint=EndpointConfig(
            url="http://upstream.example.test/x", auth_env="SKELETON_TEST_UPSTREAM_TOKEN"
        ),
    )
    # The planner puts per-user credentials in params.metadata (context.metadata);
    # also plant them in message metadata and a DataPart to cover every entry point.
    message = a2a_types.Message(
        role=a2a_types.Role.user,
        message_id="m-1",
        parts=[
            a2a_types.Part(root=a2a_types.TextPart(text="find the thing")),
            a2a_types.Part(
                root=a2a_types.DataPart(data={"topic": "obesity", "token": SENTINEL})
            ),
        ],
        metadata={"Authorization": f"Bearer {SENTINEL}"},
    )
    context = RequestContext(
        request=a2a_types.MessageSendParams(
            message=message,
            metadata={
                "user_id": "user@example.test",
                "credentials": {"ncbi_api_key": {"api_key": SENTINEL}},
            },
        ),
        task_id="t-1",
        context_id="c-1",
    )

    agent = executor_mod.SkeletonAgentExecutor(model="test-model", spec=spec)
    asyncio.run(agent.execute(context, event_queue=None))

    assert updater.final == "completed"
    # 1) Nothing sent to the LLM carries the request credential (or the upstream token).
    assert len(completions.calls) == 2
    sent = _llm_visible_text(completions.calls)
    assert SENTINEL not in sent
    assert UPSTREAM_TOKEN not in sent
    assert "find the thing" in sent and "obesity" in sent  # ordinary input still flows
    # 2) The tool still authenticated upstream with its own credential.
    assert len(opener.requests) == 1
    assert opener.requests[0].get_header("Authorization") == f"Bearer {UPSTREAM_TOKEN}"
    assert SENTINEL not in (opener.requests[0].data or b"").decode()
    # 3) The result artifact returned to the caller does not echo it either.
    artifacts = json.dumps([p.model_dump(mode="json") for p in updater.artifacts])
    assert SENTINEL not in artifacts
