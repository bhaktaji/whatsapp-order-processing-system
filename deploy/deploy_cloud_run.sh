#!/usr/bin/env bash
# ==============================================================================
# OrderBridge — Google Cloud Run + Cloud SQL PostgreSQL 16 Deployment Script
# ==============================================================================
# Defaults to --dry-run mode so no cloud billing or production mutations occur
# without explicit operator confirmation (--execute).
# ==============================================================================

set -euo pipefail

MODE="dry-run"
if [[ "${1:-}" == "--execute" ]]; then
  MODE="execute"
elif [[ "${1:-}" == "--dry-run" || -z "${1:-}" ]]; then
  MODE="dry-run"
else
  echo "Usage: $0 [--dry-run | --execute]" >&2
  exit 1
fi

GCP_PROJECT_ID="${GCP_PROJECT_ID:-[ADD: GCP_PROJECT_ID]}"
GCP_REGION="${GCP_REGION:-asia-south1}"
SERVICE_NAME="${SERVICE_NAME:-orderbridge-api}"
AR_REPO="${AR_REPO:-orderbridge}"
CLOUDSQL_INSTANCE="${CLOUDSQL_INSTANCE:-orderbridge-pg16}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo 'latest')}"
IMAGE_URI="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${AR_REPO}/api:${IMAGE_TAG}"

run_cmd() {
  if [[ "${MODE}" == "dry-run" ]]; then
    echo "[DRY-RUN] $*"
  else
    echo "[EXECUTE] $*"
    eval "$@"
  fi
}

echo "=================================================================="
echo "OrderBridge Production Deployment (${MODE} mode)"
echo "Project  : ${GCP_PROJECT_ID}"
echo "Region   : ${GCP_REGION} (Mumbai)"
echo "Image    : ${IMAGE_URI}"
echo "=================================================================="

run_cmd "gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com --project='${GCP_PROJECT_ID}'"
run_cmd "gcloud artifacts repositories describe '${AR_REPO}' --location='${GCP_REGION}' --project='${GCP_PROJECT_ID}' >/dev/null 2>&1 || gcloud artifacts repositories create '${AR_REPO}' --repository-format=docker --location='${GCP_REGION}' --project='${GCP_PROJECT_ID}'"
run_cmd "docker build -f backend/Dockerfile -t '${IMAGE_URI}' ."
run_cmd "docker push '${IMAGE_URI}'"
run_cmd "gcloud run jobs execute orderbridge-db-migrate --region='${GCP_REGION}' --project='${GCP_PROJECT_ID}' --wait"
run_cmd "gcloud run deploy '${SERVICE_NAME}' --image='${IMAGE_URI}' --region='${GCP_REGION}' --project='${GCP_PROJECT_ID}' --platform=managed --no-allow-unauthenticated --min-instances=1 --max-instances=10 --concurrency=80 --cpu=1 --memory=512Mi --port=8000 --set-env-vars='APP_ENV=production,LOG_LEVEL=INFO' --set-secrets='DATABASE_URL=orderbridge-database-url:latest,JWT_SECRET_KEY=orderbridge-jwt-secret:latest,META_VERIFY_TOKEN=orderbridge-meta-verify-token:latest,META_APP_SECRET=orderbridge-meta-app-secret:latest,GEMINI_API_KEY=orderbridge-gemini-api-key:latest' --add-cloudsql-instances='${GCP_PROJECT_ID}:${GCP_REGION}:${CLOUDSQL_INSTANCE}'"

echo "Deployment workflow completed (${MODE})."
