# OrderBridge System Architecture

OrderBridge is a multi-tenant B2B commerce capture platform engineered for Indian distributors, wholesalers, C&F agents, and D2C brands. Buyers place orders over WhatsApp without installing apps or creating accounts; OrderBridge converts unstructured Hinglish, Devanagari, regional-script, voice, and image messages into verified, GST-compliant draft orders and exports confirmed vouchers to Tally Prime, Zoho Books, and Excel/CSV.

---

## 1. End-to-End Message & Order Flow

```mermaid
flowchart TD
    A[Buyer WhatsApp Message / Voice / Image] -->|X-Hub-Signature-256 HMAC| B[POST /webhooks/whatsapp]
    B --> C{wamid Idempotency Check}
    C -->|Duplicate wamid| D[200 OK - Ignored Replay]
    C -->|New wamid| E[12-Stage Extraction Pipeline]
    E --> F{Stage 3: Intent Classification}
    F -->|RATE_ENQUIRY / CANCEL / PAYMENT / OPT_OUT| G[Route to INBOX_REVIEW - 0 Order Lines]
    F -->|ORDER_CREATE| H[Stages 5-8: Line Segmentation, Qty/Unit, SKU Retrieval, Paise Math]
    H --> I{Stage 10: Confidence Routing}
    I -->|All Lines >= 0.85 AND tenant.auto_confirm_enabled=True| J[Status: CONFIRMED]
    I -->|Default Safe Path / Any Line < 0.85 / Risk Flag| K[Status: NEEDS_REVIEW + Clarification Reply Draft]
    K -->|Operator Workbench Review & Edit| L[Operator Alias Learning Loop]
    L -->|POST /api/orders/id/confirm| J
    J -->|POST /api/orders/id/export/tally| M[ErpSyncExport Idempotency Ledger]
    M --> N[Status: SYNCED - Tally Prime XML / Zoho Books / RFC-4180 CSV]
```

---

## 2. Order Lifecycle State Machine

```mermaid
stateDiagram-v2
    [*] --> INBOX_REVIEW: Non-order intent (Rate enquiry / Cancel / Payment / Greeting / Opt-out)
    [*] --> NEEDS_REVIEW: Order intent (Default when auto_confirm_enabled=False or confidence < 0.85)
    [*] --> CONFIRMED: All lines >= 0.85 AND zero risk flags AND auto_confirm_enabled=True
    INBOX_REVIEW --> CANCELLED: Operator dismisses / archives
    NEEDS_REVIEW --> NEEDS_REVIEW: Operator edits SKU / Qty / Unit (PATCH /lines/{line_id})
    NEEDS_REVIEW --> CONFIRMED: Operator confirms (POST /confirm - Zero UNKNOWN SKUs allowed)
    NEEDS_REVIEW --> CANCELLED: Operator cancels (POST /cancel)
    CONFIRMED --> SYNCED: Exported to ERP (POST /export/{erp_format})
    CONFIRMED --> CANCELLED: Operator cancels before ERP sync
```

---

## 3. Multi-Tenant Isolation Model

Every database table (`tenants`, `users`, `tenant_memberships`, `skus`, `sku_units`, `learned_aliases`, `customers`, `inbound_messages`, `draft_orders`, `draft_order_lines`, `order_audit_logs`, `erp_sync_exports`, `raw_webhook_events`) enforces strict tenant isolation:

1. **JWT-Scoped Tenant Context:** Authenticated API endpoints resolve `tenant_uuid` and `role` (`OWNER | STAFF | VIEWER`) exclusively from the HMAC-SHA256 signed JWT bearer token (`backend/app/auth.py`). Path or query parameters cannot override the caller's tenant scope.
2. **Scoped SKU & Alias Catalogues:** `LearnedAlias` and `Sku` rows carry composite uniqueness constraints scoped to `tenant_id` (`uq_alias_tenant_phrase`, `uq_sku_tenant_code`). An operator alias learned in `patel_fmcg` never affects extraction in `glow_insta` or `metro_fresh`.
3. **Scoped Webhook Idempotency:** Inbound WhatsApp messages enforce `UniqueConstraint("tenant_id", "wamid", name="uq_tenant_inbound_wamid")` so Meta webhook retries are deduplicated per tenant without cross-tenant collision.
