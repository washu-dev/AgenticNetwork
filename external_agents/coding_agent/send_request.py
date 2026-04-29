#!/usr/bin/env python3
"""Send a single A2A ``message/send`` request to the autonomous coding agent.

The coding agent runs an LLM agent loop with three tools: ``run_coder``
(delegates to mini-swe-agent), ``discover_agents`` (queries the AGNTCY
Directory), and ``send_email_via_directory`` (calls a discovered email agent).
The user only writes natural language — the agent decides when to write code
and whether to email anyone.

Examples (run from anywhere; this script has no AgenticNetwork dependency)::

    python send_request.py \
        --prompt "Write fizzbuzz.py that prints fizzbuzz from 1 to 100, then run it."

    python send_request.py \
        --prompt "Read the existing module and add a docstring." \
        --input-file local_src/foo.py:src/foo.py \
        --save-files build/coding_out

    python send_request.py \
        --prompt "Write a Python script that prints 'hello world' and email it to alice@example.com" \
        --save-files build/coding_out

If the agent pauses for confirmation (e.g. the email agent requires user
approval), the response state will be ``input_required``. Continue the
conversation by re-running with ``--context-id <returned-id>`` and the
user's reply as the new prompt::

    python send_request.py --prompt "yes please send it" --context-id <id>
    python send_request.py --prompt "no, cancel" --context-id <id>

The agent's reply, generated_files, delivery results, and tool-call trace come
back in a structured artifact. Use ``--raw`` to dump the full response.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx


def _parse_input_file(spec: str) -> tuple[Path, str]:
    """``HOST_PATH[:WORKSPACE_PATH]`` → (host_path, workspace_path)."""
    host_part, sep, ws_part = spec.partition(":")
    if not host_part.strip():
        raise argparse.ArgumentTypeError(
            f"--input-file expects HOST_PATH[:WORKSPACE_PATH], got {spec!r}"
        )
    host = Path(host_part.strip())
    workspace = (ws_part.strip() if sep else "") or host.name
    return host, workspace


def _build_source_files(specs: list[tuple[Path, str]]) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for host, ws in specs:
        try:
            content = host.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SystemExit(
                f"--input-file {host} is not UTF-8 text; binary seeding via this "
                f"helper isn't supported: {exc}"
            )
        payload.append({"path": ws, "content": content})
    return payload


def _build_payload(
    prompt: str,
    source_files: list[dict[str, str]],
    context_id: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if source_files:
        metadata["source_files"] = source_files
    message: dict[str, Any] = {
        "kind": "message",
        "messageId": f"msg-{uuid.uuid4().hex[:8]}",
        "role": "user",
        "parts": [{"kind": "text", "text": prompt}],
        "metadata": metadata,
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
    n_cont = agent.get("n_continuations")
    if n_cont:
        print(f"  agent.n_continuations: {n_cont}")
    if state == "input_required":
        pending = agent.get("pending") or {}
        status_msg = (result.get("status") or {}).get("message") or {}
        prompt_parts = status_msg.get("parts") or []
        prompt_text = ""
        for p in prompt_parts:
            if isinstance(p, dict) and p.get("kind") == "text":
                prompt_text = p.get("text") or ""
                break
        print("  ---- AWAITING YOUR REPLY ----")
        if prompt_text:
            for line in prompt_text.splitlines():
                print(f"    {line}")
        print(f"  pending tool     : {pending.get('tool_name')}")
        print(f"  to continue, run :")
        print(
            f"    python send_request.py --prompt 'yes' "
            f"--context-id {result.get('contextId')}"
        )
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
    files = data.get("generated_files") or []
    print(f"  generated_files ({len(files)}):")
    for entry in files:
        truncated = " (truncated)" if entry.get("content_truncated") else ""
        print(
            f"    - {entry.get('path')} [{entry.get('action')}, "
            f"{entry.get('size_bytes')} bytes]{truncated}"
        )
    deliveries = data.get("delivery_results") or []
    if deliveries:
        print(f"  delivery_results ({len(deliveries)}):")
        for d in deliveries:
            print(f"    - status={d.get('status')} to={d.get('to')} subject={d.get('subject')!r}")


def _save_generated_files(result: dict[str, Any], out_dir: Path) -> None:
    data = _extract_artifact_data(result) or {}
    files = data.get("generated_files") or []
    if not files:
        print(f"  (no generated_files to save under {out_dir})")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for entry in files:
        path = entry.get("path")
        if not path or entry.get("action") == "deleted":
            continue
        content = entry.get("content")
        if content is None:
            print(f"  skipping {path}: no inline content (truncated or binary)")
            continue
        target = out_dir / path
        try:
            target.resolve().relative_to(out_dir.resolve())
        except ValueError:
            print(f"  skipping {path}: would escape {out_dir}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        saved += 1
    print(f"  saved {saved} file(s) under {out_dir}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a single A2A message/send request to the autonomous coding agent."
    )
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:9106/",
        help="A2A endpoint URL. Default: %(default)s",
    )
    parser.add_argument("--prompt", required=True, help="Natural-language request.")
    parser.add_argument(
        "--context-id",
        default=None,
        help=(
            "A2A context_id to continue an existing conversation (e.g. to "
            "answer a confirmation prompt the agent paused on). Leave unset "
            "to start a fresh task."
        ),
    )
    parser.add_argument(
        "--input-file",
        action="append",
        default=[],
        type=_parse_input_file,
        help=(
            "Repeatable. ``HOST_PATH[:WORKSPACE_PATH]`` — read host text file and seed it "
            "into the agent's scratch workspace at WORKSPACE_PATH (defaults to host basename). "
            "The agent will see these in its first run_coder call if it chooses to read them."
        ),
    )
    parser.add_argument(
        "--save-files",
        type=Path,
        default=None,
        help="Write any returned generated_files (with inline content) into this directory.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the full JSON-RPC response to stdout instead of a summary.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="HTTP timeout in seconds. Default: %(default)s",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_files = _build_source_files(args.input_file)
    payload = _build_payload(args.prompt, source_files, context_id=args.context_id)

    suffix = f" context_id={args.context_id}" if args.context_id else ""
    print(f"POST {args.endpoint}  (files seeded={len(source_files)}{suffix})")
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

    if args.save_files is not None:
        _save_generated_files(result, args.save_files)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
