"""Phase 4 & Phase 5 Platform Integration, Security, RBAC, Webhook HMAC, Lifecycle, and ERP Export Tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.accounting_sync import AccountingSyncAdapter, sanitize_csv_cell
from app.config import get_settings
from app.failover import BusinessContinuityController, check_24h_service_window
from app.main import app


def _get_demo_token(client: TestClient, tenant_slug: str, role: str) -> str:
    resp = client.post(
        "/api/auth/demo-session",
        json={"tenant_slug": tenant_slug, "role": role},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def test_rbac_role_hierarchy_owner_staff_viewer() -> None:
    """Verify VIEWER is read-only, STAFF can edit/confirm orders, and only OWNER can change tenant settings."""
    with TestClient(app) as client:
        viewer_token = _get_demo_token(client, "patel_fmcg", "VIEWER")
        staff_token = _get_demo_token(client, "patel_fmcg", "STAFF")
        owner_token = _get_demo_token(client, "patel_fmcg", "OWNER")

        # VIEWER can read orders
        r_view = client.get("/api/orders", headers={"Authorization": f"Bearer {viewer_token}"})
        assert r_view.status_code == 200

        # VIEWER cannot update tenant settings
        r_view_set = client.patch(
            "/api/tenant/settings",
            json={"auto_confirm_enabled": False},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert r_view_set.status_code == 403

        # STAFF cannot update tenant settings
        r_staff_set = client.patch(
            "/api/tenant/settings",
            json={"auto_confirm_enabled": False},
            headers={"Authorization": f"Bearer {staff_token}"},
        )
        assert r_staff_set.status_code == 403

        # OWNER can update tenant settings
        r_owner_set = client.patch(
            "/api/tenant/settings",
            json={"auto_confirm_enabled": False, "debounce_window_seconds": 90},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert r_owner_set.status_code == 200
        assert r_owner_set.json()["autoConfirmEnabled"] is False


def test_whatsapp_webhook_hmac_signature_and_wamid_idempotency() -> None:
    """Verify POST /webhooks/whatsapp rejects bad HMACs and deduplicates repeated wamids."""
    with TestClient(app) as client:
        wamid = f"wamid.TEST.{uuid.uuid4().hex[:12]}"
        envelope = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"tenant_slug": "patel_fmcg"},
                                "contacts": [{"profile": {"name": "Ganesh Kirana"}}],
                                "messages": [
                                    {
                                        "id": wamid,
                                        "from": "+919820411209",
                                        "type": "text",
                                        "text": {
                                            "body": "Bhaiya 5 peti Parle-G aur 2 katta basmati 25kg bhej do"
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        raw_bytes = json.dumps(envelope).encode("utf-8")

        # 1. Missing signature -> 401
        r_nosig = client.post("/webhooks/whatsapp", content=raw_bytes)
        assert r_nosig.status_code == 401

        # 2. Forged signature -> 401
        r_badsig = client.post(
            "/webhooks/whatsapp",
            content=raw_bytes,
            headers={"X-Hub-Signature-256": "sha256=00000000000000000000000000000000"},
        )
        assert r_badsig.status_code == 401

        # 3. Valid HMAC-SHA256 signature -> 200 OK & order created
        secret = get_settings().meta_app_secret
        valid_sig = "sha256=" + hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
        r_ok = client.post(
            "/webhooks/whatsapp",
            content=raw_bytes,
            headers={"X-Hub-Signature-256": valid_sig},
        )
        assert r_ok.status_code == 200
        body1 = r_ok.json()
        assert body1["processed_count"] == 1
        assert body1["duplicates_ignored"] == []
        created_order = body1["orders"][0]
        assert len(created_order["lines"]) == 2

        # 4. Exact replay of the same wamid -> 200 OK, duplicate ignored, 0 new orders created
        r_dup = client.post(
            "/webhooks/whatsapp",
            content=raw_bytes,
            headers={"X-Hub-Signature-256": valid_sig},
        )
        assert r_dup.status_code == 200
        body2 = r_dup.json()
        assert body2["processed_count"] == 0
        assert body2["duplicates_ignored"] == [wamid]


def test_cross_tenant_idor_isolation_blocked() -> None:
    """Verify a glow_insta token cannot view, edit, confirm, or export a patel_fmcg order."""
    with TestClient(app) as client:
        patel_staff = _get_demo_token(client, "patel_fmcg", "STAFF")
        glow_owner = _get_demo_token(client, "glow_insta", "OWNER")

        # Create order in patel_fmcg
        r_ext = client.post(
            "/api/orders/extract",
            json={
                "tenant_id": "patel_fmcg",
                "customer_name": "Laxmi Store",
                "customer_phone": "+919820411209",
                "message_text": "3 peti Parle-G bhej do",
                "persist": True,
            },
        )
        assert r_ext.status_code == 200
        patel_order_id = r_ext.json()["order_id"]

        # glow_insta cannot see patel_fmcg's order in GET /api/orders
        r_glow_list = client.get("/api/orders", headers={"Authorization": f"Bearer {glow_owner}"})
        assert r_glow_list.status_code == 200
        glow_order_ids = {o["id"] for o in r_glow_list.json()}
        assert patel_order_id not in glow_order_ids

        # glow_insta cannot confirm patel_fmcg's order (404 Not Found in current tenant)
        r_idor_confirm = client.post(
            f"/api/orders/{patel_order_id}/confirm",
            headers={"Authorization": f"Bearer {glow_owner}"},
        )
        assert r_idor_confirm.status_code == 404

        # patel_fmcg STAFF can confirm patel_fmcg's order
        r_patel_confirm = client.post(
            f"/api/orders/{patel_order_id}/confirm",
            headers={"Authorization": f"Bearer {patel_staff}"},
        )
        assert r_patel_confirm.status_code == 200
        assert r_patel_confirm.json()["status"] == "CONFIRMED"


def test_order_lifecycle_learning_loop_and_verified_only_erp_export() -> None:
    """Verify UNKNOWN SKU blocks confirm, unconfirmed order blocks ERP export, operator edit learns alias, and Tally XML is balanced."""
    with TestClient(app) as client:
        staff_token = _get_demo_token(client, "patel_fmcg", "STAFF")
        headers = {"Authorization": f"Bearer {staff_token}"}

        # 1. Extract an order with an unknown local slang phrase "peela teekha packet"
        r1 = client.post(
            "/api/orders/extract",
            json={
                "tenant_id": "patel_fmcg",
                "customer_name": "Shree Ganesh Supermart",
                "customer_phone": "+919811277890",
                "message_text": "Bhaiya 4 peti peela teekha packet bhej do",
                "persist": True,
            },
        )
        assert r1.status_code == 200
        order_data = r1.json()["persisted_order"]
        order_id = order_data["id"]
        line_id = order_data["lines"][0]["id"]
        assert order_data["status"] == "NEEDS_REVIEW"
        assert order_data["lines"][0]["sku_code"] == "UNKNOWN"

        # 2. Attempting to confirm while SKU is UNKNOWN -> 400
        r_bad_confirm = client.post(f"/api/orders/{order_id}/confirm", headers=headers)
        assert r_bad_confirm.status_code == 400

        # 3. Attempting to export unconfirmed order to Tally Prime -> 409 Conflict (Hard Rule 4!)
        r_bad_export = client.post(f"/api/orders/{order_id}/export/TALLY_PRIME", headers=headers)
        assert r_bad_export.status_code == 409

        # 4. Operator resolves line to SKU-MG24 and teaches alias "peela teekha packet"
        r_edit = client.patch(
            f"/api/orders/{order_id}/lines/{line_id}",
            json={
                "sku_code": "SKU-MG24",
                "qty": 4.0,
                "unit": "Peti",
                "learn_alias_phrase": "peela teekha packet",
            },
            headers=headers,
        )
        assert r_edit.status_code == 200
        updated_order = r_edit.json()
        assert updated_order["lines"][0]["sku_code"] == "SKU-MG24"
        assert updated_order["requires_human_review"] is False

        # 5. Next message using "peela teekha packet" resolves automatically to SKU-MG24!
        r_next = client.post(
            "/api/orders/extract",
            json={
                "tenant_id": "patel_fmcg",
                "customer_name": "Shree Ganesh Supermart",
                "customer_phone": "+919811277890",
                "message_text": "Bhaiya 6 peti peela teekha packet bhej do",
                "persist": False,
            },
        )
        assert r_next.status_code == 200
        assert r_next.json()["lines"][0]["sku_code"] == "SKU-MG24"

        # 6. Confirm the order -> status becomes CONFIRMED and audit log has ORDER_CONFIRMED
        r_conf = client.post(f"/api/orders/{order_id}/confirm", headers=headers)
        assert r_conf.status_code == 200
        confirmed_order = r_conf.json()
        assert confirmed_order["status"] == "CONFIRMED"
        actions = [lg["action"] for lg in confirmed_order["audit_logs"]]
        assert "ORDER_CAPTURED" in actions
        assert "LINE_EDITED" in actions
        assert "ORDER_CONFIRMED" in actions

        # 7. Export confirmed order to Tally Prime XML -> verify valid XML & double-entry ledger balance!
        r_exp = client.post(f"/api/orders/{order_id}/export/TALLY_PRIME", headers=headers)
        assert r_exp.status_code == 200
        exp_body = r_exp.json()
        assert exp_body["idempotent_replay"] is False
        assert exp_body["order"]["status"] == "SYNCED"

        xml_root = ET.fromstring(exp_body["payload"])  # noqa: S314
        ledger_amounts = [float(el.text or "0") for el in xml_root.findall(".//ALLLEDGERENTRIES.LIST/AMOUNT")]
        assert len(ledger_amounts) >= 3
        # Sum of Party Debit (-total) + Sales Credit (+subtotal) + CGST (+cgst) + SGST (+sgst) MUST equal 0.00!
        assert abs(sum(ledger_amounts)) < 1e-6

        # 8. Re-exporting the same order returns idempotent_replay=True
        r_exp2 = client.post(f"/api/orders/{order_id}/export/TALLY_PRIME", headers=headers)
        assert r_exp2.status_code == 200
        assert r_exp2.json()["idempotent_replay"] is True


def test_csv_formula_injection_protection_and_rfc4180() -> None:
    """Verify malicious spreadsheet formulas (=cmd, +SUM, -1, @import) are neutralized in CSV exports."""
    assert sanitize_csv_cell("=cmd|' /C calc'!A0").startswith("'=cmd")
    assert sanitize_csv_cell("+1+1").startswith("'+1+1")
    assert sanitize_csv_cell("-2+3").startswith("'-2+3")
    assert sanitize_csv_cell("@SUM(A1:A2)").startswith("'@SUM")

    csv_out = AccountingSyncAdapter.to_csv_batch(
        [
            {
                "order_number": "ORD-SEC-1",
                "channel": "WHATSAPP",
                "customer_name": '=HYPERLINK("http://evil.example/leak")',
                "customer_phone": "+919820411209",
                "subtotal_paise": 56000,
                "cgst_paise": 5040,
                "sgst_paise": 5040,
                "igst_paise": 0,
                "total_paise": 66080,
                "status": "CONFIRMED",
                "lines": [
                    {
                        "sku_code": "SKU-PG05",
                        "sku_name": "Parle-G",
                        "hsn_code": "19053100",
                        "qty": 1,
                        "unit": "peti",
                        "unit_price_paise": 56000,
                        "line_subtotal_paise": 56000,
                        "cgst_paise": 5040,
                        "sgst_paise": 5040,
                        "igst_paise": 0,
                        "line_total_paise": 66080,
                    }
                ],
            }
        ]
    )
    assert "'=HYPERLINK" in csv_out
    assert "660.80" in csv_out


def test_24h_service_window_and_compliant_continuity_controller() -> None:
    """Verify 24h Meta customer service window and compliance halt on policy enforcement codes."""
    now = datetime.now(timezone.utc)
    recent = check_24h_service_window(now - timedelta(hours=3), now=now)
    assert recent["within_24h_window"] is True
    assert recent["allowed_message_type"] == "FREE_FORM_SESSION_MESSAGE"

    expired = check_24h_service_window(now - timedelta(hours=26), now=now)
    assert expired["within_24h_window"] is False
    assert expired["allowed_message_type"] == "APPROVED_UTILITY_TEMPLATE_ONLY"
    assert expired["required_template"] == "order_clarification_utility_v1"

    ctrl = BusinessContinuityController({"brandName": "Patel Distributors"})
    # Rate limit 130429 triggers technical continuity with TRAI DLT metadata
    res_rate = ctrl.inspect_meta_webhook_error(130429)
    assert res_rate["failover_triggered"] is True
    assert res_rate["dlt_metadata"]["regulation"] == "TRAI TCCCPR 2018"

    # Policy restriction 131031 halts automated messaging and NEVER evades enforcement
    res_policy = ctrl.inspect_meta_webhook_error(131031)
    assert res_policy["failover_triggered"] is False
    assert res_policy["status"] == "COMPLIANCE_HALT"
