# Disaster Response Agent

`disaster_response_agent.py` is an AI-powered A2A agent that turns emergency situation descriptions into practical response guidance. It can generate immediate actions, shelter and evacuation guidance, communications steps, supplies, medical/accessibility notes, avoided actions, and follow-up checks.

The Python environment is the same environment used by the email agent. If the email agent environment is already active, use it for this agent too.

## Requirements

- Same conda/Python environment as `email_agent.py`
- `OPENAI_API_KEY` for AI response generation
- `a2a-sdk`, `starlette`, and `uvicorn` for A2A server mode
- `dirctl` on `PATH` for real ADS publication/discovery

## Files

- `disaster_response_agent.py` - standalone AI agent, CLI, A2A server, ADS publish/discovery helpers
- `disaster_response_agent.card.json` - A2A Agent Card for discovery and integration

## CLI Usage

Generate a response plan:

```bash
export OPENAI_API_KEY="..."
python disaster_response_agent.py respond "A tornado warning was issued and people are inside a classroom"
```

Add context:

```bash
python disaster_response_agent.py respond \
  "Flood water is entering the first floor" \
  --location "campus housing" \
  --audience "students and staff" \
  --severity high
```

Print structured JSON:

```bash
python disaster_response_agent.py respond "armed people are near the building" --json
```

## A2A Server

Start the agent as an A2A server:

```bash
export OPENAI_API_KEY="..."
python disaster_response_agent.py serve-a2a --host 0.0.0.0 --port 9104
```

Start without publishing to ADS:

```bash
python disaster_response_agent.py serve-a2a --host 0.0.0.0 --port 9104 --no-publish-ads
```

The default advertised endpoint in the card is:

```text
http://10.229.117.223:9104/
```

Use `--advertise-url` if the reachable URL is different.

## ADS

By default, ADS uses:

```text
10.229.117.37:8888
```

The agent publishes its card to ADS on `serve-a2a` unless `--no-publish-ads` is set.

Discover agents from ADS:

```bash
python disaster_response_agent.py ads-discover --query email --limit 5
```

Send an A2A request to an ADS-discovered agent:

```bash
python disaster_response_agent.py a2a-send <agent_ref> \
  --skill email/group_send \
  --message "Draft a tornado warning notice for the notice list." \
  --data-json '{"recipient_source":"notice_list","subject":"Tornado Warning","body":"Seek shelter immediately."}'
```

## Skills

The Agent Card exposes:

- `disaster/response_plan`
- `disaster/triage`
- `disaster/checklist`

The AI planner can also use ADS tools internally to discover and call other A2A agents when useful.

## Safety Scope

This agent provides emergency planning guidance, not official emergency instructions. In immediate danger, people should contact local emergency services when safe and follow official alerts or responder directions.

For armed threats or violence, the agent focuses on life safety: escape if safe, hide/shelter if escape is unsafe, contact emergency services when safe, and follow responder commands. It should not provide offensive tactics or weapon instructions.
