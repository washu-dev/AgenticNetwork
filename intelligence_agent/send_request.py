#!/usr/bin/env python3
"""Send a single A2A ``message/send`` request to the autonomous intelligence agent.

The intelligence agent runs an LLM agent loop with three tools:
``discover_agents`` (queries the AGNTCY Directory), ``query_peer_agent``
(calls a discovered peer over A2A — typically a recon/spider-robots agent
or an OSINT scout agent), and ``web_search`` (Brave Search). The user
writes natural language; the agent decides which peers to ask, what to
search for, and how to synthesize the brief.

Examples (run from anywhere; this script has no AgenticNetwork dependency)::

    python send_request.py \\
        --prompt "Brief me on grid square ALPHA-3 — current contacts, civilian \\
                  presence, and recommended COA."

    python send_request.py \\
        --prompt "Has the recon team observed any new armor in sector 7 in the \\
                  last 30 minutes?"

    python send_request.py --prompt "..." --raw

The agent's reply, peer_calls, web_search_calls, and tool-call trace come
back in a structured artifact. Use ``--raw`` to dump the full response.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

import httpx


def _build_payload(prompt: str, context_id: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "kind": "message",
        "messageId": f"msg-{uuid.uuid4().hex[:8]}",
        "role": "user",
        "parts": [{"kind": "text", "text": prompt}],
        "metadata": {},
    }
    if context_id:
        message["contextId"] = context_id
    return {
        "jsonrpc": "2.0",
        "id": f"send-{uuid.uuid4().hex[:8]}",
        "method": "message/send",
        "params": {"message": message},
    }


def _extract_artifact_data(result: dict[str, Any]) -> dict[str, Any] | None:
    for artifact in result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if not isinstance(part, dict):
                continue
            data = part.get("data")
            if isinstance(data, dict):
                return data
    return None


def _print_summary(result: dict[str, Any]) -> None:
    data = _extract_artifact_data(result) or {}
    agent = data.get("agent") or {}
    state = (result.get("status") or {}).get("state")
    print(f"  task_id          : {result.get('id')}")
    print(f"  context_id       : {result.get('contextId')}")
    print(f"  status           : {state}")
    print(f"  agent.exit_reason: {agent.get('exit_reason')}")
    print(f"  agent.steps      : {agent.get('n_steps')}")
    print(f"  agent.elapsed_s  : {agent.get('elapsed_s')}")

    final = (agent.get("final_message") or "").strip()
    if final:
        print("  agent.final_message:")
        for line in final.splitlines():
            print(f"    {line}")

    tool_calls = agent.get("tool_calls") or []
    print(f"  tool_calls ({len(tool_calls)}):")
    for tc in tool_calls:
        err = f" ERROR={tc.get('error')}" if tc.get("error") else ""
        print(f"    - {tc.get('name')} args={tc.get('args_keys')}{err}")

    peer_calls = data.get("peer_calls") or []
    if peer_calls:
        print(f"  peer_calls ({len(peer_calls)}):")
        for p in peer_calls:
            reply = (p.get("reply_text") or "").strip().replace("\n", " ")
            if len(reply) > 120:
                reply = reply[:117] + "..."
            print(
                f"    - status={p.get('status')} skill={p.get('skill_query')} "
                f"agent={p.get('agent_id')} reply={reply!r}"
            )

    web_calls = data.get("web_search_calls") or []
    if web_calls:
        print(f"  web_search_calls ({len(web_calls)}):")
        for w in web_calls:
            print(
                f"    - status={w.get('status')} query={w.get('query')!r} "
                f"results={w.get('count', 0)}"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a single A2A message/send request to the autonomous intelligence agent."
    )
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:9105/",
        help="A2A endpoint URL. Default: %(default)s",
    )
    parser.add_argument("--prompt", required=True, help="Natural-language request.")
    parser.add_argument(
        "--context-id",
        default=None,
        help="A2A context_id to continue an existing conversation.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the full JSON-RPC response to stdout instead of a summary.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="HTTP timeout in seconds. Default: %(default)s",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = _build_payload(args.prompt, context_id=args.context_id)

    suffix = f" context_id={args.context_id}" if args.context_id else ""
    print(f"POST {args.endpoint}{suffix}")
    try:
        response = httpx.post(args.endpoint, json=payload, timeout=args.timeout)
    except httpx.HTTPError as exc:
        print(f"transport error: {exc}", file=sys.stderr)
        return 2

    if response.status_code != 200:
        print(f"HTTP {response.status_code}: {response.text}", file=sys.stderr)
        return 2

    body = response.json()

    if args.raw:
        json.dump(body, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if "error" in body:
        print("JSON-RPC error:")
        print(json.dumps(body["error"], indent=2))
        return 1

    result = body.get("result") or {}
    print("response:")
    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
