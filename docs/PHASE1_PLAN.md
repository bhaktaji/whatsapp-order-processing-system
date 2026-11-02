# Phase 1 Implementation Plan — Baseline, Security Remediation & Database Foundation

## 1. Files to Be Added, Modified, or Removed

| Action | File Path | Purpose |
| :--- | :--- | :--- |
| **DELETE** | `push_to_github.bat`, `push_to_github.sh` | Remove unsafe root git push scripts. |
| **DELETE** | `backend/prisma/schema.prisma`, `backend/prisma/` | Remove unused Node.js Prisma schema in favour of SQLAlchemy 2.0 + Alembic. |
| **ADD** | `docs/adr/0001-postgresql-sqlalchemy-alembic.md` | Document migration to PostgreSQL 16, SQLAlchemy 2.0 + Alembic, and integer-paise financial math. |
| **ADD** | `backend/Dockerfile` | Multi-stage slim Python 3.11+ image running as non-root user (`orderbridge`), honoring `$PORT`. |
| **UPDATE** | `docker-compose.yml` | Remove hardcoded `POSTGRES_PASSWORD`, remove unused `redis` service, add PostgreSQL healthcheck, wire `api` service with migrations & seed. |
| **UPDATE** | `.env.example`, `.gitignore` | Define required environment variables without secrets; ignore `.env`, `__pycache__`, `.venv`, `dist`, `.mypy_cache`, `.ruff_cache`. |
| **UPDATE** | `backend/requirements.txt`, `backend/pyproject.toml` | Pin runtime + QA dependencies (`sqlalchemy`, `alembic`, `psycopg[binary]`, `pydantic-settings`, `pytest`, `ruff`, `mypy`, `hypothesis`) and configure linters/typecheckers. |
| **ADD** | `.pre-commit-config.yaml` | Configure `ruff`, `mypy`, and secret/credential leak guards. |
| **ADD** | `.github/workflows/ci.yml` | GitHub Actions CI running `postgres:16-alpine`, `ruff`, `mypy`, `alembic upgrade head`, `python -m app.seed`, `pytest`, and frontend build. |
| **ADD** | `backend/app/config.py` | Environment-driven `Settings` + optional GCP Secret Manager loader; fail-fast validation rejecting missing or default secrets in staging/prod. |
| **ADD** | `backend/app/money.py` | Deterministic integer-paise and basis-points GST financial calculation engine (CGST/SGST vs IGST, zero floating-point arithmetic). |
| **ADD** | `backend/app/db.py`, `backend/app/models.py` | SQLAlchemy 2.0 engine/session factory and relational models (`Tenant`, `User`, `TenantMembership`, `Sku`, `SkuUnit`, `Customer`, `InboundMessage`, `DraftOrder`, `DraftOrderLine`, `OrderAuditLog`, `LearnedAlias`, `ErpSyncExport`, `ProcessingJob`). |
| **ADD** | `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/versions/0001_initial_schema.py` | Alembic migration configuration and initial baseline schema revision. |
| **UPDATE** | `config/tenants.json` & `backend/app/seed.py` | Unified canonical demo-tenant catalog with integer paise prices, per-SKU unit multipliers, HSN codes, and idempotent database seeder (`python -m app.seed`). |
| **UPDATE** | `backend/app/main.py` | Wire `/health`, `/ready` (DB connectivity check), env-driven CORS, and database-backed tenant & order endpoints. |
| **ADD** | `backend/tests/test_phase1_baseline.py` | Automated tests for `/health`, `/ready`, migrations, idempotent seed, integer-paise math, `wamid` idempotency, tenant isolation, and fail-fast secret enforcement. |
| **UPDATE** | `README.md`, `AUDIT.md` | Sync repository layout, quickstart instructions, and Phase 1 baseline verification evidence. |

---

## 2. Database & Financial Decisions
- **Production Database:** PostgreSQL 16 (`Cloud SQL` in staging/production; `postgres:16-alpine` in Docker Compose and GitHub Actions CI).
- **Financial Precision:** All prices, line totals, tax amounts, and spending caps are stored as integer paise (`BigInteger`), and tax rates as integer basis points (`Integer`, e.g., `1800` = `18.00%`). Floating-point arithmetic is banned from financial calculations.
- **Per-SKU Units:** Modelled in `sku_units` with `(tenant_id, sku_id, unit_code)` uniqueness and rational conversion factors (`multiplier_num`, `multiplier_den`) to the SKU's `base_unit`.

---

## 3. Security Remediation in Phase 1
- Remove fallback `"orderbridge_webhook_verify_2026"` from `backend/app/main.py` and `"secretpassword"` from `docker-compose.yml`.
- Add startup validation in `backend/app/config.py` that fails fast if `META_VERIFY_TOKEN`, `META_APP_SECRET`, `JWT_SECRET`, or `DATABASE_URL` are unset or match known placeholder strings when `APP_ENV` is `staging` or `production`.
- Replace wildcard `allow_origins=["*"]` with environment-configured `CORS_ALLOWED_ORIGINS`.

---

## 4. Regression Test Strategy & Acceptance Criteria
- **Linter & Type Check:** `ruff check backend` and `mypy backend/app` exit with `0` errors.
- **Database Migration & Seed:** `alembic upgrade head` and `python -m app.seed` succeed cleanly and are idempotent when run twice.
- **Automated Pytest Suite (`pytest backend/tests`):** Verifies `/health`, `/ready`, integer-paise GST math, `wamid` uniqueness constraint, per-SKU unit conversions, tenant isolation, and fail-fast secret checks.
- **GitHub Actions CI:** Pushed to `release/prod-ready` on GitHub and verified.
