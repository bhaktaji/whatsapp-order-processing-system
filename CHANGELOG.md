# Changelog

All notable engineering changes to **OrderBridge** are documented in this file.

## [2.5.0] - 2026-11-02 (`release/prod-ready`)

### Security & Compliance
- Removed `push_to_github.bat` and `push_to_github.sh` and eliminated hardcoded fallback webhook token.
- Added fail-closed startup secret validation (`validate_production_readiness` in `backend/app/config.py`).
- Added cryptographic `X-Hub-Signature-256` HMAC verification (`hmac.compare_digest`), `wamid` idempotency (`uq_tenant_inbound_wamid`), and `256 KB` payload upper bound (`413`) on `POST /webhooks/whatsapp`.
- Replaced ban-evasion number switching in `backend/app/failover.py` with `BusinessContinuityController` enforcing Meta's 24-hour customer service window, TRAI TCCCPR 2018 DLT template verification, `STOP` opt-out handling, and `COMPLIANCE_HALT` on policy enforcement codes (`131031`, `368`).
- Implemented HMAC-SHA256 JWT authentication and RBAC (`OWNER | STAFF | VIEWER`) where `tenant_id` is derived strictly from the JWT session (`backend/app/auth.py`).
- Added DPDP Act 2023 Right-to-Erasure endpoint (`DELETE /api/customers/{customer_id}/pii`) and Indian mobile PII masking (`+91******1209`) in logs and LLM prompts.

### NLP & Extraction Pipeline (`backend/app/extractor.py`)
- Replaced the single-pass regex extractor with a 12-stage pure-function pipeline returning `(result, confidence, evidence_span)` at every stage.
- Resolved all 15 Phase 0 extraction failures (`F-01` through `F-15`), achieving **100.00% Test Line F1**, **100.00% Order Exact Match**, **0.00% False Auto-Confirm Rate (`0/206`)**, and **7.89 ms p95 latency**.
- Added candidate-locked Gemini provider abstraction (`backend/app/llm_provider.py`) with SHA-256 response caching and per-tenant monthly budget caps (`monthly_llm_cap_paise`).
- Added tenant-isolated operator alias learning loop (`LearnedAlias`) with 1-click rollback (`POST /api/aliases/{id}/rollback`).

### Database, Financial Math & Accounting Sync
- Consolidated storage onto PostgreSQL 16 + SQLAlchemy 2.0 + Alembic (`0001_initial_schema.py`, 13 tables) and removed unused `backend/prisma/schema.prisma`.
- Implemented exact integer-paise (`BigInteger`) and basis-point (`Integer`) GST arithmetic (`CGST`, `SGST`, `IGST`) in `backend/app/money.py`.
- Upgraded `backend/app/accounting_sync.py` to generate double-entry balanced Tally Prime XML (`Party Debit + Sales Credit + Tax Credits == 0.00`), formula-injection sanitized RFC-4180 CSV, Zoho Books v3 JSON, and deterministic `ErpSyncExport` idempotency keys.
