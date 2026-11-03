# OrderBridge REST API Reference

All authenticated endpoints require `Authorization: Bearer <access_token>` where `tenant_id` and `role` (`OWNER | STAFF | VIEWER`) are cryptographically bound inside the JWT token.

---

## 1. Probes & Webhooks

- `GET /health` — Liveness probe (`200 OK`)
- `GET /ready` — Database readiness probe (`200 OK` / `503`)
- `GET /webhooks/whatsapp` — Meta Cloud API challenge verification (`hub.verify_token`)
- `POST /webhooks/whatsapp` — Signed Meta Cloud API webhook receiver (`X-Hub-Signature-256`, `wamid` idempotency, `256 KB` payload cap)
- `POST /api/webhooks/whatsapp/simulate` — Signed webhook simulator for interactive verification

---

## 2. Authentication & Order Lifecycle

| Method & Path | Minimum Role | Description |
|---|---|---|
| `POST /api/auth/login` | Public | Authenticate user email/password and receive JWT bearer token. |
| `POST /api/auth/demo-session` | Public | Mint scoped workbench JWT token (`OWNER | STAFF | VIEWER`). |
| `GET /api/orders` | `VIEWER` | List persisted orders for the caller's tenant. |
| `PATCH /api/orders/{order_id}/lines/{line_id}` | `STAFF` | Resolve or edit line SKU/Qty/Unit and optionally learn tenant alias (`learn_alias: true`). |
| `POST /api/orders/{order_id}/confirm` | `STAFF` | Confirm draft order (`400` if any line has `sku_code == "UNKNOWN"`). |
| `POST /api/orders/{order_id}/cancel` | `STAFF` | Cancel order and append `OrderAuditLog`. |
| `POST /api/orders/{order_id}/export/{erp_format}` | `STAFF` | Export `CONFIRMED` order to `TALLY_PRIME`, `EXCEL_CSV`, `ZOHO_BOOKS`, or `XERO` (`409` if unconfirmed). |
| `GET /api/aliases` | `VIEWER` | List tenant-scoped `LearnedAlias` mappings. |
| `POST /api/aliases/{alias_id}/rollback` | `STAFF` | Deactivate a learned alias immediately (`1-click rollback`). |
| `DELETE /api/customers/{customer_id}/pii` | `OWNER` | DPDP Act 2023 Right-to-Erasure PII redaction endpoint. |
