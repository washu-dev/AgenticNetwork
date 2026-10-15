# Docker Compose: one host

Run the whole system in containers on one machine, a laptop or any cloud VM, with
Docker Compose: Postgres, the real directory daemon (`dirctl`), the registration
service, the planner and a front door. It uses the same images as the ENGR cluster
deployment. It is also the **tier 2** test from
[`OPEN_SOURCE_RELEASE.md`](../OPEN_SOURCE_RELEASE.md): the native tier plus real Postgres
and real `dirctl`.

Files: [`deployment/compose/compose.yaml`](../../deployment/compose/compose.yaml),
[`.env.example`](../../deployment/compose/.env.example) and
[`smoke.sh`](../../deployment/compose/smoke.sh). For hacking on the code, the native dev
stack ([`DEVSTACK.md`](../DEVSTACK.md)) is faster: no images to rebuild.

## Quick start

You need Docker with the Compose v2 plugin (tested with Docker 28.5 and Compose 2.40) and
`python3` for the smoke test. From the repo root:

```bash
cd deployment/compose
cp .env.example .env              # optional: every setting has a default
docker compose up -d --build      # build the images, start everything
docker compose run --rm seed      # exits 0 once the planner lists the seed agent
./smoke.sh                        # one prompt end to end; prints "smoke: OK"
```

Open **http://127.0.0.1:18080/app**: the chat UI, signed in as `dev@example.test`. Try
"Echo this back: hello". With the default mock LLM the answer starts with
`[mock-llm] Echo:`. `docker compose down` stops everything and keeps the data;
`docker compose down -v` also deletes it.

The first build took about 2.5 minutes on a 10-core arm64 Mac with an empty cache (planner
77 s, registration 52 s, Postgres 18 s, ADS 10 s, base images included). Docker Desktop
reported the images at 3.8 GB (planner), 640 MB (registration), 600 MB (Postgres) and
320 MB (ADS). A start with built images takes about 20 seconds.

## What runs

