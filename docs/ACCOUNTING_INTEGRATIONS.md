# Accounting & ERP Integrations Reference

OrderBridge exports **verified (`CONFIRMED` or `SYNCED`)** orders to Indian and international accounting tools via `backend/app/accounting_sync.py`.

---

## 1. Connector Maturity Matrix

| ERP Target | Status | Output Format | Key Safeguards |
|---|---|---|---|
| **Tally Prime** (`TALLY_PRIME`) | **PRODUCTION** | `<ENVELOPE>` XML (`VCHTYPE="Sales Order"`) | Double-entry balanced `ALLLEDGERENTRIES.LIST` (`Party Debit + Sales Credit + CGST/SGST/IGST Credits == 0.00`), XML entity escaping (`xml.sax.saxutils.escape`), HSN & unit quantities. |
| **Excel / CSV Batch** (`EXCEL_CSV`) | **PRODUCTION** | RFC-4180 CSV (`text/csv`) | Formula-injection sanitization (`sanitize_csv_cell` prefixes leading `=`, `+`, `-`, `@`, `\t`, `\r` with `'`), HSN code, CGST/SGST/IGST split. |
| **Zoho Books India** (`ZOHO_BOOKS`) | **PRODUCTION** | Zoho Books v3 Sales Order JSON | Intra-state (`gst_treatment: business_gst`) vs inter-state (`IGST`) tax payload, HSN/SAC mapping, integer-paise derived rupee decimals. |
| **Xero** (`XERO`) | **BETA** | Xero `ACCREC` Invoice JSON | Clearly marked `connector_maturity: "BETA"` in API responses and UI. |

---

## 2. Hard Rule 4: Human-in-the-Loop Export Gate

`POST /api/orders/{order_id}/export/{erp_format}` enforces:
1. If `order.status` is not `CONFIRMED` or `SYNCED`, the server rejects the export with **`409 Conflict`**.
2. An order cannot transition to `CONFIRMED` (`POST /api/orders/{order_id}/confirm`) while any line has `sku_code == "UNKNOWN"` (`400 Bad Request`).
3. Every export persists a deterministic `ErpSyncExport` record (`SHA256(tenant_slug | order_number | erp_target)`) so repeated exports are idempotent (`idempotent_replay: true`).
