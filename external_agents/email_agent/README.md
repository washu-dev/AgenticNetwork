# Email Agent

Running the Email Agent requires `credentials.json` and `token.json`, which are not included in this repo. Ask Lingchen for the credentials.

This directory contains the standalone Gmail-backed Email Agent service. It is intended to be operated as an external organization-owned A2A agent and discovered by the AgenticNetwork through its Agent Card.

## What It Does

- Reads and searches Gmail messages.
- Prepares and sends email through Gmail.
- Lists and creates Google Calendar events/invites.
- Lists, reads, creates, appends, and edits Google Docs.
- Lists, reads, creates, writes, and appends Google Sheets.
- Exposes those capabilities through an A2A HTTP endpoint.

## Setup

Create the conda environment:

```bash
conda env create -f environment.yml
conda activate email-agent-a2a
```

Place the private Google OAuth files next to `email_agent.py`:

```text
credentials.json
token.json
```

## Run

Start the external Email Agent service:

```bash
conda activate email-agent-a2a
python email_agent.py serve-a2a --host 0.0.0.0 --port 9103
```

The Agent Card should point to the reachable host and port, for example:

```text
http://10.229.117.223:9103/
```

## Test

From the AgenticNetwork repo, validate the running endpoint:

```bash
conda activate email-agent-a2a
python -m v2_agent_network.scripts.validate_agent_endpoint \
  --card v2_agent_network/configs/agent_cards/email_agent.card.json \
  --skill email/draft \
  --prompt "Draft an email to Bob with two meeting options." \
  --endpoint http://10.229.117.223:9103/
```

Write actions such as `email/send`, `email/calendar/create_event`, and Google Docs/Sheets mutations require confirmation metadata in the A2A request.
