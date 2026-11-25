#!/usr/bin/env bash
#
# One-time GCP setup for building custom agents with Cloud Build (AGENT_BUILDER=cloudbuild)
# and keeping uploaded agent code in Cloud Storage (AGENT_CODE_BUCKET). See
# registration_service/gcp_build.py and docs/deployment/gcp/DEPLOY_YOUR_OWN.md step 4.
#
# Usage: GCP_PROJECT=my-project bash deployment/scripts/gcp_cloud_build_setup.sh
#
# Creates or ensures:
#   * bucket  <project>-agent-code  — uploads/<agent id>.tar.gz and build-contexts/...;
#                                     versioned, noncurrent versions deleted after 90 days
#   * agent-builder@                — the identity Cloud Build runs agent builds as: pushes to
#                                     the Artifact Registry repo, writes build logs, reads
#                                     build contexts from the bucket
#   * registration-service@ grants  — create builds, run them as agent-builder, read and
#                                     write the bucket
#   * build-notifier@ + Pub/Sub     — topic cloud-builds (Cloud Build publishes every build's
#                                     state there) and a push subscription that calls the
#                                     registration service's /internal/build-events as
#                                     build-notifier, which has run.invoker on it
#
# Run gcp_iam_step7.sh first (it creates registration-service@) and deploy the
# registration service first (the subscription needs its URL).
#
# Idempotent. Three bindings are project-level or repository-level and need
# roles/owner or roles/resourcemanager.projectIamAdmin (Editor is not enough):
# cloudbuild.builds.editor and logging.logWriter on the project, and
# artifactregistry.writer on the repository. Everything else needs Editor plus
# roles/iam.serviceAccountAdmin and roles/run.admin.
set -euo pipefail

PROJECT="${GCP_PROJECT:?set GCP_PROJECT to your Google Cloud project id}"
REGION="${GCP_REGION:-us-central1}"
REPO="${AR_REPO:-agenticnetwork}"
BUCKET="${AGENT_CODE_BUCKET:-${PROJECT}-agent-code}"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
REG_SA="registration-service@${PROJECT}.iam.gserviceaccount.com"
BUILD_SA="agent-builder@${PROJECT}.iam.gserviceaccount.com"
NOTIFIER_SA="build-notifier@${PROJECT}.iam.gserviceaccount.com"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud services enable cloudbuild.googleapis.com pubsub.googleapis.com storage.googleapis.com \
  artifactregistry.googleapis.com --project="$PROJECT"

create_sa () {  # $1 = account id, $2 = display name
  gcloud iam service-accounts describe "$1@${PROJECT}.iam.gserviceaccount.com" --project="$PROJECT" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$1" --project="$PROJECT" --display-name="$2"
}

# ── Bucket ──────────────────────────────────────────────────────────────────────
if ! gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --project="$PROJECT" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
gcloud storage buckets update "gs://${BUCKET}" --versioning
LIFECYCLE="$(mktemp)"
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"isLive":false,"daysSinceNoncurrentTime":90}}]}' > "$LIFECYCLE"
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file="$LIFECYCLE"
rm -f "$LIFECYCLE"

# ── Build identity ──────────────────────────────────────────────────────────────
create_sa agent-builder "Builds custom agent images (Cloud Build)"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/storage.objectViewer --format=none
# Owner / projectIamAdmin needed for the next two:
gcloud artifacts repositories add-iam-policy-binding "$REPO" --location="$REGION" --project="$PROJECT" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/artifactregistry.writer --format=none
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/logging.logWriter --condition=None --format=none

# ── Registration service: start builds as agent-builder, use the bucket ─────────
# Owner / projectIamAdmin needed for the project binding:
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${REG_SA}" --role=roles/cloudbuild.builds.editor --condition=None --format=none
gcloud iam service-accounts add-iam-policy-binding "$BUILD_SA" --project="$PROJECT" \
  --member="serviceAccount:${REG_SA}" --role=roles/iam.serviceAccountUser --format=none
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${REG_SA}" --role=roles/storage.objectAdmin --format=none

# ── Build-finished notices → registration ───────────────────────────────────────
create_sa build-notifier "Pub/Sub push: Cloud Build notices to registration"
gcloud run services add-iam-policy-binding registration --region="$REGION" --project="$PROJECT" \
  --member="serviceAccount:${NOTIFIER_SA}" --role=roles/run.invoker --format=none
gcloud iam service-accounts add-iam-policy-binding "$NOTIFIER_SA" --project="$PROJECT" \
  --member="serviceAccount:${PUBSUB_AGENT}" --role=roles/iam.serviceAccountTokenCreator --format=none
gcloud pubsub topics describe cloud-builds --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub topics create cloud-builds --project="$PROJECT"
REG_URL="$(gcloud run services describe registration --region="$REGION" --project="$PROJECT" --format='value(status.url)')"
gcloud pubsub subscriptions describe registration-build-events --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub subscriptions create registration-build-events --project="$PROJECT" --topic=cloud-builds \
       --push-endpoint="${REG_URL}/internal/build-events" --push-auth-service-account="$NOTIFIER_SA" \
       --push-auth-token-audience="$REG_URL" --ack-deadline=60 --message-retention-duration=1h \
       --expiration-period=never

cat <<EOF
Done. Set on the registration service:
  AGENT_BUILDER=cloudbuild
  AGENT_CODE_BUCKET=${BUCKET}
  CLOUD_BUILD_SERVICE_ACCOUNT=${BUILD_SA}
  IMAGE_REGISTRY=${REGION}-docker.pkg.dev/${PROJECT}/${REPO}
EOF
