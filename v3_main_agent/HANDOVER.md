# Handover — v3 Main Agent

## First-time setup

Copy the env template and put your OpenAI key in it:

```bash
cd /home/server2/Jian/MAS_demo/AgenticNetwork
cp v3_main_agent/.env.example v3_main_agent/.env.local
$EDITOR v3_main_agent/.env.local   # set OPENAI_API_KEY=sk-...
```

`.env.local` is gitignored — keep your key there, never in committed files.

## Start

```bash
cd /home/server2/Jian/MAS_demo/AgenticNetwork
v3_main_agent/scripts/start.sh
```

Open the chat UI in your browser at **http://localhost:8090/** (VS Code Remote auto-forwards port 8090; if it doesn't, add it manually in the **PORTS** panel).
