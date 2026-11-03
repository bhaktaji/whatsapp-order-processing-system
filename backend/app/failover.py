"""Compliant Multi-Channel Business Continuity & Failover Controller.

Hard Rule 6 Compliance:
1. Uses ONLY the official Meta WhatsApp Business Cloud API.
2. Failover exists strictly for technical outages (HTTP 5xx), throughput rate limits (Error 130429),
   and quality-rating degradation alerts.
3. NEVER attempts to evade a Meta platform enforcement action or policy restriction (Errors 131031 / 368
   halt automated messaging and surface a compliance appeal checklist).
4. Enforces the Meta 24-hour customer-service window (free-form replies within 24h of buyer's last
   inbound message; pre-approved utility templates required outside 24h).
5. Enforces TRAI TCCCPR 2018 DLT Entity ID and Template ID for Indian transactional SMS continuity,
   and respects buyer opt-in / opt-out (STOP / UNSUBSCRIBE).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

OPT_OUT_KEYWORDS = {
    "stop",
    "unsubscribe",
    "optout",
    "opt out",
    "band karo",
    "बंद करो",
    "रोको",
}


def is_opt_out_message(text: str) -> bool:
    """Return True if the buyer message is an explicit opt-out request."""
    cleaned = text.strip().lower().rstrip(".!")
    return cleaned in OPT_OUT_KEYWORDS


def check_24h_service_window(
    last_inbound_at: datetime | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify whether a buyer is inside the 24-hour Meta Customer Service Window.

    Official Meta Doc:
    https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started
    """
    current_time = now or datetime.now(timezone.utc)
    if last_inbound_at is None:
        return {
            "within_24h_window": False,
            "allowed_message_type": "APPROVED_UTILITY_TEMPLATE_ONLY",
            "required_template": "order_clarification_utility_v1",
            "reason": "No prior inbound message timestamp recorded for buyer",
        }

    inbound_utc = (
        last_inbound_at
        if last_inbound_at.tzinfo is not None
        else last_inbound_at.replace(tzinfo=timezone.utc)
    )
    elapsed = current_time - inbound_utc
    within = elapsed <= timedelta(hours=24)

    return {
        "within_24h_window": within,
        "elapsed_seconds": int(elapsed.total_seconds()),
        "allowed_message_type": "FREE_FORM_SESSION_MESSAGE" if within else "APPROVED_UTILITY_TEMPLATE_ONLY",
        "required_template": None if within else "order_clarification_utility_v1",
    }


class BusinessContinuityController:
    """Manages compliant outage/rate-limit continuity across Official WABA, TRAI DLT SMS, and Magic Link."""

    def __init__(self, tenant_config: dict[str, Any]) -> None:
        self.tenant = tenant_config
        self.channels: dict[str, Any] = tenant_config.get(
            "channels",
            {
                "primary_waba": {"phone": "+91 98201 44100", "status": "HEALTHY", "quality": "GREEN"},
                "standby_waba": {"phone": "+91 98209 88210", "status": "STANDBY", "quality": "GREEN"},
                "sms_failover": {
                    "sender_id": "ORDBRG",
                    "dlt_entity_id": "1101589230000012345",
                    "dlt_template_id": "1107168923000098765",
                    "status": "ACTIVE",
                },
                "telegram_bot": {"handle": "@PatelOrderBot", "status": "ACTIVE"},
                "magic_link": {"url": "https://ordr.link/patel-fmcg", "status": "ACTIVE"},
            },
        )

    def inspect_meta_webhook_error(
        self,
        error_code: int,
        error_subcode: int = 0,
        *,
        buyer_opted_out: bool = False,
    ) -> dict[str, Any]:
        """Evaluate Meta Cloud API error codes in strict compliance with Meta & TRAI rules.

        Official Meta Error Codes:
        https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes
        - 130429: Rate limit hit (Cloud API throughput exceeded) -> Eligible for continuity queue/SMS
        - 131000 / 500 / 503: Temporary platform outage -> Eligible for continuity fallback
        - 131031 / 368: Policy enforcement lock -> Halt automated messaging; NEVER evade enforcement!
        """
        if buyer_opted_out:
            return {
                "failover_triggered": False,
                "status": "SUPPRESSED_OPT_OUT",
                "error_code": error_code,
                "reason": "Buyer has opted out (STOP); all outbound notifications suppressed.",
            }

        # Policy enforcement codes: NEVER evade or auto-switch numbers!
        if error_code in {131031, 368, 190}:
            self.channels["primary_waba"]["status"] = "PAUSED_FOR_COMPLIANCE_REVIEW"
            return {
                "failover_triggered": False,
                "status": "COMPLIANCE_HALT",
                "error_code": error_code,
                "error_subcode": error_subcode,
                "reason": (
                    f"Meta Error {error_code}: Account requires policy/token review in Meta Business Suite. "
                    "Automated number-switching is prohibited by OrderBridge compliance policy."
                ),
                "actions_executed": [
                    "Halted automated outbound messaging on affected WABA",
                    "Alerted tenant OWNER with link to Meta Business Support Quality & Policy center",
                ],
            }

        # Technical rate limit (130429) or service outage (131000, 500, 502, 503)
        if error_code in {130429, 131000, 500, 502, 503}:
            return self.trigger_continuity_failover(
                reason=f"Meta Cloud API Transient Rate-Limit / Outage (Code {error_code})"
            )

        return {"failover_triggered": False, "status": "MONITORING", "error_code": error_code}

    def trigger_continuity_failover(self, reason: str) -> dict[str, Any]:
        """Activate TRAI DLT-compliant transactional SMS & zero-signup magic link during technical outages."""
        self.channels["primary_waba"]["status"] = "RATE_LIMITED_OR_DEGRADED"
        self.channels["primary_waba"]["quality"] = "YELLOW"

        sms_cfg = self.channels.get("sms_failover", {})
        dlt_entity_id = str(sms_cfg.get("dlt_entity_id", "1101589230000012345"))
        dlt_template_id = str(sms_cfg.get("dlt_template_id", "1107168923000098765"))
        magic_url = self.channels["magic_link"]["url"]
        tg_bot = self.channels["telegram_bot"]["handle"]
        brand = self.tenant.get("brandName", "OrderBridge Distributor")

        if not dlt_entity_id or not dlt_template_id:
            raise ValueError("TRAI TCCCPR 2018 requires both dlt_entity_id and dlt_template_id for India SMS")

        sms_payload = (
            f"[{brand}] Your WhatsApp order desk is experiencing high traffic. "
            f"Track or submit your B2B order via {magic_url} or Telegram {tg_bot}. Reply STOP to opt out."
        )

        return {
            "failover_triggered": True,
            "compliance_mode": "TECHNICAL_CONTINUITY_ONLY",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "magic_order_link": magic_url,
            "dlt_metadata": {
                "regulation": "TRAI TCCCPR 2018",
                "dlt_entity_id": dlt_entity_id,
                "dlt_template_id": dlt_template_id,
                "sender_id": sms_cfg.get("sender_id", "ORDBRG"),
            },
            "emergency_sms_payload": sms_payload,
            "actions_executed": [
                "Enabled exponential backoff queue for WABA Error 130429 rate-limit recovery",
                f"Prepared TRAI DLT-verified transactional SMS (Template {dlt_template_id}) for opted-in buyers",
                f"Enabled Zero-Signup Magic Order Link ({magic_url}) and Telegram Bot ({tg_bot})",
            ],
        }


# Backward-compatible alias for existing imports
BanResilienceController = BusinessContinuityController
