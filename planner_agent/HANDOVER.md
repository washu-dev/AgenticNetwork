# Handover — Planner Agent

## First-time setup

Copy the env template and put your OpenAI key in it:

```bash
cd /path/to/AgenticNetwork
cp planner_agent/.env.example planner_agent/.env.local
$EDITOR planner_agent/.env.local   # set OPENAI_API_KEY=sk-...
```

`.env.local` is gitignored — keep your key there, never in committed files.

## Start

```bash
cd /path/to/AgenticNetwork
planner_agent/scripts/start.sh
```

Open the chat UI in your browser at **http://localhost:8090/** (VS Code Remote auto-forwards port 8090; if it doesn't, add it manually in the **PORTS** panel).
