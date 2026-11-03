# OrderBridge Production Operations Runbook

This runbook documents deterministic operational procedures for on-call engineers managing **OrderBridge** on Google Cloud Run (`asia-south1`) and Cloud SQL for PostgreSQL 16.

---

## 1. Service Health & Readiness Checks

| Probe Endpoint | Purpose | Expected HTTP Status | Failure Action |
|---|---|---|---|
| `GET /health` | Liveness probe (process & event loop responsive) | `200 OK` (`{"status": "ok"}`) | Cloud Run restarts container after 3 consecutive failures. |
| `GET /ready` | Readiness probe (`SELECT 1` against PostgreSQL 16) | `200 OK` (`{"status": "ready", "database": "connected"}`) | Instance removed from Cloud Run load balancer pool (`503`). |
| `GET /webhooks/whatsapp` | Meta Cloud API webhook verification challenge | `200 OK` echoing `hub.challenge` when token matches; `403` on mismatch | Verify `META_VERIFY_TOKEN` secret in GCP Secret Manager. |

---

## 2. Emergency Cloud Run Revision Rollback (< 60 Seconds)

```bash
gcloud run revisions list --service=orderbridge-api --region=asia-south1 --project="${GCP_PROJECT_ID}"
gcloud run services update-traffic orderbridge-api --to-revisions="orderbridge-api-00041-abc=100" --region=asia-south1 --project="${GCP_PROJECT_ID}"
```

---

## 3. Database Migration Rollback & Point-in-Time Recovery (PITR)

```bash
# Reversible Alembic downgrade
PYTHONPATH=backend alembic -c backend/alembic.ini downgrade -1

# Cloud SQL Point-in-Time Restore
gcloud sql instances clone orderbridge-pg16 orderbridge-pg16-recovery --point-in-time="2026-11-02T14:30:00.000Z" --project="${GCP_PROJECT_ID}"
```

---

## 4. Zero-Downtime Secret & Meta Token Rotation

```bash
printf "%s" "${NEW_SECRET_VALUE}" | gcloud secrets versions add orderbridge-meta-app-secret --data-file=- --project="${GCP_PROJECT_ID}"
gcloud run services update orderbridge-api --region=asia-south1 --project="${GCP_PROJECT_ID}"
```

---

## 5. Gemini LLM Outage or Tenant Budget Cap Exhaustion

Stages 1–8 of the extraction pipeline are **100% deterministic**. If the Gemini API (`gemini-3.8-flash` / `gemini-3.5-transcribe`) times out or a tenant reaches `monthly_llm_cap_paise`, `GeminiExtractionProvider` returns `LLMDisambiguationResult(sku_id="UNKNOWN", confidence=0.0)` and routes uncertain lines to `NEEDS_REVIEW` with zero dropped orders and zero hallucinated SKUs.
