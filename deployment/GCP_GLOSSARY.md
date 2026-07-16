# GCP Glossary — Terms in the Migration Plan

A crash course on every service, term, and feature used in `GCP_MIGRATION_PROPOSAL.md`,
so it's easy to explain. Each entry: plain definition + how it's used in *our* system.

## The 30-second version (opener)

> "We're moving the infrastructure from the Slurm cluster to GCP. The always-on parts —
> planner, registry, database — become managed cloud services that autoscale. The LLM
> moves to Google's managed Gemini. And instead of running every agent 24/7, each agent
> becomes a service that starts on demand when it's called and shuts off when it's idle."

---

## 1. Core concepts (mental models)

- **Container / image** — a packaged app with everything it needs to run. An *image* is the
  frozen template; a *container* is a running copy. (We already use these — Docker.)
- **Registry** — storage for container images. You push images to it; the cloud pulls them to run.
- **Managed service** — Google runs the software for you (patching, scaling, backups); you
  just use it. Opposite of "we operate the server."
- **Serverless** — you give the cloud a container and it decides *when and how many* copies
  run. No VMs to manage. Pay only while it's doing work.
- **Stateless vs stateful** — *stateless* = keeps no important data in its own memory (safe to
  run many copies / restart anytime); *stateful* = holds data locally (a database, a
  long-lived daemon). Stateless autoscales easily; stateful needs a durable home.
- **Autoscaling** — automatically adding/removing copies as traffic rises and falls.
- **Scale-to-zero** — when nothing's using it, drop to *zero* running copies → $0 while idle.
- **Cold start** — the delay when a scaled-to-zero service gets its first request and has to
  boot up before answering.
- **Region / egress** — a *region* is a geographic location (e.g. `us-central1`). *Egress* is
  data **leaving** GCP or crossing regions — it's billed. Keeping everything in one region
  makes internal traffic free and is our main cost-control move.

## 2. Compute services

- **Cloud Run** — the star of the plan. "Hand it a container, it runs and autoscales it,
  scales to zero, gives it a stable HTTPS URL." Fully managed serverless. We use it for the
  planner, registration, ADS, **and** each on-demand agent.
  - **min-instances / max-instances** — floor and ceiling on running copies. `min=0` = scale
    to zero; `min=1` = keep one warm (no cold start); `min=max=1` = exactly one (pinned singleton).
  - **concurrency** — how many requests *one* copy handles at once (default 80). Set to 1 for
    one instance per request.
  - **CPU always allocated** — billing/runtime setting; needed if the app does work *between*
    requests (background loops), like our registration service.
- **GKE (Google Kubernetes Engine)** — managed **Kubernetes**: a real cluster with
  fine-grained control over sizing, GPUs, and lifecycle. Our **graduation path**, not the
  starting point — used only when an agent needs a GPU or strict isolation.
  - **Kubernetes** — the open-source system that schedules containers across machines and
    keeps them healthy. GKE is Google running it for you.
  - **Pod** — smallest run unit (one or more containers). **Node** — a machine Pods run on.
    **Autopilot** — a GKE mode where you don't manage nodes; you pay per Pod's requested CPU/mem.
- **Knative** *(nice-to-know)* — the open-source project Cloud Run is built on. Its
  "start-a-container-on-request, idle-it-down" model *is* the on-demand pattern we wanted —
  Cloud Run gives it to us for free.

## 3. Data & LLM

- **Cloud SQL** — managed relational database (we use PostgreSQL). Replaces our self-hosted
  Postgres. Managed but *always-on* (not scale-to-zero) — databases hold state.
- **Vertex AI** — Google's managed AI platform. We use it for **managed Gemini** inference so
  we don't run our own GPU for the LLM.
  - **Managed Gemini** — Google's LLM, hosted by Google, billed per use.
  - **OpenAI-compatible endpoint** — Vertex exposes an API that speaks the same "language" as
    OpenAI's, so our existing code works with just a URL change.
  - **Pay-per-token** — billed by amount of text in/out (tokens), not by the hour. No idle
    cost. **Flash vs Pro** are cheaper/pricier Gemini tiers.

## 4. Platform / plumbing

- **Artifact Registry** — GCP's container-image registry (replaces our personal Docker Hub).
  Same-region image pulls are free.
- **Secret Manager** — a secure vault for secrets (API keys, encryption keys, OAuth secrets).
  Replaces secrets in `.env` files or baked into images.
- **VPC (Virtual Private Cloud)** — your own private network inside GCP. Services talk
  privately, and we **restrict access to WashU's IP range**. This is the "hardest part."
- **Cloud Load Balancing / HTTPS Load Balancer** — the managed front door that receives
  internet traffic and routes it to a service (with TLS). The **forwarding rule** is its
  billable component (~$18/mo).
- **Cloud Logging / Monitoring** — managed log collection + dashboards/alerts (replaces
  reading Slurm `.log` files).

## 5. Identity & security

- **IAM (Identity and Access Management)** — the "who can do what" permission system across GCP.
- **Service account** — a non-human identity for an *app* (vs. a person). Our services use one
  to prove who they are.
- **Workload Identity** — lets a Cloud Run/GKE workload authenticate as a service account
  **without a key file**. How the planner calls Vertex — short-lived credentials automatically,
  no static API key.
- **Shibboleth / SSO** *(what we already have)* — WashU's single-sign-on. A proxy checks the
  user logged in with WashU credentials and passes their identity to the planner. We keep this,
  just repoint it at the new URL.
- **IAP (Identity-Aware Proxy)** *(future option)* — Google's managed "front door that forces
  login" — a GCP-native alternative to running our own Shibboleth proxy.
- **Workforce Identity Federation** *(future option)* — connects WashU's login into GCP so IAP
  can still use WashU credentials. Pairs with IAP if we retire the Shibboleth box.

## 6. Ops & testing

- **FinOps / budget alerts** — cost discipline: spending alerts, cap `max-instances`, watch
  egress.
- **Load testing** — hammering the system with fake traffic to find its limits. We'll use
  **k6** or **Locust** hitting the **API** (not the UI) from cloud VMs, simulating ~50
  concurrent users.

---

## How they fit together

**Cloud Run** runs our code; **Cloud SQL** stores our data; **Vertex AI** is our brain;
**Artifact Registry** stores the images Cloud Run runs; **Secret Manager** holds the passwords;
**VPC + Load Balancer + Shibboleth** control who can reach it; **IAM + Workload Identity**
control what our services are allowed to do; **Cloud Logging + budget alerts** tell us what's
happening and what it costs.