| Service | Image | What it does |
|---|---|---|
| `net` | `python:3.11-slim-bookworm` | Owns the shared network namespace and publishes the front door. Runs only `sleep`. |
| `postgres` | `Dockerfile.postgres` | Postgres 15. Database `registration` (registered agents, users, conversation history, credentials) persists in the `pgdata` volume; database `dir` (the directory's metadata) is recreated on every start. |
| `ads` | `Dockerfile.ads` | The AGNTCY directory daemon (`dirctl daemon`, `deployment/ads_daemon.yaml`) on `localhost:8888`. |
| `registration` | `Dockerfile.registration` | The registration service on `localhost:8091`. Spawns each registered agent as a subprocess in this container (ports 19200-19299) and publishes its card to the directory. Uploaded code and agent logs live in the `registration-data` volume. |
| `planner` | `Dockerfile.planner` | The planner and chat UI on `localhost:8090`, with history in Postgres. |
| `mock-llm` | `python:3.11-slim-bookworm` | The offline mock LLM (`devstack/mock_llm.py`) on `localhost:8099`. Used unless you configure a real model. |
| `proxy` | `python:3.11-slim-bookworm` | The front door for local use (`devstack/dev_identity_proxy.py`), port 8080 in the namespace. It drops identity headers sent by the client and signs every request in as `DEV_USER_EMAIL`. |
| `seed` | `python:3.11-slim-bookworm` | One-shot: registers `devstack/seeds/*.json` through the registration API if they are not registered yet, then exits 0 once the planner lists them. |

`mock-llm`, `proxy` and `seed` run the checkout's `devstack/` (standard library only),
mounted read-only. Logs: `docker compose logs [-f] SERVICE`.

### How it maps to the ENGR deployment

[`deployment/submit.sbatch`](../../deployment/submit.sbatch) runs the same four images as
parallel containers on one cluster node with host networking, so the services find each
other on `localhost` and the entrypoints wait for Postgres and the directory on
`localhost` ports. Compose copies that layout: every service joins the network namespace
of `net` (`network_mode: service:net`), so `localhost` means the same thing in every
container and the ENGR defaults work unchanged. The registration service advertises
spawned agents as `http://localhost:<port>/`, which the planner can only reach from the
same namespace. Separate service hostnames would need new settings in the registration
service, the directory config and the entrypoints; the shared namespace needs none.

| | ENGR cluster | Compose |
|---|---|---|
| Launcher | Slurm job, one `srun --overlap` step per image | `docker compose up` |
| Networking | Host networking on the node | One shared namespace, owned by `net` |
| Postgres data | `/engrfs` (prod) or node scratch (test) | `pgdata` volume |
| Spawned agents | `SPAWN_BACKEND=cluster`: images built through GitHub and Docker Hub, run as `srun` steps by `deployment/spawn_agent_loop.sh` | Default subprocess backend: subprocesses in the `registration` container |
| Specialist agents | Six images from the `agent-directory` repo | None; only agents you register |
| LLM | vLLM on a GPU node (`start_vllm.sbatch`) | Mock LLM, or any OpenAI-compatible endpoint |
| Front door | nginx + Apache + Shibboleth on a separate host | `devstack/dev_identity_proxy.py`; your authenticating proxy in production |

## Configuration

Compose reads `deployment/compose/.env`. Every variable is optional.

| Variable | Default | Meaning |
|---|---|---|
| `FRONT_DOOR_BIND` | `127.0.0.1` | Host address the front door is published on. |
| `FRONT_DOOR_PORT` | `18080` | Host port of the front door (18080 so it does not clash with the dev stack's 8080). |
| `DEV_USER_EMAIL` | `dev@example.test` | The identity the dev proxy signs every request in as. |
| `ADMIN_EMAILS` | `dev@example.test` | Comma-separated admin emails, read by the planner and the registration service. |
| `OPENAI_BASE_URL` | `http://127.0.0.1:8099/v1` (the mock) | OpenAI-compatible Chat Completions endpoint for the planner and the spawned agents. |
| `OPENAI_API_KEY` | `mock-key` | Its API key. |
| `MAIN_AGENT_LLM_MODEL` | `mock-planner` | The planner's model name. |
| `DEVSTACK_SPECIALIST_MODEL` | `MAIN_AGENT_LLM_MODEL` | The model the seed agents are registered with. |
| `CREDENTIALS_ENCRYPTION_KEY` | empty | 32-byte base64 key that encrypts users' stored credentials. Needed before anyone saves a credential. |
| `COMPOSE_PROJECT_NAME` | `agenticnetwork` | Compose's own setting. A second stack on the same host needs another name and another `FRONT_DOOR_PORT`; it gets its own containers and volumes. |

Other settings the services read (for example `GITHUB_ISSUES_REPO` and
`GITHUB_ISSUES_TOKEN` for bug reports, see `deployment/.env.example`) go under the
service's `environment:` in a `compose.override.yaml` next to `compose.yaml`, which
Compose merges automatically. Keep secret values in `.env` and reference them there,
for example `GITHUB_ISSUES_TOKEN: ${GITHUB_ISSUES_TOKEN}`; git ignores both `.env` and
`compose.override.yaml`.

## Switching the LLM

Set all three in `.env`, before the first start:

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
MAIN_AGENT_LLM_MODEL=gpt-4o-mini
```

Any OpenAI-compatible Chat Completions endpoint works: a hosted API, vLLM or Ollama. The
model must support tool calling (for vLLM, see the launch flags in
[`CLAUDE.md`](../../CLAUDE.md) §7). The endpoint must be reachable from the containers;
`127.0.0.1` inside the stack is the stack itself, not your machine.

The seed agent stores its model name when it is registered. If the stack already ran
with another model, start from empty volumes (`docker compose down -v`, then `up`), or
the seed agent keeps the old model. With a real model, `smoke.sh` checks discovery,
delegation and history but not the mock's exact answer.

## Production checklist

The defaults are for one person on their own machine. Before other people use it:

1. **Put an authenticating proxy in front.** The dev proxy signs *everyone* in as
   `DEV_USER_EMAIL`, an admin by default. Never publish it beyond `127.0.0.1` and never
   set `FRONT_DOOR_BIND=0.0.0.0` while it is the front door. Replace the `proxy` service
   (edit `compose.yaml`, or override its `image` and `command` in `compose.override.yaml`)
   with a proxy that:
   - authenticates every request (an SSO service provider, an OAuth2/OIDC proxy, or an
     identity-aware load balancer in front of a header-mapping proxy);
   - removes any `mail`, `eppn` and `remote-user` headers the client sent, then sets
     `mail` to the verified email. The planner trusts these headers, in that order of
     priority ([`AUTH.md`](../AUTH.md) §1);
   - forwards to `http://127.0.0.1:8090` from inside the namespace
     (`network_mode: service:net`), listens on port 8080 there, and streams responses
     unbuffered (the chat UI uses server-sent events on `/messages/stream`).
2. **Keep the planner and registration ports private.** They trust identity headers
   and have unauthenticated internal routes (`/internal/*`, [`SECURITY.md`](../../SECURITY.md)).
   Both bind `127.0.0.1` inside the namespace and are not published; do not change that.
3. **TLS.** Terminate HTTPS at the proxy or a load balancer in front of it.
4. **Secrets.** Set `CREDENTIALS_ENCRYPTION_KEY`
   (`python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"`)
   and keep it stable: if it changes or is lost, stored credentials cannot be decrypted.
   Store a copy apart from the database backups. `chmod 600 .env`; git ignores `.env`.
   Set `ADMIN_EMAILS` to the real admins.
5. **Back up** the `registration` database and the uploaded agents:
   ```bash
   mkdir -p ~/agenticnetwork-backup   # outside the checkout: the dump holds conversations
   docker compose exec -T postgres pg_dump -h /tmp/postgresql -U postgres registration > ~/agenticnetwork-backup/registration.sql
   docker compose cp registration:/var/lib/agenticnetwork ~/agenticnetwork-backup/registration-data
   ```
   To restore, replace the database while the services that use it are stopped:
   ```bash
   docker compose stop seed planner registration
   docker compose exec -T postgres dropdb -h /tmp/postgresql -U postgres registration
   docker compose exec -T postgres createdb -h /tmp/postgresql -U postgres registration
   docker compose exec -T postgres psql -h /tmp/postgresql -U postgres -d registration -q -v ON_ERROR_STOP=1 < ~/agenticnetwork-backup/registration.sql
   docker compose cp ~/agenticnetwork-backup/registration-data/. registration:/var/lib/agenticnetwork
   docker compose start registration planner
   ```
   The `dir` database needs no backup: it is rebuilt on every start.
6. **Review uploaded code before approving it.** Approved custom agents run inside the
   `registration` container and share its network namespace, so they can reach every
   service on `localhost`, Postgres included (trust authentication, as on ENGR).
7. **Trust every account like an admin.** Only uploaded code waits for approval. Any
   signed-in user can register an `llm` agent with `http` tools, or a `proxy` agent, and
   it starts at once inside the `registration` container with that container's
   environment: it can send the LLM key (or any variable named in `auth_env`) to a URL of
   its choosing and call the planner and registration on `localhost` with forged identity
   headers ([`SECURITY.md`](../../SECURITY.md) §2). Give accounts only to people you trust.

## Deploying on a cloud VM

Any Linux VM with Docker works the same way:

1. Create a VM with 2 or more vCPUs, 8 GB of memory and a 30 GB disk. (The running
   stack used about 300 MB of memory when idle in testing; the margin is for builds and
   agents.)
2. Install Docker Engine with the Compose plugin and git.
3. Clone the repository, `cd deployment/compose`, create `.env` (LLM, admins, encryption
   key) and `chmod 600 .env`.
4. `docker compose up -d --build`, `docker compose run --rm seed`, `./smoke.sh`.
5. Reach the front door through an SSH tunnel (operators), or put the authenticating
   proxy and TLS in front (users). Open only SSH and 443 in the firewall.

Example on Google Compute Engine (Debian 12, `us-central1-a`):

```bash
gcloud compute instances create agenticnetwork --zone=us-central1-a \
    --machine-type=e2-standard-2 --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=30GB
gcloud compute ssh agenticnetwork --zone=us-central1-a

# on the VM
curl -fsSL https://get.docker.com | sudo sh        # Docker Engine + Compose plugin
sudo usermod -aG docker "$USER" && newgrp docker   # use docker without sudo
sudo apt-get install -y git python3
git clone https://github.com/washu-dev/AgenticNetwork.git
cd AgenticNetwork/deployment/compose
cp .env.example .env && chmod 600 .env             # then edit it
docker compose up -d --build && docker compose run --rm seed && ./smoke.sh

# from your laptop: tunnel to the front door, then open http://127.0.0.1:18080/app
gcloud compute ssh agenticnetwork --zone=us-central1-a -- -L 18080:127.0.0.1:18080
```

The services restart with Docker (`restart: unless-stopped`). After a VM reboot, check
`docker compose ps`; if anything is unhealthy, run `docker compose down` and
`docker compose up -d`.

This is separate from the Cloud Run deployment in [`gcp/README.md`](gcp/README.md), which
runs each service as a managed Cloud Run service with Cloud SQL.

## Limits

- **One host, one copy of each service.** No high availability or scaling out.
- **Agents run as subprocesses of the registration service,** in its container: they
  share its CPU, memory and network namespace, at most 100 at a time (ports 19200-19299).
  The registration service stops them when it stops and starts them again, and
  republishes their cards, when it starts.
- **Custom-agent uploads** need an admin's approval (`ADMIN_EMAILS`). An agent with
  `requirements` gets its own virtualenv, created in the container on first spawn, which
  needs internet access to PyPI. `system_packages` (apt) are skipped with a warning; they
  apply only to cluster image builds. Uploaded files and virtualenvs persist in the
  `registration-data` volume.
- **No specialist agents** from the `agent-directory` repo (email, scout and so on) and
  no cluster build pipeline (`SPAWN_BACKEND=cluster`) or Cloud Run backend.
- **The directory starts empty on every start,** as on ENGR: the `dir` database is
  recreated and the daemon's store lives in its container. The registration service
  republishes its live agents. Restart the whole stack (`down`, then `up -d`), not `ads`
  alone.
- **Postgres stops uncleanly.** `entrypoint-postgres.sh` keeps the container alive with
  `tail`, so stopping the container kills Postgres, which runs crash recovery on the next
  start (seen in testing; committed data survives). Keep backups.
- **Publishing may need internet:** `deployment/ads_daemon.yaml` configures the OASF
  validator with `schema_url: https://schema.oasf.outshift.com`. A fully offline start
  was not tested.
- **Images are built for the host's architecture.** Tested on arm64 (Apple silicon)
  only; `deployment/build_and_push.sh` builds `linux/amd64` for ENGR.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `docker compose up` says a dependency failed to start | `docker compose ps` shows which service is unhealthy; `docker compose logs SERVICE` shows why. |
| The planner is unhealthy | `docker compose exec planner curl -s http://127.0.0.1:8090/ready` names the failing check: `directory`, `db` or `llm`. |
| `docker compose run --rm seed` exits non-zero | `docker compose logs seed registration`. With a real LLM, the planner must be ready, so check its `llm` check first. |
| An agent misbehaves | Its log: `docker compose exec registration ls /var/lib/agenticnetwork/logs/agents` (one file per agent id). |
| The chat finds no agents on a fresh stack | `up` does not seed (the `seed` service has its own profile): run `docker compose run --rm seed`. |
| Port 18080 is in use | Set `FRONT_DOOR_PORT` in `.env`. |
| Many `Failed to parse enhanced label key` warnings from `ads` | Harmless; they appear in passing runs too. |
| Start over from nothing | `docker compose down -v` deletes the database and uploads. |

## The tier-2 smoke test

`smoke.sh` drives the stack through the front door the way
[`tests/pipeline/test_full_chain.py`](../../tests/pipeline/test_full_chain.py) drives the
dev stack, and exits 1 with `smoke: FAILED at <step>: ...` on the first failure:

1. **ready:** `GET /ready` through the front door reports the real directory and a
   working database.
2. **seed agent listed:** the planner's `GET /agents` lists *Echo Assistant*.
3. **sign-in:** `GET /me` returns the signed-in user.
4. **chat, discovery, delegation, answer:** one prompt posted the chat UI's way
   (`POST /messages/stream`) runs `list_agents`, then `call_agent` on the seed agent, then
   `final_answer`; with the mock LLM the answer is the agent's `[mock-llm] Echo:` reply.
5. **history:** `GET /conversations/<id>` holds the prompt and the answer, and
   `GET /sessions` lists the conversation.
6. **persistence** (with `--expect-conversation ID`): an earlier conversation is still
   saved.
7. **directory:** the seed agent's record (its `ads_cid` from the registration service)
   is in `dirctl routing list` on the daemon.
8. **postgres:** the turn's messages are rows in `conversation_messages` for that user.

To check that state survives a restart:

```bash
./smoke.sh                                   # note the context_id it prints
docker compose down && docker compose up -d && docker compose run --rm seed
./smoke.sh --expect-conversation <context_id>
```

After the restart the seed agent keeps its `registration_id` (the seed service finds it
instead of registering it again) and is listed by the planner again, because the
registration service respawns and republishes it. It runs from any directory and honours
`COMPOSE_PROJECT_NAME`; set `FRONT_DOOR_URL` to test a front door other than the one
`docker compose port net 8080` reports.
