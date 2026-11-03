"""Phase 5 & Phase 6 Security, Privacy (DPDP Act 2023), LLM Guardrail, and Compliance Tests."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.config import get_settings
from app.failover import BusinessContinuityController, check_24h_service_window
from app.llm_provider import (
    CandidateSkuOption,
    GeminiExtractionProvider,
    LLMDisambiguationResult,
    mask_pii_text,
    validate_candidate_locked_payload,
)
from app.main import app

client = TestClient(app)


def test_http_security_headers_and_request_id_propagation() -> None:
    """Verify OWASP HTTP security headers and X-Request-ID correlation header on API responses."""
    resp = client.get("/health", headers={"X-Request-ID": "req_audit_check_001"})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == "req_audit_check_001"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert "max-age=31536000" in (resp.headers.get("Strict-Transport-Security") or "")


def test_webhook_oversized_payload_rejected_413() -> None:
    """Verify oversized webhook payloads (>256 KB) are rejected with 413 Payload Too Large."""
    oversized_body = b"x" * 270_000
    resp = client.post(
        "/webhooks/whatsapp",
        content=oversized_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert resp.status_code == 413


def test_dpdp_right_to_erasure_redacts_pii_while_preserving_gst_totals() -> None:
    """Verify DELETE /api/customers/{id}/pii redacts phone/name/raw_text and logs audit event."""
    owner_token = client.post(
        "/api/auth/demo-session",
        json={"tenant_slug": "patel_fmcg", "role": "OWNER"},
    ).json()["access_token"]
    staff_token = client.post(
        "/api/auth/demo-session",
        json={"tenant_slug": "patel_fmcg", "role": "STAFF"},
    ).json()["access_token"]

    sim_resp = client.post(
        "/api/webhooks/whatsapp/simulate",
        json={
            "tenant_slug": "patel_fmcg",
            "sender_phone": "+919899001122",
            "sender_name": "DPDP Erasure Test Buyer",
            "message_text": "5 peti Parle-G bhej do",
        },
    )
    assert sim_resp.status_code == 200
    order = sim_resp.json()["orders"][0]
    customer_id = order["customer_id"]
    original_total_paise = order["total_paise"]
    assert original_total_paise > 0

    forbidden_resp = client.delete(
        f"/api/customers/{customer_id}/pii",
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert forbidden_resp.status_code == 403

    del_resp = client.delete(
        f"/api/customers/{customer_id}/pii",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert del_resp.status_code == 200
    del_body = del_resp.json()
    assert del_body["status"] == "PII_REDACTED"
    assert del_body["redacted_tag"].startswith("REDACTED-")

    orders_after = client.get(
        "/api/orders",
        headers={"Authorization": f"Bearer {owner_token}"},
    ).json()
    matching = [o for o in orders_after if o["id"] == order["id"]]
    assert len(matching) == 1
    redacted_order = matching[0]
    assert redacted_order["total_paise"] == original_total_paise
    assert redacted_order["raw_message"] == "[REDACTED BY DPDP REQUEST]"
    assert redacted_order["customer_name"] == "[REDACTED BY DPDP REQUEST]"
    assert any(a["action"] == "CUSTOMER_PII_REDACTED_DPDP" for a in redacted_order["audit_logs"])


def test_candidate_locked_llm_schema_and_pii_masking() -> None:
    """Verify Hard Rule 5 & 7: LLMs cannot invent SKUs outside candidate set and phone PII is masked."""
    masked = mask_pii_text("Call +91 98204 11209 or 9820411209 urgent")
    assert "98204 11209" not in masked
    assert "9820411209" not in masked
    assert "+91******1209" in masked

    allowed = {"PARLE-G-100G", "AMUL-BUTTER-500G"}
    validated = validate_candidate_locked_payload(
        payload={"sku_id": "INVENTED-SKU-999", "confidence": 0.99, "reason": "guessed"},
        allowed_sku_codes=allowed,
    )
    assert isinstance(validated, LLMDisambiguationResult)
    assert validated.sku_id == "UNKNOWN"
    assert validated.confidence == 0.0

    valid_pick = validate_candidate_locked_payload(
        payload={"sku_id": "PARLE-G-100G", "qty": 5, "unit": "peti", "confidence": 0.80, "reason": "exact match"},
        allowed_sku_codes=allowed,
    )
    assert valid_pick.sku_id == "PARLE-G-100G"
    assert valid_pick.confidence == 0.80

    provider = GeminiExtractionProvider(api_key="")
    fallback = provider.disambiguate_line(
        tenant_id="patel_fmcg",
        raw_line="parle biscuit",
        candidates=[
            CandidateSkuOption(
                sku_code="PARLE-G-100G",
                name="Parle-G",
                pack_size="100g",
                allowed_units=["peti", "pcs"],
            )
        ],
        monthly_spent_paise=500_000,
        monthly_cap_paise=500_000,
    )
    assert fallback.sku_id == "UNKNOWN"


def test_whatsapp_24h_window_and_trai_dlt_continuity_controls() -> None:
    """Verify Hard Rule 6: 24-hour service window check, TRAI DLT validation, and policy enforcement halt."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    recent_inbound = now - timedelta(hours=4)
    expired_inbound = now - timedelta(hours=30)

    win_open = check_24h_service_window(last_inbound_at=recent_inbound, now=now)
    assert win_open["within_24h_window"] is True
    assert win_open["allowed_message_type"] == "FREE_FORM_SESSION_MESSAGE"

    win_closed = check_24h_service_window(last_inbound_at=expired_inbound, now=now)
    assert win_closed["within_24h_window"] is False
    assert win_closed["allowed_message_type"] == "APPROVED_UTILITY_TEMPLATE_ONLY"
    assert win_closed["required_template"] == "order_clarification_utility_v1"

    controller = BusinessContinuityController({"brandName": "Patel FMCG"})
    rate_limit_evt = controller.inspect_meta_webhook_error(error_code=130429)
    assert rate_limit_evt["failover_triggered"] is True
    assert rate_limit_evt["dlt_metadata"]["regulation"] == "TRAI TCCCPR 2018"

    ban_evt = controller.inspect_meta_webhook_error(error_code=131031)
    assert ban_evt["status"] == "COMPLIANCE_HALT"
    assert ban_evt["failover_triggered"] is False


def test_webhook_stop_opt_out_halts_automated_replies() -> None:
    """Verify buyer texting 'STOP' marks customer opted-out and does not create false orders."""
    settings = get_settings()
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
                            "contacts": [{"profile": {"name": "OptOut Buyer"}}],
                            "messages": [
                                {
                                    "id": "wamid.OPTOUT_TEST_002",
                                    "from": "+919811223344",
                                    "type": "text",
                                    "text": {"body": "STOP"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    raw_bytes = json.dumps(envelope).encode("utf-8")
    sig = "sha256=" + hmac.new(settings.meta_app_secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhooks/whatsapp",
        content=raw_bytes,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 200
    order = resp.json()["orders"][0]
    assert len(order["lines"]) == 0
    assert order["customer_opted_out"] is True
    assert "OPT_OUT_REQUESTED" in order["review_reasons"]
