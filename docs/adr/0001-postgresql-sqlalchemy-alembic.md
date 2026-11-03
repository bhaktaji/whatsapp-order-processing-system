# ADR 0001: Standardising on PostgreSQL 16 with SQLAlchemy 2.0, Alembic, and Integer Paise Financials

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** Repository Owner, Backend & QA Lead

---

## 1. Context & Problem Statement

In the initial commit (`d4600d2e`) and early prototyping:
1. Local prototyping relied on ad-hoc JSON files (`config/tenants.json`), browser `localStorage`, or lightweight SQLite assumptions without transactional concurrency guarantees, while `backend/prisma/schema.prisma` declared a Node.js generator (`prisma-client-js`) that was never wired to the Python FastAPI backend.
2. Financial fields (`basePrice`, `wholesalePrice`, `subtotal`, `taxAmount`, `grandTotal`) were defined as IEEE-754 floating-point numbers (`Float`), which introduce binary rounding errors (`0.1 + 0.2 != 0.3`) on multi-line GST invoices (CGST, SGST, IGST) and fail Tally Prime double-entry balancing rules.
3. Unit multipliers (`peti`, `katta`, `bori`, `dozen`) were modeled as a single tenant-wide JSON blob rather than per-SKU relational records.
4. Webhook ingestion lacked database-enforced `wamid` idempotency, row-level locking for multi-message debounce aggregation, and immutable audit logging.

---

## 2. Decision

1. **Primary Database (`PostgreSQL 16`):**
   - PostgreSQL 16 is the production and staging database (Cloud SQL for PostgreSQL in `asia-south1`, and `postgres:16-alpine` in `docker-compose.yml` and GitHub Actions CI).
   - Why PostgreSQL over SQLite in production:
     - Concurrent write scaling and `SELECT ... FOR UPDATE SKIP LOCKED` support for our durable database-backed webhook & debounce job queue (eliminating the need for a separate Redis/Memorystore cluster).
     - Strict `UNIQUE (tenant_id, wamid)` constraints under concurrent Meta webhook retries.
     - Native transactional DDL for zero-surprise Alembic migrations.
   - Note on hermetic local unit testing: SQLAlchemy 2.0 models use portable standard types (`BigInteger`, `Integer`, `String`, `Text`, `Boolean`, `DateTime(timezone=True)`, `JSON`) so fast unit tests can also run against an ephemeral SQLite engine when a local PostgreSQL daemon is not present, while CI and staging always verify against PostgreSQL 16.

2. **ORM & Migrations (`SQLAlchemy 2.0` + `Alembic`):**
   - Replace the unused `backend/prisma/schema.prisma` with typed SQLAlchemy 2.0 `Mapped[]` declarative models (`backend/app/models.py`) and Alembic migrations (`backend/alembic/`).

3. **Integer Paise / Minor Currency Units (`BigInteger`) & Basis Points (`Integer`):**
   - All monetary values (`base_price_paise`, `wholesale_price_paise`, `distributor_price_paise`, `unit_price_paise`, `line_subtotal_paise`, `cgst_paise`, `sgst_paise`, `igst_paise`, `tax_amount_paise`, `grand_total_paise`, `monthly_llm_cap_paise`) are stored and calculated in **integer paise / minor currency units** (`1 INR = 100 paise`).
   - All tax rates (`tax_rate_bps`) are stored in **integer basis points** (`18.00% GST = 1800 bps`, `5.00% GST = 500 bps`).
   - Deterministic tax math uses integer arithmetic with half-even / half-up rounding in paise, guaranteeing `subtotal_paise + cgst_paise + sgst_paise + igst_paise == grand_total_paise` with zero floating-point drift.

4. **Per-SKU Unit Conversion (`sku_units` table):**
   - Each `Sku` defines a `base_unit` (e.g., `Peti`, `Katta`, `Kg`, `Pcs`) and has $1..N$ `SkuUnit` rows scoped to `(tenant_id, sku_id, unit_code)` with explicit integer numerator/denominator conversion factors (`multiplier_num`, `multiplier_den`) and optional `unit_price_override_paise`.

5. **Tenant Isolation, RBAC, Idempotency & Auditability:**
   - Every tenant-owned table includes a non-nullable `tenant_id` foreign key indexed with composite unique constraints.
   - RBAC is enforced via `users` and `tenant_memberships` (`OWNER`, `STAFF`, `VIEWER`).
   - Webhook idempotency is enforced via `UNIQUE (tenant_id, wamid)` on `inbound_messages`.
   - Every state transition (`DRAFT -> NEEDS_REVIEW -> CONFIRMED -> SYNCED / FAILED`) and manual line edit writes an immutable row to `order_audit_logs`.

---

## 3. Consequences

- **Positive:** Zero floating-point rounding bugs in Tally Prime XML / GST invoices; no Node/Rust Prisma binary dependency inside the Python container; durable transactional job queue in PostgreSQL without Redis cost; reproducible schema migrations via `alembic upgrade head`.
- **Negative / Trade-offs:** API serialization layers must convert `_paise` integers to formatted decimal strings (`"560.00"`) at the presentation boundary for human readability and ERP XML/CSV output. We centralize this in a tested `money.py` utility module.
