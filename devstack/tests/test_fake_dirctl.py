"""Fake dirctl, driven as a black box through devstack/bin/dirctl.

Covers the commands the repository calls (push, pull, routing publish/list/
search/unpublish/info, search, delete, events listen), the stored-vs-routed
distinction, the routing delay and concurrent writers.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[1] / "bin" / "dirctl"
ADDR = ["--server-addr", "127.0.0.1:8888"]


def record(name: str, skill: str = "nlp/natural_language_generation/text_summarization") -> dict:
    return {
        "name": name,
        "version": "v1.0.0",
        "schema_version": "0.7.0",
        "skills": [{"name": skill, "id": 10202}],
        "locators": [{"type": "a2a", "url": f"http://127.0.0.1:1/{name}"}],
    }


@pytest.fixture
def env(tmp_path):
    return {**os.environ, "FAKE_DIRCTL_STATE": str(tmp_path / "directory.json")}


def dirctl(env: dict, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(SHIM), *ADDR, *args], env=env, capture_output=True, text=True, timeout=30
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def push(env: dict, tmp_path: Path, rec: dict) -> str:
    path = tmp_path / f"{rec['name']}.json"
    path.write_text(json.dumps(rec), encoding="utf-8")
    return dirctl(env, "push", str(path), "--output", "raw").stdout.strip()


def routed_cids(env: dict, *args: str) -> list[str]:
    items = json.loads(dirctl(env, "routing", "list", *args, "--output", "json").stdout)
    return [item["record_ref"]["cid"] for item in items]


def test_shim_is_executable():
    result = subprocess.run([str(SHIM), "version"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "fake" in result.stdout


def test_push_is_content_addressed_and_pull_returns_the_record(env, tmp_path):
    rec = record("alpha")
    cid = push(env, tmp_path, rec)
    assert cid.startswith("bae")
    assert push(env, tmp_path, rec) == cid  # same record, same CID
    assert push(env, tmp_path, record("beta")) != cid
    assert json.loads(dirctl(env, "pull", cid, "--output", "json").stdout) == rec


def test_stored_is_not_routed_until_published(env, tmp_path):
    cid = push(env, tmp_path, record("alpha"))
    assert routed_cids(env) == []
    assert json.loads(dirctl(env, "routing", "search", "--limit", "200", "--output", "json").stdout) == []
    assert json.loads(dirctl(env, "search", "--limit", "200", "--output", "json").stdout) == [cid]

    dirctl(env, "routing", "publish", cid, "--output", "json")
    assert routed_cids(env) == [cid]
    assert routed_cids(env, "--cid", cid) == [cid]
    assert routed_cids(env, "--cid", "bae-other") == []
    items = json.loads(dirctl(env, "routing", "list", "--output", "json").stdout)
    assert "/skills/nlp/natural_language_generation/text_summarization" in items[0]["labels"]


def test_routing_search_filters_by_skill(env, tmp_path):
    text = push(env, tmp_path, record("alpha"))
    vision = push(env, tmp_path, record("beta", skill="images_computer_vision/image_classification"))
    for cid in (text, vision):
        dirctl(env, "routing", "publish", cid)

    def search(*args: str, limit: str = "200") -> list[str]:
        out = dirctl(env, "routing", "search", *args, "--limit", limit, "--output", "json").stdout
        return [item["record_ref"]["cid"] for item in json.loads(out)]

    assert search("--skill", "nlp") == [text]  # hierarchical prefix
    assert search("--skill", "*classification") == [vision]  # wildcard
    assert search("--skill", "audio") == []
    assert sorted(search()) == sorted([text, vision])
    assert len(search(limit="1")) == 1


def test_unpublish_keeps_the_record_and_delete_removes_it(env, tmp_path):
    cid = push(env, tmp_path, record("alpha"))
    dirctl(env, "routing", "publish", cid)
    dirctl(env, "routing", "unpublish", cid)
    assert routed_cids(env) == []
    assert json.loads(dirctl(env, "pull", cid, "--output", "json").stdout)["name"] == "alpha"

    dirctl(env, "delete", cid, "--output", "json")
    missing = dirctl(env, "pull", cid, "--output", "json", check=False)
    assert missing.returncode == 1 and missing.stderr.startswith("Error:") and missing.stdout == ""
    assert json.loads(dirctl(env, "search", "--output", "json").stdout) == []


def test_route_delay_keeps_a_published_record_pending(env, tmp_path):
    env = {**env, "FAKE_DIRCTL_ROUTE_DELAY_SECONDS": "1.0"}
    cid = push(env, tmp_path, record("alpha"))
    dirctl(env, "routing", "publish", cid)
    assert routed_cids(env) == []
    info = json.loads(dirctl(env, "routing", "info", "--output", "json").stdout)
    assert (info["stored_records"], info["routed_records"], info["pending_records"]) == (1, 0, 1)
    deadline = time.monotonic() + 10
    while routed_cids(env) != [cid]:
        assert time.monotonic() < deadline, "record never became routed"
        time.sleep(0.2)


def test_failures_are_loud(env, tmp_path):
    unknown_flag = dirctl(env, "routing", "list", "--bogus", "x", check=False)
    assert unknown_flag.returncode == 2 and "unknown flag" in unknown_flag.stderr
    unknown_command = dirctl(env, "frobnicate", check=False)
    assert unknown_command.returncode == 2
    no_state = {k: v for k, v in env.items() if k != "FAKE_DIRCTL_STATE"}
    assert dirctl(no_state, "routing", "list", check=False).returncode == 1
    assert dirctl(env, "routing", "publish", "bae-missing", check=False).returncode == 1


def test_concurrent_pushes_from_many_processes_all_land(env, tmp_path):
    count = 12
    procs = []
    for i in range(count):
        path = tmp_path / f"rec{i}.json"
        path.write_text(json.dumps(record(f"agent-{i}")), encoding="utf-8")
        procs.append(
            subprocess.Popen(
                [sys.executable, str(SHIM), *ADDR, "push", str(path), "--output", "raw"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        )
    cids = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        cids.append(out.strip())
    assert len(set(cids)) == count
    stored = json.loads(dirctl(env, "search", "--output", "json").stdout)
    assert sorted(stored) == sorted(cids)
    json.loads(Path(env["FAKE_DIRCTL_STATE"]).read_text())  # still valid JSON


def test_events_listen_streams_publications(env, tmp_path):
    cid = push(env, tmp_path, record("alpha"))
    listener = subprocess.Popen(
        [sys.executable, str(SHIM), *ADDR, "events", "listen", "--types", "RECORD_PUBLISHED", "--output", "jsonl"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        time.sleep(0.5)  # let it record the starting event id
        dirctl(env, "routing", "publish", cid)
        assert select.select([listener.stdout], [], [], 10)[0], "no event within 10 s"
        event = json.loads(listener.stdout.readline())
        assert event["type"] == "EVENT_TYPE_RECORD_PUBLISHED" and event["resource_id"] == cid
    finally:
        listener.terminate()
        listener.wait(timeout=10)
