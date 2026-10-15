#!/usr/bin/env bash
#
# Step 7 (CloudRunBackend) — one-time GCP IAM setup for the register→deploy pipeline.
# See docs/deployment/gcp/IMPLEMENTATION_CLOUDRUN_BACKEND.md "Step 7 — IAM" and
# docs/deployment/gcp/DEPLOY_YOUR_OWN.md. Usage: GCP_PROJECT=my-project bash deployment/scripts/gcp_iam_step7.sh
#
# Creates the two service accounts and grants the registration service the rights
# to deploy on-demand agents:
#   * agent-runtime          — the low-privilege identity each agent RUNS as (not Editor,
#                              not the default compute SA). Gets Vertex (fallback LLM).
#   * registration-service   — the identity the reg service runs as (the deployer). Gets
#                              run.admin (create/delete services + set per-service invoker
#                              IAM) and serviceAccountUser on agent-runtime (to deploy
#                              agents AS that SA).
#
# The planner SA is granted run.invoker per-service by CloudRunBackend at spawn time
# (which is why the reg-service SA needs run.admin), so no standing grant here.
#
# Idempotent: re-running is safe (existing SAs are skipped; re-adding an existing
# binding is a no-op). REQUIRES the runner to hold roles/owner or
# roles/resourcemanager.projectIamAdmin — project-level bindings need
# resourcemanager.projects.setIamPolicy (Editor/resource-admin roles do NOT include it).
set -euo pipefail

PROJECT="${GCP_PROJECT:?set GCP_PROJECT to your Google Cloud project id}"
REGION="${GCP_REGION:-us-central1}"
AGENT_SA="agent-runtime@${PROJECT}.iam.gserviceaccount.com"
REG_SA="registration-service@${PROJECT}.iam.gserviceaccount.com"

create_sa () {  # $1 = account id, $2 = display name
  local email="$1@${PROJECT}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "$email" --project="$PROJECT" >/dev/null 2>&1; then
    echo "SA $1 already exists — skipping create"
  else
    gcloud iam service-accounts create "$1" --project="$PROJECT" --display-name="$2"
  fi
}

# 1. Service accounts.
create_sa agent-runtime        "AgenticNetwork on-demand agent runtime (low-privilege)"
create_sa registration-service "AgenticNetwork registration service (Cloud Run deployer)"

# 2. reg-service SA: deploy + set per-service invoker IAM (project-level).
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${REG_SA}" --role="roles/run.admin" --condition=None

# 3. reg-service SA: deploy agents AS agent-runtime (scoped to that SA, least-privilege).
gcloud iam service-accounts add-iam-policy-binding "$AGENT_SA" --project="$PROJECT" \
  --member="serviceAccount:${REG_SA}" --role="roles/iam.serviceAccountUser"

# 4. agent-runtime SA: Vertex (the fallback LLM path agents use).
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${AGENT_SA}" --role="roles/aiplatform.user" --condition=None

# Deferred (see the doc):
#   * roles/vpcaccess.user on reg-service SA — only if on-demand agents attach the VPC
#     connector for VPC-internal egress; agents calling Vertex (a Google API) don't.
#   * roles/secretmanager.secretAccessor — granted per-secret in Step 9, when the reg
#     service's secrets (GITHUB_TOKEN, DB creds) actually exist.

echo
echo "Step 7 IAM applied. Set PLANNER_INVOKER_SA to the planner's runtime SA:"
gcloud run services describe planner --region="$REGION" --project="$PROJECT" \
  --format="value(spec.template.spec.serviceAccountName)"
