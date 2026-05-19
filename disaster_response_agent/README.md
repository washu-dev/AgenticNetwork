# Disaster Response Agent

This directory contains the standalone Disaster Response Agent service. It runs as an external A2A agent that turns emergency situation descriptions into practical response guidance: immediate actions, shelter and evacuation guidance, communications steps, supplies, medical/accessibility notes, avoided actions, and follow-up checks.

## What It Does

- Builds disaster response plans from natural-language incident descriptions.
- Triage likely incident type, risk level, immediate hazards, and first actions.
- Creates action checklists for shelter, evacuation, supplies, communications, and follow-up.
- Exposes those capabilities through an A2A HTTP endpoint.
- Can use ADS discovery and outbound A2A calls only when started with `--enable-remote-agent-tools`.

## Setup

Use the shared conda environment from the email agent:

```bash
cd <project_root>/email_agent
conda env create -f environment.yml
conda activate email-agent-a2a
cd ../disaster_response_agent
```

The shared `email-agent-a2a` environment includes the Email, Disaster Response, and GeoSpatial agent dependencies.

Set the OpenAI API key:

```bash
export OPENAI_API_KEY="your-openai-api-key"
```

## Run

Start the Disaster Response Agent service:

```bash
python3 disaster_response_agent.py serve-a2a --enable-remote-agent-tools
```

Set the hosted port:

```bash
python3 disaster_response_agent.py serve-a2a --enable-remote-agent-tools --host 0.0.0.0 --port 9104
```

Set the ADS server address:

```bash
python3 disaster_response_agent.py serve-a2a --enable-remote-agent-tools --ads-url 10.229.117.37:8888
```

Start without publishing to ADS, useful when ADS is offline:

```bash
python3 disaster_response_agent.py serve-a2a --enable-remote-agent-tools --no-publish-ads
```

The Agent Card should point to the reachable host and port, for example:

```text
http://10.229.117.223:9104/
```

Use `--advertise-url` when the externally reachable URL differs from the card.

## Model

The default model is `gpt-5`. Change it with `--model`:

```bash
python3 disaster_response_agent.py serve-a2a --enable-remote-agent-tools --model gpt-5.2
```

The local CLI uses the same model flag:

```bash
python3 disaster_response_agent.py respond --model gpt-5.2 "A tornado warning was issued and people are inside a classroom"
```

## ADS And Remote Agents

`serve-a2a` publishes the agent card to ADS by default. Use `--ads-url` to choose the ADS server and `--no-publish-ads` to skip publication.

`--enable-remote-agent-tools` controls runtime access to ADS discovery and outbound A2A request tools. Without that flag, the AI planner does not get those tools.

## Skills

The Agent Card exposes:

- `disaster/response_plan`
- `disaster/triage`
- `disaster/checklist`

## Safety Scope

This agent provides emergency planning guidance, not official emergency instructions. In immediate danger, people should contact local emergency services when safe and follow official alerts or responder directions.

For armed threats or violence, the agent focuses on life safety: escape if safe, hide/shelter if escape is unsafe, contact emergency services when safe, and follow responder commands. It should not provide offensive tactics or weapon instructions.
