#!/usr/bin/env bash
# Tier-2 smoke test for the Compose deployment (docs/deployment/COMPOSE.md): real
# Postgres, the real dirctl daemon, the real images.
#
# Through the front door, the way tests/pipeline/test_full_chain.py drives the dev
# stack: wait until the stack is ready and the seed agent is listed, send one prompt
# the chat UI's way (POST /messages/stream), check that the planner discovered the
# seed agent in the directory and delegated to it, and that the answer is saved in
# the signed-in user's history, in Postgres.
#
#   deployment/compose/smoke.sh [--expect-conversation CONTEXT_ID]
#
#   --expect-conversation ID  also check that an earlier run's conversation is still
#                             saved (run it after `docker compose down` and `up`)
#   FRONT_DOOR_URL            default: the address `docker compose port net 8080` reports
#   SMOKE_TIMEOUT             seconds to wait for readiness (default 300)
#
# Runs from any directory. Needs python3 and `docker compose` for this project (it
# honours COMPOSE_PROJECT_NAME and deployment/compose/.env).
# Exit 0 when every step passes; otherwise 1, naming the failing step.
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "smoke: FAILED at $1: $2" >&2; exit 1; }

expect_cid=""
while [ $# -gt 0 ]; do
    case "$1" in
        --expect-conversation)
            [ $# -ge 2 ] || fail "usage" "--expect-conversation needs a context id"
            expect_cid="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) fail "usage" "unknown argument $1 (see --help)" ;;
    esac
done

command -v python3 >/dev/null || fail "preflight" "python3 not found"
docker compose ps --status running --services 2>/dev/null | grep -qx postgres \
    || fail "preflight" "postgres is not running in this compose project (docker compose up -d first)"

if [ -z "${FRONT_DOOR_URL:-}" ]; then
    addr="$(docker compose port net 8080 2>/dev/null || true)"
    [ -n "$addr" ] || fail "preflight" "no published front door (docker compose port net 8080)"
    FRONT_DOOR_URL="http://${addr/0.0.0.0/127.0.0.1}"
fi
echo "smoke: front door $FRONT_DOOR_URL"

# Steps 1-6 over HTTP; they leave KEY=VALUE lines in $out for steps 7-8.
out="$(mktemp)"
trap 'rm -f "$out"' EXIT
FRONT_DOOR_URL="$FRONT_DOOR_URL" EXPECT_CID="$expect_cid" SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-300}" \
    python3 - >"$out" <<'PY'
import contextlib, http.client, json, os, sys, time
from urllib.parse import urlsplit

BASE = os.environ["FRONT_DOOR_URL"].rstrip("/")
DEADLINE = time.monotonic() + float(os.environ["SMOKE_TIMEOUT"])
SEED = "Echo Assistant"
MARKER = "[mock-llm] Echo:"
PROMPT = "Echo this back and summarize it: the compose smoke test reached a specialist agent."


def fail(step, why):
    print(f"smoke: FAILED at {step}: {why}", file=sys.stderr)
    sys.exit(1)


def request(method, path, body=None, timeout=30):
    parts = urlsplit(BASE + path)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data is not None else {}
    try:
        conn.request(method, parts.path, body=data, headers=headers)
        return conn, conn.getresponse()
    except OSError as exc:
        conn.close()
        return None, exc


def call(method, path, body=None):
    conn, resp = request(method, path, body)
    if conn is None:
        return 0, str(resp)
    with contextlib.closing(conn):
        raw = resp.read()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, raw.decode("utf-8", "replace")[:300]


def wait_for(step, check):
    last = ""
    while True:
        ok, last = check()
        if ok:
            return
        if time.monotonic() >= DEADLINE:
            fail(step, f"not ready before the timeout: {last}")
        time.sleep(2)


def ready():
    status, body = call("GET", "/ready")
    if status != 200 or not isinstance(body, dict):
        return False, f"GET /ready -> {status}: {body}"
    checks = body.get("checks", {})
    if checks.get("directory", {}).get("backend") != "real" or checks.get("db", {}).get("status") != "ok":
        return False, f"expected the real directory and a database: {checks}"
    return True, ""


listed = {}


def seed_listed():
    status, body = call("GET", "/agents")
    if status != 200 or not isinstance(body, dict):
        return False, f"GET /agents -> {status}: {body}"
    listed.clear()
    listed.update({a.get("name"): a.get("agent_id") for a in body.get("agents", [])})
    return SEED in listed, f"the planner lists {sorted(listed) or 'nothing'}"


# 1. The stack answers through the front door, with the real directory and Postgres.
wait_for("ready", ready)
# 2. The seed agent is registered, published and discoverable.
wait_for("seed agent listed", seed_listed)

# 3. Signed in: the front door's identity creates (or finds) the user.
status, me = call("GET", "/me")
if status != 200 or not isinstance(me, dict) or not me.get("email"):
    fail("sign-in", f"GET /me -> {status}: {me}")

