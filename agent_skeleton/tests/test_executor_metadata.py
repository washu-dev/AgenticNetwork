"""Regression tests for the metadata leak fixes in executor.py.

Based on a contribution by raneemousa3 (agent-skeleton PR #29). These verify that
reserved private keys (credentials) and the signed-in user's identity in
request/message/part metadata never reach the model-visible payload built by
_payload_from_context(), that skill hints and task input still do, and that a
tool which authenticates upstream still gets its own credential. Uses sentinel
secrets and example.test identities only, and no network (the LLM client and the
upstream HTTP opener are both faked).

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
USER_EMAIL = "user@example.test"


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


def test_helper_strips_identity_keys_case_insensitive():
    out = _model_visible_metadata(
        {
            "user_id": USER_EMAIL,
            "Auth_User_Id": "0b4f-uuid",
            "mail": USER_EMAIL,
            "EPPN": USER_EMAIL,
            "remote_user": "user",
            "remote-user": "user",
            "skill": "s/x",
        }
    )
    assert out == {"skill": "s/x"}


def test_planner_shaped_request_keeps_identity_out_but_task_input_flows():
    # The metadata the AgenticNetwork planner attaches to every call_agent
    # dispatch (planner runtime._build_a2a_request + a2a.adapters): the login
    # identity, the channel, the task input spread into params.metadata, scoped
    # credentials, the skill hint trio and routing fields. The task input also
    # rides as a DataPart.
    a2a_types = pytest.importorskip("a2a.types")
    task_input = {"topic": "obesity", "max_results": 5}
    message = _msg(
        metadata={
            "target_agent_id": "lookup-agent",
            "requested_task_id": "t-1",
            "skill": "lookup/search",
            "requested_skill": "lookup/search",
            "skill_hint": "lookup/search",
        },
        parts=[
            a2a_types.Part(root=a2a_types.TextPart(text="find papers on obesity")),
            a2a_types.Part(root=a2a_types.DataPart(data=dict(task_input))),
        ],
    )
    ctx = _ctx(
        metadata={
            "user_id": USER_EMAIL,
            "channel": "web",
            **task_input,
            "credentials": {"ncbi_api_key": {"api_key": SENTINEL}},
            "skill": "lookup/search",
            "requested_skill": "lookup/search",
            "skill_hint": "lookup/search",
            "target_agent_id": "lookup-agent",
            "user_goal": "find papers on obesity",
        },
        message=message,
    )

    payload = _payload_from_context(ctx)
    dumped = json.dumps(payload)
    assert USER_EMAIL not in dumped and SENTINEL not in dumped
    assert "user_id" not in payload
    assert payload["topic"] == "obesity" and payload["max_results"] == 5
    assert payload["skill_hint"] == "lookup/search"
    assert payload["channel"] == "web"
    # skill routing still reads the raw request metadata
    assert executor_mod._skill_from_context(ctx) == "lookup/search"


def test_task_input_named_like_identity_still_reaches_model_via_datapart():
    # A skill whose input has a field literally named `user_id` (here an
    # upstream account handle). The planner keeps the login identity in
    # params.metadata["user_id"] and leaves a task-input `user_id` out of the
    # metadata spread, so the DataPart is its only carrier. Only metadata loses
    # identity keys; DataPart data must keep them or that input is dropped.
    a2a_types = pytest.importorskip("a2a.types")
    message = _msg(
        parts=[
            a2a_types.Part(root=a2a_types.TextPart(text="look up this account")),
            a2a_types.Part(root=a2a_types.DataPart(data={"user_id": "octocat"})),
        ],
    )
    ctx = _ctx(metadata={"user_id": USER_EMAIL, "channel": "web"}, message=message)

    payload = _payload_from_context(ctx)
    assert payload["user_id"] == "octocat"
    assert USER_EMAIL not in json.dumps(payload)


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
    """Records every chat.completions.create call. With ``tool`` set, the first
    reply calls that tool; the next reply (or the only one) is the final answer."""

    def __init__(self, tool: tuple[str, dict] | None = ("call_endpoint", {"request": "look it up"})) -> None:
        self.calls: list[dict] = []
        self._tool = tool

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._tool and len(self.calls) == 1:
            name, args = self._tool
            call = SimpleNamespace(
                id="call-1", function=SimpleNamespace(name=name, arguments=json.dumps(args))
            )
            message = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
        else:
            message = SimpleNamespace(
                role="assistant",
                content=json.dumps({"answer": "done", "tools_used": [self._tool[0]] if self._tool else []}),
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
                "user_id": USER_EMAIL,
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
    assert USER_EMAIL not in sent  # nor the signed-in user's identity
    assert "find the thing" in sent and "obesity" in sent  # ordinary input still flows
    # 2) The tool still authenticated upstream with its own credential.
    assert len(opener.requests) == 1
    assert opener.requests[0].get_header("Authorization") == f"Bearer {UPSTREAM_TOKEN}"
    assert SENTINEL not in (opener.requests[0].data or b"").decode()
    # 3) The result artifact returned to the caller does not echo it either.
    artifacts = json.dumps([p.model_dump(mode="json") for p in updater.artifacts])
    assert SENTINEL not in artifacts


# --- The cluster / Cloud Run tool-loop image (container_runner) ------------
#
# An uploaded tool loop runs locally through registration's LLMAgentExecutor,
# which sends the model only the user's text. In the per-agent image the same
# agent is served by container_runner._serve_tool_loop -> create_app ->
# SkeletonAgentExecutor, which merges request metadata into the prompt. Drive
# that app over its real A2A JSON-RPC route.

_TOOLS_MODULE = """
TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo the text back.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
}]
TOOL_REGISTRY = {"echo": lambda text="": {"echo": text}}
"""


def test_container_tool_loop_keeps_identity_and_credentials_out_of_llm(monkeypatch, tmp_path):
    pytest.importorskip("a2a.types")
    httpx = pytest.importorskip("httpx")
    uvicorn = pytest.importorskip("uvicorn")
    from agent_skeleton import container_runner

    tools = tmp_path / "tools.py"
    tools.write_text(_TOOLS_MODULE)
    monkeypatch.setenv("HANDLER_FILE_PATH", str(tools))
    served: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **_kw: served.setdefault("app", app))
    completions = _FakeCompletions(tool=("echo", {"text": "hi"}))
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(llm_loop, "build_openai_client", lambda: fake_client)

    container_runner._serve_tool_loop(
        {
            "agent_id": "lookup",
            "port": 22001,
            "handler_type": "llm",
            "config": {
                "name": "Lookup",
                "system_prompt": "Answer briefly.",
                "model": "test-model",
                "tools": {"source": "upload", "entry_file": "tools.py"},
            },
        }
    )

    request = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": "m-1",
                "parts": [
                    {"kind": "text", "text": "find the thing"},
                    {"kind": "data", "data": {"topic": "obesity"}},
                ],
                "metadata": {"skill_hint": "lookup/respond"},
            },
            "metadata": {
                "user_id": USER_EMAIL,
                "channel": "web",
                "topic": "obesity",
                "skill": "lookup/respond",
                "credentials": {"ncbi_api_key": {"api_key": SENTINEL}},
            },
        },
    }

    async def send() -> dict:
        transport = httpx.ASGITransport(app=served["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            resp = await client.post("/", json=request)
        assert resp.status_code == 200, resp.text
        return resp.json()

    body = asyncio.run(send())
    assert body["result"]["status"]["state"] == "completed", body
    assert len(completions.calls) == 2
    sent = _llm_visible_text(completions.calls)
    assert USER_EMAIL not in sent and SENTINEL not in sent
    assert "find the thing" in sent and "obesity" in sent and "lookup/respond" in sent
    assert USER_EMAIL not in json.dumps(body) and SENTINEL not in json.dumps(body)
