# Security, Privacy & DPDP Act 2023 Compliance

OrderBridge complies with India's **Digital Personal Data Protection Act, 2023 (DPDP Act)**, the **Central Goods and Services Tax (CGST) Act, 2017**, and OWASP API Security Top 10 best practices.

---

## 1. Secret Management & Fail-Closed Configuration

- All sensitive credentials (`DATABASE_URL`, `JWT_SECRET_KEY`, `META_VERIFY_TOKEN`, `META_APP_SECRET`, `GEMINI_API_KEY`) are loaded via `backend/app/config.py` from environment variables or Google Cloud Secret Manager.
- **Fail-Closed Startup (`validate_production_readiness`):** When `APP_ENV=production`, startup aborts if any secret matches `FORBIDDEN_SECRET_VALUES`.

---

## 2. Authentication, RBAC & Tenant Isolation

- **HMAC-SHA256 JWT Session Tokens (`backend/app/auth.py`):** All `/api/orders*`, `/api/aliases*`, `/api/tenant/settings`, and `/api/customers*` endpoints require `Authorization: Bearer <token>`.
- **Strict Tenant Isolation:** `tenant_id` (`auth.tenant_uuid`) and `role` (`OWNER | STAFF | VIEWER`) are extracted strictly from the verified JWT payload. Cross-tenant IDOR attempts return `404 Not Found`.

---

## 3. DPDP Act 2023 Data Minimisation & Right to Erasure

- **Phone Number Hashing & Masking:** Buyer phone numbers are indexed via salted SHA-256 (`Customer.whatsapp_phone_hash`), masked in UI/logs (`+91******1209`), and scrubbed by `mask_pii_text()` before any external Gemini call.
- **Right to Erasure (`DELETE /api/customers/{customer_id}/pii`):** Overwrites `whatsapp_phone_e164`, `whatsapp_phone_masked`, `name`, `business_name`, `InboundMessage.raw_text`, and `DraftOrder.raw_message_text` with `[REDACTED BY DPDP REQUEST]` / `REDACTED-<hash[:10]>`, while preserving statutory GST voucher totals required under Section 35 of the CGST Act 2017 (DPDP Act Section 12(3) exemption).
