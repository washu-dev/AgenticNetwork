"""Per-user credentials and the user's identity stay out of LLM prompts and agent logs.

The planner injects each user's credentials into A2A request metadata under
"credentials", and the login identity (an email under SSO) under "user_id". These
tests drive the agents registration runs (llm / http operation tools via
``LLMAgentExecutor``; proxy via ``ProxyExecutor``) and the runner's config
parsing, and check that a sentinel secret never reaches the model, a log line or
the returned artifact and the identity never reaches the model or the upstream,
while an operation tool still authenticates with its own token. No network: the
LLM client, the upstream opener and the proxy's HTTP call are faked. The
skeleton's own executor is covered by agent_skeleton/tests/test_executor_metadata.py.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from a2a.server.agent_execution import RequestContext  # noqa: E402
from a2a.types import (  # noqa: E402
    DataPart,
    Message,
    MessageSendParams,
    Part,
    Role,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)

from agent_skeleton import llm_loop  # noqa: E402
from agent_skeleton.spec import llm_wrapper_spec, operation_wrapper_spec  # noqa: E402
from agent_skeleton.system_tools import call_endpoint as call_endpoint_mod  # noqa: E402
from agent_skeleton.system_tools.call_endpoint import EndpointConfig, OperationConfig  # noqa: E402
from registration_service.handlers.proxy_handler import ProxyExecutor  # noqa: E402
from registration_service.handlers.llm_handler import LLMAgentExecutor  # noqa: E402

SENTINEL = "TEST_SECRET_DO_NOT_LEAK"
UPSTREAM_TOKEN = "TEST_UPSTREAM_TOKEN_DO_NOT_LEAK"
USER_EMAIL = "user@example.test"


class _Queue:
    """Minimal EventQueue stand-in: captures whatever the TaskUpdater enqueues."""

    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


def _context_with_credentials() -> RequestContext:
    # Credentials ride in params.metadata (context.metadata), as the planner sends
    # them; also plant them in message metadata and a DataPart.
    message = Message(
        role=Role.user,
        message_id="m-1",
        parts=[
            Part(root=TextPart(text="what is the weather in Paris")),
            Part(root=DataPart(data={"city": "Paris", "credentials": {"k": SENTINEL}})),
        ],
        metadata={"authorization": f"Bearer {SENTINEL}"},
    )
    return RequestContext(
        request=MessageSendParams(
            message=message,
            metadata={
                "user_id": USER_EMAIL,
                "credentials": {"weather_api_key": {"api_key": SENTINEL}},
            },
        ),
        task_id="task-1",
        context_id="ctx-1",
    )


class _FakeCompletions:
    """Records every chat.completions.create call. With ``tool`` set, the first
    reply calls that tool; the next reply is the final answer."""

    def __init__(self, tool: tuple[str, dict] | None = None) -> None:
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
                role="assistant", content=json.dumps({"answer": "sunny"}), tool_calls=None
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _n):
        return b'{"forecast": "sunny"}'


class _FakeOpener:
    def __init__(self) -> None:
        self.requests: list = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return _FakeResponse()


def _dump(obj) -> str:
    return json.dumps(obj, default=lambda o: vars(o) if hasattr(o, "__dict__") else str(o))


def _events_text(events) -> str:
    return json.dumps([e.model_dump(mode="json") for e in events])


def _llm_spec():
    return llm_wrapper_spec(name="Weather", system_prompt="Answer weather questions.")


def _operation_spec():
    base = EndpointConfig(url="http://weather.example.test", auth_env="PROMPT_SECRETS_TEST_TOKEN")
    op = OperationConfig(
        name="get_weather",
        method="GET",
        path="/weather",
        params={"city": {"type": "string"}},
        required=["city"],
    )
    return operation_wrapper_spec(name="Weather", base=base, operations=[op])


@pytest.mark.parametrize(
    "make_spec, tool",
    [(_llm_spec, None), (_operation_spec, ("get_weather", {"city": "Paris"}))],
    ids=["llm", "http-operations"],
)
async def test_llm_executor_keeps_credentials_out_of_prompt(
    monkeypatch, caplog, make_spec, tool
) -> None:
    completions = _FakeCompletions(tool)
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(llm_loop, "build_openai_client", lambda: fake_client)
    opener = _FakeOpener()
    monkeypatch.setattr(call_endpoint_mod, "_OPENER", opener)
    monkeypatch.setenv("PROMPT_SECRETS_TEST_TOKEN", UPSTREAM_TOKEN)
    caplog.set_level(logging.DEBUG)

    q = _Queue()
    await LLMAgentExecutor("weather", make_spec(), "test-model").execute(
        _context_with_credentials(), q
    )

    assert any(
        isinstance(e, TaskStatusUpdateEvent) and e.status.state == TaskState.completed
        for e in q.events
    )
    sent = _dump(completions.calls)
    assert "what is the weather in Paris" in sent
    assert SENTINEL not in sent and UPSTREAM_TOKEN not in sent
    # Only the user's text reaches the model: no request metadata, so no identity.
    assert USER_EMAIL not in sent
    assert SENTINEL not in _events_text(q.events)
    assert SENTINEL not in caplog.text
    if tool:
        # The operation tool still authenticated upstream with its own token.
        assert len(opener.requests) == 1
        assert opener.requests[0].get_header("Authorization") == f"Bearer {UPSTREAM_TOKEN}"
        assert SENTINEL not in opener.requests[0].full_url


async def test_proxy_keeps_credentials_out_of_logs_and_artifact(monkeypatch, caplog) -> None:
    seen: dict = {}

    async def fake_forward(self, payload, headers):
        seen.update(payload=payload, headers=headers)
        return {"result": {"status": {"message": {"parts": [{"kind": "text", "text": "ok"}]}}}}

    monkeypatch.setattr(ProxyExecutor, "_forward", fake_forward)
    caplog.set_level(logging.DEBUG)

    q = _Queue()
    await ProxyExecutor("proxy", "http://upstream.example.test/a2a", "Bearer operator").execute(
        _context_with_credentials(), q
    )

    # Today a proxy forwards only the user's text plus its own operator auth
    # header; it never relays request metadata. Change this deliberately if
    # proxies are ever meant to pass per-user credentials upstream.
    assert SENTINEL not in json.dumps(seen["payload"])
    assert USER_EMAIL not in json.dumps(seen["payload"])
    assert seen["headers"]["Authorization"] == "Bearer operator"
    assert SENTINEL not in _events_text(q.events)
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize(
    "handler_type, config, missing",
    [
        ("proxy", {"auth_header": f"Bearer {SENTINEL}"}, "upstream_url"),
        # Short key: pydantic elides the middle of long input reprs, which would
        # hide most of the sentinel but still leak part of a real secret.
        ("custom", {"extra": {"key": SENTINEL}}, "class_name"),
    ],
)
def test_runner_bad_config_log_omits_config_values(
    monkeypatch, caplog, handler_type, config, missing
) -> None:
    from registration_service import runner

    envelope = {"agent_id": "a1", "handler_type": handler_type, "port": 22001, "config": config}
    monkeypatch.setenv("AGENT_CONFIG_JSON", json.dumps(envelope))
    monkeypatch.delenv("AGENT_PARENT_PID", raising=False)
    caplog.set_level(logging.INFO)

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 1
    assert "runner_bad_config" in caplog.text and missing in caplog.text
    assert SENTINEL not in caplog.text
