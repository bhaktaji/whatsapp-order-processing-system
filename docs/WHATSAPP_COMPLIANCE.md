# WhatsApp Business Cloud API & TRAI DLT Compliance

OrderBridge strictly enforces Meta WhatsApp Business Platform policies and Telecom Regulatory Authority of India (TRAI) TCCCPR 2018 regulations.

---

## 1. Official Meta Documentation References (Verified)

- **Webhook Endpoint & `X-Hub-Signature-256` Payload Verification:**
  `https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint/`
- **24-Hour Customer Service Window & Template Messaging Rules:**
  `https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages`
- **Cloud API Error Codes & Rate Limits (`130429`, `131056`, `131031`):**
  `https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes`
- **WhatsApp Business Messaging Policy:**
  `https://business.whatsapp.com/policy`

---

## 2. Cryptographic Webhook Authentication & `wamid` Idempotency

1. **Challenge Handshake (`GET /webhooks/whatsapp`):** Validates `hub.mode=subscribe` and compares `hub.verify_token` against `META_VERIFY_TOKEN`. Fails closed (`503`/`403`) if unconfigured or mismatched.
2. **HMAC-SHA256 Signature Check (`POST /webhooks/whatsapp`):** Validates `X-Hub-Signature-256: sha256=<hex>` using `hmac.compare_digest` with `META_APP_SECRET`. Unsigned or tampered payloads are rejected with `401 Unauthorized`.
3. **Message ID (`wamid`) Idempotency:** Deduplicates inbound messages on `UniqueConstraint("tenant_id", "wamid")`. Duplicate `wamid` deliveries return `200 OK` with `"duplicates_ignored": ["wamid..."]`.

---

## 3. 24-Hour Customer Service Window, Opt-Out (`STOP`) & Policy Halt

- **24-Hour Service Window (`check_24h_service_window`):** Free-form messages (`FREE_FORM_SESSION_MESSAGE`) are permitted only within 24 hours of `Customer.last_inbound_at`; outside 24 hours, `APPROVED_UTILITY_TEMPLATE_ONLY` is required.
- **Instant Opt-Out (`STOP`):** Texting `"STOP"` or `"BAND KARO"` sets `Customer.opted_out_at = UTC_NOW`, attaches `OPT_OUT_REQUESTED`, and suppresses automated replies.
- **No Ban Evasion (`COMPLIANCE_HALT`):** Meta error codes `131031` and `368` trigger `status: "COMPLIANCE_HALT"` (`failover_triggered: False`). Failover is strictly limited to technical outages (`500`/`503`) and throughput rate limits (`130429`) using TRAI TCCCPR 2018 DLT-registered SMS templates (`dlt_entity_id` + `dlt_template_id`).
