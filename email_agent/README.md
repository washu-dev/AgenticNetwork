# Email Agent

This directory contains the standalone Gmail-backed Email Agent service. It runs as an external A2A agent for Gmail, Calendar, Google Docs, Google Sheets, and local visible-file workflows.

## What It Does

- Reads and searches Gmail messages.
- Prepares and sends email through Gmail.
- Lists and creates Google Calendar events/invites.
- Lists, reads, creates, appends, and edits Google Docs.
- Lists, reads, creates, writes, and appends Google Sheets.
- Lists, reads, writes, and attaches only local files placed under `visible_files/`.
- Exposes those capabilities through an A2A HTTP endpoint.
- Can use ADS discovery and outbound A2A calls only when started with `--enable-remote-agent-tools`.

## Setup

Create or update the shared conda environment:

```bash
cd <project_root>/email_agent
conda env create -f environment.yml
conda activate email-agent-a2a
```

This shared environment includes the Email, Disaster Response, and GeoSpatial agent dependencies, including the geospatial `attack_bundle` requirements.

Set the OpenAI API key for AI agent mode and autonomous A2A requests:

```bash
export OPENAI_API_KEY="your-openai-api-key"
```

Place the Google OAuth client file next to `email_agent.py`:

```text
credentials.json
```

`credentials.json` is not included in this repo. Ask Lingchen for the credentials or create an OAuth desktop/client credential with Gmail, Calendar, Docs, Sheets, and Drive metadata scopes.

## Google Auth

Authorize the Google account and create `token.json`:

```bash
python3 email_agent.py google-auth
```

Follow the browser OAuth flow, then return to the terminal. The agent writes `token.json` next to `email_agent.py`.

Check the current auth status:

```bash
python3 email_agent.py google-status
```

Reauthenticate from scratch:

```bash
mv token.json token.json.bak
python3 email_agent.py google-auth
```

Calendar-only auth is also available, but Gmail access uses `google-auth`:

```bash
python3 email_agent.py calendar-auth
```

`token.json` contains OAuth secrets. Do not commit or share it.

## Run

Start the Email Agent service:

```bash
python3 email_agent.py serve-a2a --enable-remote-agent-tools
```

Set the hosted port:

```bash
python3 email_agent.py serve-a2a --enable-remote-agent-tools --host 0.0.0.0 --port 9103
```

Set the ADS server address:

```bash
python3 email_agent.py serve-a2a --enable-remote-agent-tools --ads-url 10.229.117.37:8888
```

Start without publishing to ADS, useful when ADS is offline:

```bash
python3 email_agent.py serve-a2a --enable-remote-agent-tools --no-publish-ads
```

The Agent Card should point to the reachable host and port, for example:

```text
http://10.229.117.223:9103/
```

Use `--advertise-url` when the externally reachable URL differs from the card.

## Model

The default model is set in `email_agent.py`. Change the model for local AI agent mode with `--model`:

```bash
python3 email_agent.py agent --enable-remote-agent-tools --model gpt-5.2 "Summarize my latest unread email"
```

Autonomous A2A requests use the same tool-calling agent defaults in code.

## ADS And Remote Agents

`serve-a2a` publishes the agent card to ADS by default. Use `--ads-url` to choose the ADS server and `--no-publish-ads` to skip publication.

`--enable-remote-agent-tools` controls runtime access to ADS discovery and outbound A2A request tools. Without that flag, the AI agent does not get those tools.

## Test

From the AgenticNetwork repo, validate the running endpoint:

```bash
conda activate email-agent-a2a
python3 -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json \
  --skill email/draft \
  --prompt "Draft an email to Bob with two meeting options." \
  --endpoint http://10.229.117.223:9103/
```

Write actions such as `email/send`, `email/calendar/create_event`, and Google Docs/Sheets mutations require confirmation metadata in the A2A request.

## Skills

The Agent Card exposes Gmail, Calendar, Google Docs, Google Sheets, visible-file, and current-time skills, including:

- `email/search`
- `email/read`
- `email/draft`
- `email/send`
- `email/group_send`
- `email/calendar/list_events`
- `email/calendar/create_event`
- `email/docs/read`
- `email/sheets/read`
- `email/files/list`
- `email/files/read`
- `email/files/write`

## Visible Files Sandbox

Local file discovery is intentionally limited to:

```text
email_agent/visible_files/
```

The agent can list, read, write, and attach files only inside that folder. Relative attachment paths are resolved from `visible_files/`, and paths outside the sandbox are rejected.

For example, if `visible_files/id_rsa.pub` exists, the agent can discover it with `email/files/list`, read it with `email/files/read`, and attach it to `email/send` using `visible_file_paths: ["id_rsa.pub"]`.