# 4. One prompt, sent the chat UI's way, delegated to the seed agent.
body = {"user_id": "demo-user", "channel": "api", "text": PROMPT, "context_id": None}
conn, resp = request("POST", "/messages/stream", body, timeout=180)
if conn is None:
    fail("chat", f"POST /messages/stream: {resp}")
events, name, data = [], "message", []
with contextlib.closing(conn):
    if resp.status != 200:
        fail("chat", f"POST /messages/stream -> {resp.status}: {resp.read()[:300]!r}")
    for raw in resp:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line and data:
            events.append((name, json.loads("\n".join(data))))
            name, data = "message", []
finals = [d for n, d in events if n == "final"]
if not finals:
    fail("chat", f"no final event; events: {[n for n, _ in events]}")
final = finals[-1]
tools = [s.get("tool") for s in final.get("tool_trace") or []]
if final.get("state") != "completed":
    fail("chat", f"planner state {final.get('state')!r}: {final.get('text')!r}")
if not {"list_agents", "call_agent", "final_answer"} <= set(tools):
    fail("discovery", f"the planner did not discover and call an agent (tools: {tools})")
found = next(s for s in final["tool_trace"] if s.get("tool") == "list_agents")
names = [a.get("name") for a in (found.get("content") or {}).get("agents", [])]
if SEED not in names:
    fail("discovery", f"list_agents did not return {SEED!r} (got {names})")
if final.get("selected_agent") != listed[SEED]:
    fail("delegation", f"selected {final.get('selected_agent')!r}, not {SEED!r} ({listed[SEED]!r})")
status, health = call("GET", "/health")
mock = isinstance(health, dict) and str(health.get("model", "")).startswith("mock-")
text = final.get("text") or ""
if mock and not (text.startswith(MARKER) and PROMPT in text):
    fail("answer", f"the answer lacks the seed agent's {MARKER!r} reply: {text!r}")

# 5. Saved in this user's history.
cid = final.get("context_id")
status, history = call("GET", f"/conversations/{cid}")
messages = [(m.get("role"), m.get("text")) for m in history.get("messages", [])] if isinstance(history, dict) else []
if not messages or messages[0] != ("user", PROMPT) or messages[-1] != ("assistant", text):
    fail("history", f"GET /conversations/{cid} -> {status}: {history}")
status, sessions = call("GET", "/sessions")
if not isinstance(sessions, dict) or cid not in [s.get("context_id") for s in sessions.get("sessions", [])]:
    fail("history", f"GET /sessions does not list {cid}: {status}: {sessions}")

# 6. An earlier conversation survived a restart.
expect = os.environ.get("EXPECT_CID", "")
if expect:
    status, old = call("GET", f"/conversations/{expect}")
    if not isinstance(old, dict) or not old.get("messages"):
        fail("persistence", f"earlier conversation {expect} -> {status}: {old}")

status, regs = call("GET", "/register/agents")
reg = next((a for a in regs if a.get("name") == SEED), {}) if status == 200 and isinstance(regs, list) else {}
print(f"user={me['email']}")
print(f"agent={listed[SEED]}")
print(f"registration_id={reg.get('id', '')}")
print(f"ads_cid={reg.get('ads_cid') or ''}")
print(f"context_id={cid}")
print(f"messages={len(messages)}")
print(f"answer={text}")
PY
result="$(cat "$out")"

value() { printf '%s\n' "$result" | sed -n "s/^$1=//p" | head -n1; }
user="$(value user)"; cid="$(value context_id)"; cid_ok="$(printf '%s' "$cid" | tr -cd 'A-Za-z0-9_-')"
ads_cid="$(value ads_cid)"
[ -n "$cid" ] && [ "$cid" = "$cid_ok" ] || fail "history" "unexpected context id '$cid'"

# 7. The seed agent's record is routed in the real dirctl daemon.
[ -n "$ads_cid" ] || fail "directory" "registration has no ads_cid for the seed agent"
routed="$(docker compose exec -T chat-app dirctl --server-addr localhost:8888 routing list --output json 2>&1)" \
    || fail "directory" "dirctl routing list failed: $routed"
printf '%s' "$routed" | grep -q "$ads_cid" || fail "directory" "record $ads_cid is not routed: $routed"

# 8. The turn is in Postgres (not only the planner's cache).
owner="$(printf '%s' "$user" | sed "s/'/''/g")"
rows="$(docker compose exec -T postgres psql -h /tmp/postgresql -U postgres -d registration -tAc \
    "SELECT count(*) FROM conversation_messages WHERE context_id = '$cid' AND owner_email = '$owner'" 2>&1)" \
    || fail "postgres" "psql failed: $rows"
[ "${rows//[[:space:]]/}" -ge 2 ] 2>/dev/null || fail "postgres" "expected >= 2 saved messages for $cid, got: $rows"

for key in user agent registration_id context_id answer; do echo "  $key: $(value "$key")"; done
echo "  directory: $ads_cid is routed"
echo "  postgres: ${rows//[[:space:]]/} messages saved for $user"
echo "smoke: OK"
