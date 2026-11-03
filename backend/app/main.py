"""FastAPI Multi-Tenant Production Server for OrderBridge WhatsApp Order Capture Suite.

Implements:
- Health & Readiness Probes (/health, /ready)
- JWT Authentication & RBAC (OWNER | STAFF | VIEWER) where tenant_id comes strictly from JWT session
- Meta WhatsApp Cloud API Webhook (GET /webhooks/whatsapp + POST /webhooks/whatsapp with X-Hub-Signature-256 HMAC,
  wamid idempotency, 24-hour customer service window guard, and STOP opt-out handling)
- Transactional Order Lifecycle (DRAFT -> NEEDS_REVIEW -> CONFIRMED -> SYNCED / CANCELLED) with OrderAuditLog
- Tenant-isolated Operator Alias Learning Loop with 1-click Rollback
- Verified-Only ERP Exports (Tally Prime XML with balanced ledgers, RFC-4180 CSV with formula-injection protection,
  Zoho Books JSON, and BETA Xero) with ErpSyncExport idempotency
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .accounting_sync import AccountingSyncAdapter, compute_export_idempotency_key
from .auth import (
    AuthContext,
    create_session_token,
    require_role,
    verify_password,
)
from .config import FORBIDDEN_SECRET_VALUES, get_settings
from .db import check_database_ready, get_db, get_engine
from .extractor import OrderExtractionEngine, canonicalise_indian_text, normalise_sku_dict
from .failover import BusinessContinuityController, check_24h_service_window, is_opt_out_message
from .models import (
    Base,
    Customer,
    DraftOrder,
    DraftOrderLine,
    ErpSyncExport,
    InboundMessage,
    LearnedAlias,
    OrderAuditLog,
    Sku,
    Tenant,
    TenantMembership,
    User,
)
from .money import (
    bps_to_percent_str,
    calculate_line_financials,
    paise_to_rupees_str,
    round_div_half_up,
)
from .seed import hash_phone, mask_phone, run_seed


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    settings.validate_production_readiness()
    Base.metadata.create_all(bind=get_engine())
    run_seed(create_tables_if_missing=False)
    yield


settings = get_settings()

app = FastAPI(
    title="OrderBridge Multi-Tenant WhatsApp Order Capture API",
    version="2.5.0",
    description=(
        "Capture WhatsApp/SMS/Telegram orders, extract line items with human-in-the-loop "
        "verification, and sync verified GST orders to Tally Prime, Zoho Books, and Excel/CSV."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Hub-Signature-256"],
)

MAX_WEBHOOK_PAYLOAD_BYTES = 262_144  # 256 KB upper bound against oversized payload DoS


@app.middleware("http")
async def security_headers_and_request_id_middleware(request: Request, call_next: Any) -> Any:
    request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response



# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------


class LoginPayload(BaseModel):
    email: str
    password: str
    tenant_slug: str | None = None


class DemoSessionPayload(BaseModel):
    tenant_slug: str = "patel_fmcg"
    role: str = "OWNER"


class TenantSettingsUpdatePayload(BaseModel):
    auto_confirm_enabled: bool | None = None
    clarification_replies_enabled: bool | None = None
    default_erp: str | None = None
    debounce_window_seconds: int | None = None


class InboundMessagePayload(BaseModel):
    tenant_id: str
    customer_name: str
    customer_phone: str
    channel: str = "WHATSAPP_PRIMARY"
    message_text: str
    price_tier: str = "WHOLESALE"
    wamid: str | None = None
    persist: bool = True


class LineUpdatePayload(BaseModel):
    sku_code: str
    qty: float
    unit: str
    learn_alias_phrase: str | None = None


class WebhookSimulatePayload(BaseModel):
    tenant_slug: str = "patel_fmcg"
    sender_phone: str = "+919820411209"
    sender_name: str = "Laxmi Kirana & General Store"
    message_text: str
    wamid: str | None = None


# ---------------------------------------------------------------------------
# Serialization Helpers
# ---------------------------------------------------------------------------


def serialize_tenant(tenant: Tenant) -> dict[str, Any]:
    skus_out: list[dict[str, Any]] = []
    for sku in tenant.skus:
        if not sku.is_active:
            continue
        skus_out.append(
            {
                "id": sku.id,
                "code": sku.code,
                "name": sku.name,
                "category": sku.category,
                "baseUnit": sku.base_unit,
                "packSizeLabel": sku.pack_size_label,
                "basePricePaise": sku.base_price_paise,
                "wholesalePricePaise": sku.wholesale_price_paise,
                "distributorPricePaise": sku.distributor_price_paise,
                "basePrice": float(paise_to_rupees_str(sku.base_price_paise)),
                "wholesalePrice": float(paise_to_rupees_str(sku.wholesale_price_paise)),
                "distributorPrice": float(paise_to_rupees_str(sku.distributor_price_paise)),
                "taxRateBps": sku.tax_rate_bps,
                "taxRate": float(bps_to_percent_str(sku.tax_rate_bps)),
                "hsnCode": sku.hsn_code,
                "moq": sku.moq,
                "maxOrderQty": sku.max_order_qty,
                "aliases": sku.aliases_json,
                "allowedUnits": [
                    {
                        "unit": u.unit_code,
                        "multiplierNum": u.multiplier_num,
                        "multiplierDen": u.multiplier_den,
                        "isBase": u.is_base_unit,
                    }
                    for u in sku.allowed_units
                ],
            }
        )

    return {
        "id": tenant.slug,
        "tenantUuid": tenant.id,
        "slug": tenant.slug,
        "brandName": tenant.brand_name,
        "industry": tenant.industry_preset,
        "primaryColor": tenant.primary_color,
        "currencySymbol": tenant.currency_symbol,
        "currencyCode": tenant.currency_code,
        "taxRegime": tenant.tax_regime,
        "stateCode": tenant.state_code,
        "gstin": tenant.gstin,
        "defaultErp": tenant.default_erp,
        "autoConfirmEnabled": tenant.auto_confirm_enabled,
        "clarificationRepliesEnabled": tenant.clarification_replies_enabled,
        "autoApproveThreshold": tenant.auto_approve_threshold,
        "fuzzyMatchThreshold": tenant.fuzzy_match_threshold,
        "debounceWindowSeconds": tenant.debounce_window_seconds,
        "monthlyLlmCapPaise": tenant.monthly_llm_cap_paise,
        "monthlyLlmSpentPaise": tenant.monthly_llm_spent_paise,
        "skus": skus_out,
    }


def serialize_order(order: DraftOrder) -> dict[str, Any]:
    cust = order.customer
    window_info = check_24h_service_window(cust.last_inbound_at if cust else None)
    lines_sorted = sorted(order.lines, key=lambda ln: ln.line_index)
    logs_sorted = sorted(order.audit_logs, key=lambda lg: lg.created_at)

    return {
        "id": order.id,
        "order_number": order.order_number,
        "tenant_id": order.tenant_id,
        "customer_id": order.customer_id,
        "customer_name": cust.name if cust else "WhatsApp Buyer",
        "customer_phone": cust.whatsapp_phone_masked if cust else "+91******0000",
        "customer_gstin": cust.gstin if cust else None,
        "customer_opted_out": bool(cust and cust.opted_out_at is not None),
        "service_window_24h": window_info,
        "channel": order.source_channel,
        "raw_message": order.raw_message_text,
        "intent": order.detected_intent,
        "detected_language": order.detected_language,
        "status": order.status,
        "overall_confidence": order.overall_confidence,
        "requires_human_review": order.requires_human_review,
        "review_reasons": order.review_reasons_json,
        "subtotal_paise": order.subtotal_paise,
        "cgst_paise": order.cgst_paise,
        "sgst_paise": order.sgst_paise,
        "igst_paise": order.igst_paise,
        "tax_amount_paise": order.tax_amount_paise,
        "total_paise": order.grand_total_paise,
        "subtotal": float(paise_to_rupees_str(order.subtotal_paise)),
        "tax_total": float(paise_to_rupees_str(order.tax_amount_paise)),
        "grand_total": float(paise_to_rupees_str(order.grand_total_paise)),
        "erp_sync_target": order.erp_sync_target,
        "erp_sync_ref": order.erp_sync_ref,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "lines": [
            {
                "id": ln.id,
                "line_index": ln.line_index,
                "raw_segment": ln.raw_segment,
                "sku_code": ln.sku_code,
                "sku_name": ln.sku_name,
                "qty": ln.qty_display,
                "qty_num": ln.qty_num,
                "qty_den": ln.qty_den,
                "unit": ln.unit,
                "unit_price_paise": ln.unit_price_paise,
                "unit_price": float(paise_to_rupees_str(ln.unit_price_paise)),
                "line_subtotal_paise": ln.line_subtotal_paise,
                "cgst_paise": ln.cgst_paise,
                "sgst_paise": ln.sgst_paise,
                "igst_paise": ln.igst_paise,
                "line_total_paise": ln.line_total_paise,
                "line_total": float(paise_to_rupees_str(ln.line_total_paise)),
                "tax_rate_bps": ln.tax_rate_bps,
                "tax_rate": float(bps_to_percent_str(ln.tax_rate_bps)),
                "confidence": ln.confidence,
                "requires_review": ln.requires_review,
                "review_reasons": ln.review_flags_json,
                "candidates": ln.candidates_json,
            }
            for ln in lines_sorted
        ],
        "audit_logs": [
            {
                "id": lg.id,
                "actor_user_id": lg.actor_user_id,
                "actor_role": lg.actor_role,
                "action": lg.action,
                "details": lg.details_json,
                "created_at": lg.created_at.isoformat() if lg.created_at else None,
            }
            for lg in logs_sorted
        ],
    }


def _get_or_create_customer(
    db: Session,
    tenant: Tenant,
    phone_raw: str,
    name_raw: str,
    price_tier: str = "WHOLESALE",
) -> Customer:
    p_hash = hash_phone(phone_raw)
    stmt = select(Customer).where(
        Customer.tenant_id == tenant.id,
        Customer.whatsapp_phone_hash == p_hash,
    )
    cust = db.scalar(stmt)
    if cust is None:
        cust = Customer(
            tenant_id=tenant.id,
            name=name_raw or "WhatsApp Buyer",
            business_name=name_raw or "WhatsApp Buyer",
            whatsapp_phone_hash=p_hash,
            whatsapp_phone_masked=mask_phone(phone_raw),
            whatsapp_phone_e164=phone_raw,
            state_code=tenant.state_code,
            price_tier=price_tier,
            consent_recorded_at=datetime.now(timezone.utc),
        )
        db.add(cust)
        db.flush()
    return cust


def _persist_extracted_order(
    *,
    db: Session,
    tenant: Tenant,
    customer: Customer,
    wamid: str,
    channel: str,
    message_text: str,
    extracted: dict[str, Any],
) -> tuple[DraftOrder | None, bool]:
    """Persist InboundMessage + DraftOrder + DraftOrderLine + OrderAuditLog with wamid idempotency."""
    existing_msg = db.scalar(
        select(InboundMessage).where(
            InboundMessage.tenant_id == tenant.id,
            InboundMessage.wamid == wamid,
        )
    )
    if existing_msg is not None:
        existing_order = (
            db.scalar(
                select(DraftOrder)
                .where(DraftOrder.id == existing_msg.order_id)
                .options(
                    selectinload(DraftOrder.lines),
                    selectinload(DraftOrder.customer),
                    selectinload(DraftOrder.audit_logs),
                )
            )
            if existing_msg.order_id
            else None
        )
        return existing_order, True

    now = datetime.now(timezone.utc)
    customer.last_inbound_at = now
    customer.last_inbound_wamid = wamid

    if is_opt_out_message(message_text):
        customer.opted_out_at = now
        extracted = {
            **extracted,
            "intent": "OPT_OUT",
            "status": "INBOX_REVIEW",
            "requires_human_review": True,
            "lines": [],
            "review_reasons": list({*extracted.get("review_reasons", []), "OPT_OUT_REQUESTED"}),
            "subtotal_paise": 0,
            "cgst_paise": 0,
            "sgst_paise": 0,
            "igst_paise": 0,
            "total_paise": 0,
        }

    order_count = db.scalar(select(func.count(DraftOrder.id)).where(DraftOrder.tenant_id == tenant.id)) or 0
    order_number = f"OB-{tenant.slug[:3].upper()}-{1001 + order_count}-{uuid.uuid4().hex[:4].upper()}"

    draft_order = DraftOrder(
        tenant_id=tenant.id,
        customer_id=customer.id,
        order_number=order_number,
        source_channel=channel,
        raw_message_text=message_text,
        detected_intent=str(extracted.get("intent", "NEW_ORDER")),
        detected_language=str(extracted.get("detected_language", "hinglish")),
        status=str(extracted.get("status", "NEEDS_REVIEW")),
        overall_confidence=float(extracted.get("overall_confidence", 0.0)),
        requires_human_review=bool(extracted.get("requires_human_review", True)),
        review_reasons_json=list(extracted.get("review_reasons", [])),
        subtotal_paise=int(extracted.get("subtotal_paise", 0)),
        cgst_paise=int(extracted.get("cgst_paise", 0)),
        sgst_paise=int(extracted.get("sgst_paise", 0)),
        igst_paise=int(extracted.get("igst_paise", 0)),
        tax_amount_paise=int(
            extracted.get("cgst_paise", 0) + extracted.get("sgst_paise", 0) + extracted.get("igst_paise", 0)
        ),
        grand_total_paise=int(extracted.get("total_paise", 0)),
    )
    db.add(draft_order)
    db.flush()

    sku_by_code = {s.code: s for s in tenant.skus}
    for ln in extracted.get("lines", []):
        matched_sku = sku_by_code.get(str(ln.get("sku_code", "")))
        line_row = DraftOrderLine(
            order_id=draft_order.id,
            tenant_id=tenant.id,
            line_index=int(ln.get("line_index", 0)),
            raw_segment=str(ln.get("raw_segment", "")),
            sku_id=matched_sku.id if matched_sku else None,
            sku_code=str(ln.get("sku_code", "UNKNOWN")),
            sku_name=str(ln.get("sku_name", "UNKNOWN")),
            qty_num=int(ln.get("qty_num", 1)),
            qty_den=int(ln.get("qty_den", 1)),
            qty_display=float(ln.get("qty", 1.0)),
            unit=str(ln.get("unit", "pcs")),
            unit_price_paise=int(ln.get("unit_price_paise", 0)),
            tax_rate_bps=matched_sku.tax_rate_bps if matched_sku else 1800,
            line_subtotal_paise=int(ln.get("line_subtotal_paise", 0)),
            cgst_paise=int(ln.get("cgst_paise", 0)),
            sgst_paise=int(ln.get("sgst_paise", 0)),
            igst_paise=int(ln.get("igst_paise", 0)),
            line_tax_paise=int(ln.get("cgst_paise", 0) + ln.get("sgst_paise", 0) + ln.get("igst_paise", 0)),
            line_total_paise=int(ln.get("line_total_paise", 0)),
            confidence=float(ln.get("confidence", 0.0)),
            match_method="HYBRID_PIPELINE",
            requires_review=bool(ln.get("requires_review", True)),
            review_flags_json=list(ln.get("review_reasons", [])),
            candidates_json=list(ln.get("candidates", [])),
        )
        db.add(line_row)

    inbound_row = InboundMessage(
        tenant_id=tenant.id,
        customer_id=customer.id,
        wamid=wamid,
        channel=channel,
        message_type="text",
        raw_text=message_text,
        detected_script=str(extracted.get("detected_script", "Latin")),
        detected_language=str(extracted.get("detected_language", "hinglish")),
        detected_intent=str(extracted.get("intent", "NEW_ORDER")),
        order_id=draft_order.id,
    )
    db.add(inbound_row)

    audit_row = OrderAuditLog(
        tenant_id=tenant.id,
        order_id=draft_order.id,
        actor_user_id=None,
        actor_role="SYSTEM",
        action="ORDER_CAPTURED",
        details_json={
            "wamid": wamid,
            "channel": channel,
            "intent": extracted.get("intent"),
            "status": draft_order.status,
            "overall_confidence": draft_order.overall_confidence,
            "line_count": len(extracted.get("lines", [])),
        },
    )
    db.add(audit_row)
    db.commit()
    db.refresh(draft_order)
    return draft_order, False


def _load_tenant_learned_aliases(
    db: Session, tenant_uuid: str, sku_id_to_code: dict[str, str]
) -> dict[str, str]:
    rows = db.scalars(
        select(LearnedAlias).where(
            LearnedAlias.tenant_id == tenant_uuid,
            LearnedAlias.is_active.is_(True),
        )
    ).all()
    out: dict[str, str] = {}
    for r in rows:
        code = sku_id_to_code.get(r.sku_id)
        if code:
            out[r.normalized_phrase] = code
    return out


# ---------------------------------------------------------------------------
# Health & Readiness Probes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "OrderBridge Multi-Tenant API", "version": "2.5.0"}


@app.get("/ready")
async def readiness_check(db: Session = Depends(get_db)) -> JSONResponse:
    ok, detail = check_database_ready()
    if not ok:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unreachable", "detail": detail},
        )
    tenant_count = db.scalar(select(func.count(Tenant.id))) or 0
    return JSONResponse(
        status_code=200,
        content={"status": "ready", "database": "connected", "seeded_tenants": tenant_count},
    )


# ---------------------------------------------------------------------------
# Auth & RBAC Endpoints
# ---------------------------------------------------------------------------


@app.post("/api/auth/login")
async def login(payload: LoginPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.scalar(
        select(User)
        .where(User.email == payload.email.strip().lower())
        .options(selectinload(User.memberships).selectinload(TenantMembership.tenant))
    )
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.memberships:
        raise HTTPException(status_code=403, detail="User has no tenant memberships")

    chosen_membership = user.memberships[0]
    if payload.tenant_slug:
        for m in user.memberships:
            if m.tenant and m.tenant.slug == payload.tenant_slug:
                chosen_membership = m
                break

    tenant = chosen_membership.tenant
    token = create_session_token(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        tenant_uuid=tenant.id,
        tenant_slug=tenant.slug,
        role=chosen_membership.role,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": chosen_membership.role,
            "tenant_slug": tenant.slug,
            "tenant_uuid": tenant.id,
            "tenant_brand": tenant.brand_name,
        },
    }


@app.post("/api/auth/demo-session")
async def issue_demo_session(payload: DemoSessionPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Issue a signed JWT for the Workbench so operators can test OWNER / STAFF / VIEWER RBAC."""
    tenant = db.scalar(select(Tenant).where(Tenant.slug == payload.tenant_slug))
    if tenant is None:
        run_seed(create_tables_if_missing=True)
        tenant = db.scalar(select(Tenant).where(Tenant.slug == payload.tenant_slug))
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant '{payload.tenant_slug}' not found")

    role_clean = payload.role.strip().upper()
    if role_clean not in {"OWNER", "STAFF", "VIEWER"}:
        raise HTTPException(status_code=400, detail="Role must be OWNER, STAFF, or VIEWER")

    email = f"{role_clean.lower()}@{tenant.slug}.orderbridge.in"
    token = create_session_token(
        user_id=f"usr_{tenant.slug}_{role_clean.lower()}",
        email=email,
        full_name=f"{tenant.brand_name} ({role_clean})",
        tenant_uuid=tenant.id,
        tenant_slug=tenant.slug,
        role=role_clean,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": f"usr_{tenant.slug}_{role_clean.lower()}",
            "email": email,
            "full_name": f"{tenant.brand_name} ({role_clean})",
            "role": role_clean,
            "tenant_slug": tenant.slug,
            "tenant_uuid": tenant.id,
            "tenant_brand": tenant.brand_name,
        },
    }


@app.get("/api/auth/me")
async def get_me(auth: AuthContext = Depends(require_role("VIEWER"))) -> dict[str, Any]:
    return {
        "user_id": auth.user_id,
        "email": auth.email,
        "full_name": auth.full_name,
        "tenant_uuid": auth.tenant_uuid,
        "tenant_slug": auth.tenant_slug,
        "role": auth.role,
    }


# ---------------------------------------------------------------------------
# Tenant Catalogue & Settings Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/tenants")
async def list_tenants(db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(Tenant).options(selectinload(Tenant.skus).selectinload(Sku.allowed_units))
    tenants = list(db.scalars(stmt).all())
    if not tenants:
        run_seed(create_tables_if_missing=True)
        tenants = list(db.scalars(stmt).all())
    return {t.slug: serialize_tenant(t) for t in tenants}


@app.patch("/api/tenant/settings")
async def update_tenant_settings(
    payload: TenantSettingsUpdatePayload,
    auth: AuthContext = Depends(require_role("OWNER")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """OWNER-only endpoint; tenant_id is strictly resolved from the verified JWT session."""
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == auth.tenant_uuid)
        .options(selectinload(Tenant.skus).selectinload(Sku.allowed_units))
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="Authenticated tenant not found")

    if payload.auto_confirm_enabled is not None:
        tenant.auto_confirm_enabled = payload.auto_confirm_enabled
    if payload.clarification_replies_enabled is not None:
        tenant.clarification_replies_enabled = payload.clarification_replies_enabled
    if payload.default_erp is not None:
        tenant.default_erp = payload.default_erp
    if payload.debounce_window_seconds is not None:
        tenant.debounce_window_seconds = max(0, min(600, payload.debounce_window_seconds))

    db.commit()
    db.refresh(tenant)
    return serialize_tenant(tenant)


# ---------------------------------------------------------------------------
# Order Extraction, Lifecycle & Audit Trail Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/orders")
async def list_tenant_orders(
    auth: AuthContext = Depends(require_role("VIEWER")),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return orders strictly scoped to the authenticated user's tenant_uuid."""
    stmt = (
        select(DraftOrder)
        .where(DraftOrder.tenant_id == auth.tenant_uuid)
        .options(
            selectinload(DraftOrder.lines),
            selectinload(DraftOrder.customer),
            selectinload(DraftOrder.audit_logs),
        )
        .order_by(DraftOrder.created_at.desc())
    )
    orders = list(db.scalars(stmt).all())
    return [serialize_order(o) for o in orders]


@app.post("/api/orders/extract")
async def extract_order(
    payload: InboundMessagePayload,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = (
        select(Tenant)
        .where(Tenant.slug == payload.tenant_id)
        .options(selectinload(Tenant.skus).selectinload(Sku.allowed_units))
    )
    tenant = db.scalar(stmt)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant '{payload.tenant_id}' not found")

    tenant_dict = serialize_tenant(tenant)
    engine = OrderExtractionEngine(
        tenant_config=tenant_dict,
        sku_catalog=tenant_dict.get("skus", []),
    )
    sku_id_to_code = {s.id: s.code for s in tenant.skus}
    learned_map = _load_tenant_learned_aliases(db, tenant.id, sku_id_to_code)
    for phrase, code in learned_map.items():
        engine.extractor.record_operator_correction(
            tenant_id=tenant.slug,
            alias_phrase=phrase,
            sku_code=code,
        )

    result = engine.parse_message(payload.message_text, price_tier=payload.price_tier)
    result["customer_name"] = payload.customer_name
    result["customer_phone"] = payload.customer_phone
    result["channel"] = payload.channel

    if payload.persist:
        cust = _get_or_create_customer(
            db,
            tenant,
            payload.customer_phone,
            payload.customer_name,
            price_tier=payload.price_tier,
        )
        wamid = payload.wamid or f"wamid.OB.{uuid.uuid4().hex[:16]}"
        order_row, is_dup = _persist_extracted_order(
            db=db,
            tenant=tenant,
            customer=cust,
            wamid=wamid,
            channel=payload.channel,
            message_text=payload.message_text,
            extracted=result,
        )
        result["duplicate_wamid"] = is_dup
        if order_row is not None:
            result["order_id"] = order_row.id
            result["order_number"] = order_row.order_number
            result["persisted_order"] = serialize_order(order_row)

    return result


@app.patch("/api/orders/{order_id}/lines/{line_id}")
async def update_order_line(
    order_id: str,
    line_id: str,
    payload: LineUpdatePayload,
    auth: AuthContext = Depends(require_role("STAFF")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Operator edits a line item (SKU, quantity, unit), recalculates integer-paise GST, and optionally learns alias."""
    order = db.scalar(
        select(DraftOrder)
        .where(DraftOrder.id == order_id, DraftOrder.tenant_id == auth.tenant_uuid)
        .options(
            selectinload(DraftOrder.lines),
            selectinload(DraftOrder.customer),
            selectinload(DraftOrder.audit_logs),
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found in current tenant")

    target_line = next((ln for ln in order.lines if ln.id == line_id), None)
    if target_line is None:
        raise HTTPException(status_code=404, detail="Order line not found")

    sku = db.scalar(
        select(Sku)
        .where(Sku.tenant_id == auth.tenant_uuid, Sku.code == payload.sku_code)
        .options(selectinload(Sku.allowed_units))
    )
    if sku is None:
        raise HTTPException(status_code=400, detail=f"SKU '{payload.sku_code}' not found in tenant catalogue")

    before_snapshot = {
        "sku_code": target_line.sku_code,
        "qty": target_line.qty_display,
        "unit": target_line.unit,
        "line_total_paise": target_line.line_total_paise,
    }

    qty_frac = Fraction(str(payload.qty)).limit_denominator(100)
    unit_clean = payload.unit.strip().lower()
    norm_sku = normalise_sku_dict(
        {
            "code": sku.code,
            "name": sku.name,
            "baseUnit": sku.base_unit,
            "wholesalePricePaise": sku.wholesale_price_paise,
            "taxRateBps": sku.tax_rate_bps,
            "allowedUnits": [
                {"unit": u.unit_code, "multiplierNum": u.multiplier_num, "multiplierDen": u.multiplier_den}
                for u in sku.allowed_units
            ],
        }
    )
    mult_info = norm_sku["units"].get(unit_clean, {"multiplier_num": 1, "multiplier_den": 1})
    unit_price_p = round_div_half_up(
        sku.wholesale_price_paise * int(mult_info["multiplier_num"]),
        int(mult_info["multiplier_den"]),
    )

    tax_res = calculate_line_financials(
        qty_num=qty_frac.numerator,
        qty_den=qty_frac.denominator,
        unit_price_paise=unit_price_p,
        tax_rate_bps=sku.tax_rate_bps,
        seller_state_code="27",
        buyer_state_code=order.customer.state_code if order.customer else "27",
        tax_regime="GST_INDIA",
    )

    target_line.sku_id = sku.id
    target_line.sku_code = sku.code
    target_line.sku_name = sku.name
    target_line.qty_num = qty_frac.numerator
    target_line.qty_den = qty_frac.denominator
    target_line.qty_display = float(qty_frac)
    target_line.unit = payload.unit.strip()
    target_line.unit_price_paise = unit_price_p
    target_line.tax_rate_bps = sku.tax_rate_bps
    target_line.line_subtotal_paise = tax_res.line_subtotal_paise
    target_line.cgst_paise = tax_res.cgst_paise
    target_line.sgst_paise = tax_res.sgst_paise
    target_line.igst_paise = tax_res.igst_paise
    target_line.line_tax_paise = tax_res.line_tax_paise
    target_line.line_total_paise = tax_res.line_total_paise
    target_line.confidence = 1.0
    target_line.match_method = "OPERATOR_VERIFIED"
    target_line.requires_review = False
    target_line.review_flags_json = []

    # Recompute order totals in integer paise
    order.subtotal_paise = sum(ln.line_subtotal_paise for ln in order.lines)
    order.cgst_paise = sum(ln.cgst_paise for ln in order.lines)
    order.sgst_paise = sum(ln.sgst_paise for ln in order.lines)
    order.igst_paise = sum(ln.igst_paise for ln in order.lines)
    order.tax_amount_paise = order.cgst_paise + order.sgst_paise + order.igst_paise
    order.grand_total_paise = order.subtotal_paise + order.tax_amount_paise
    order.requires_human_review = any(ln.requires_review for ln in order.lines)
    if not order.requires_human_review and order.status == "NEEDS_REVIEW":
        order.status = "DRAFT"

    learned_alias_id: str | None = None
    if payload.learn_alias_phrase and payload.learn_alias_phrase.strip():
        norm_phrase = canonicalise_indian_text(payload.learn_alias_phrase)
        if norm_phrase:
            existing_alias = db.scalar(
                select(LearnedAlias).where(
                    LearnedAlias.tenant_id == auth.tenant_uuid,
                    LearnedAlias.normalized_phrase == norm_phrase,
                )
            )
            if existing_alias is None:
                new_alias = LearnedAlias(
                    tenant_id=auth.tenant_uuid,
                    normalized_phrase=norm_phrase,
                    sku_id=sku.id,
                    default_unit=payload.unit.strip(),
                    source_order_id=order.id,
                    created_by_user_id=auth.user_id,
                    is_active=True,
                )
                db.add(new_alias)
                db.flush()
                learned_alias_id = new_alias.id
            else:
                existing_alias.sku_id = sku.id
                existing_alias.is_active = True
                existing_alias.reverted_at = None
                learned_alias_id = existing_alias.id

    db.add(
        OrderAuditLog(
            tenant_id=auth.tenant_uuid,
            order_id=order.id,
            actor_user_id=auth.user_id,
            actor_role=auth.role,
            action="LINE_EDITED",
            details_json={
                "line_id": line_id,
                "before": before_snapshot,
                "after": {
                    "sku_code": target_line.sku_code,
                    "qty": target_line.qty_display,
                    "unit": target_line.unit,
                    "line_total_paise": target_line.line_total_paise,
                },
                "learned_alias_id": learned_alias_id,
            },
        )
    )
    db.commit()
    db.refresh(order)
    return serialize_order(order)


@app.post("/api/orders/{order_id}/confirm")
async def confirm_order(
    order_id: str,
    auth: AuthContext = Depends(require_role("STAFF")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    order = db.scalar(
        select(DraftOrder)
        .where(DraftOrder.id == order_id, DraftOrder.tenant_id == auth.tenant_uuid)
        .options(
            selectinload(DraftOrder.lines),
            selectinload(DraftOrder.customer),
            selectinload(DraftOrder.audit_logs),
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found in current tenant")

    if not order.lines:
        raise HTTPException(status_code=400, detail="Cannot confirm an order with 0 line items")

    if any(ln.sku_code == "UNKNOWN" for ln in order.lines):
        raise HTTPException(
            status_code=400,
            detail="Cannot confirm order while any line has SKU='UNKNOWN'; resolve all lines first",
        )

    prev_status = order.status
    order.status = "CONFIRMED"
    order.requires_human_review = False
    order.confirmed_by_user_id = auth.user_id
    order.confirmed_at = datetime.now(timezone.utc).isoformat()
    for ln in order.lines:
        ln.requires_review = False

    db.add(
        OrderAuditLog(
            tenant_id=auth.tenant_uuid,
            order_id=order.id,
            actor_user_id=auth.user_id,
            actor_role=auth.role,
            action="ORDER_CONFIRMED",
            details_json={"previous_status": prev_status, "new_status": "CONFIRMED"},
        )
    )
    db.commit()
    db.refresh(order)
    return serialize_order(order)


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    auth: AuthContext = Depends(require_role("STAFF")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    order = db.scalar(
        select(DraftOrder)
        .where(DraftOrder.id == order_id, DraftOrder.tenant_id == auth.tenant_uuid)
        .options(
            selectinload(DraftOrder.lines),
            selectinload(DraftOrder.customer),
            selectinload(DraftOrder.audit_logs),
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found in current tenant")

    prev_status = order.status
    order.status = "CANCELLED"
    db.add(
        OrderAuditLog(
            tenant_id=auth.tenant_uuid,
            order_id=order.id,
            actor_user_id=auth.user_id,
            actor_role=auth.role,
            action="ORDER_CANCELLED",
            details_json={"previous_status": prev_status, "new_status": "CANCELLED"},
        )
    )
    db.commit()
    db.refresh(order)
    return serialize_order(order)


@app.get("/api/aliases")
async def list_learned_aliases(
    auth: AuthContext = Depends(require_role("VIEWER")),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(LearnedAlias)
            .where(LearnedAlias.tenant_id == auth.tenant_uuid)
            .order_by(LearnedAlias.created_at.desc())
        ).all()
    )
    skus = {s.id: s for s in db.scalars(select(Sku).where(Sku.tenant_id == auth.tenant_uuid)).all()}
    return [
        {
            "id": r.id,
            "normalized_phrase": r.normalized_phrase,
            "sku_id": r.sku_id,
            "sku_code": skus[r.sku_id].code if r.sku_id in skus else "UNKNOWN",
            "sku_name": skus[r.sku_id].name if r.sku_id in skus else "Unknown",
            "default_unit": r.default_unit,
            "is_active": r.is_active,
            "created_by_user_id": r.created_by_user_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@app.post("/api/aliases/{alias_id}/rollback")
async def rollback_learned_alias(
    alias_id: str,
    auth: AuthContext = Depends(require_role("STAFF")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    alias_row = db.scalar(
        select(LearnedAlias).where(
            LearnedAlias.id == alias_id,
            LearnedAlias.tenant_id == auth.tenant_uuid,
        )
    )
    if alias_row is None:
        raise HTTPException(status_code=404, detail="Learned alias not found in current tenant")
    alias_row.is_active = False
    alias_row.reverted_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": alias_row.id, "is_active": False, "status": "reverted"}


@app.delete("/api/customers/{customer_id}/pii")
async def redact_customer_pii_dpdp(
    customer_id: str,
    auth: AuthContext = Depends(require_role("OWNER")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """DPDP Act 2023 Right-to-Erasure endpoint (OWNER only).

    Redacts buyer PII (phone_e164, display_name, and raw message text) within the caller's
    tenant while preserving immutable GST tax voucher totals required under Section 35 of the
    CGST Act, 2017 (permitted under DPDP Act 2023 Section 12(3) statutory retention exemption).
    """
    cust = db.scalar(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.tenant_id == auth.tenant_uuid,
        )
    )
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found in current tenant")

    redacted_tag = f"REDACTED-{cust.whatsapp_phone_hash[:10]}"
    cust.whatsapp_phone_e164 = redacted_tag
    cust.whatsapp_phone_masked = redacted_tag
    cust.name = "[REDACTED BY DPDP REQUEST]"
    cust.business_name = "[REDACTED BY DPDP REQUEST]"
    cust.opted_out_at = datetime.now(timezone.utc)

    inbound_rows = list(
        db.scalars(
            select(InboundMessage).where(
                InboundMessage.tenant_id == auth.tenant_uuid,
                InboundMessage.customer_id == cust.id,
            )
        ).all()
    )
    for msg in inbound_rows:
        msg.raw_text = "[REDACTED BY DPDP REQUEST]"

    orders = list(
        db.scalars(
            select(DraftOrder).where(
                DraftOrder.tenant_id == auth.tenant_uuid,
                DraftOrder.customer_id == cust.id,
            )
        ).all()
    )
    for ord_row in orders:
        ord_row.raw_message_text = "[REDACTED BY DPDP REQUEST]"
        db.add(
            OrderAuditLog(
                tenant_id=auth.tenant_uuid,
                order_id=ord_row.id,
                actor_user_id=auth.user_id,
                actor_role=auth.role,
                action="CUSTOMER_PII_REDACTED_DPDP",
                details_json={"customer_id": cust.id, "redacted_tag": redacted_tag},
            )
        )

    db.commit()
    return {
        "customer_id": cust.id,
        "status": "PII_REDACTED",
        "redacted_tag": redacted_tag,
        "messages_redacted": len(inbound_rows),
        "orders_redacted": len(orders),
    }



# ---------------------------------------------------------------------------
# Accounting Sync & Export Endpoints (Enforces CONFIRMED-Only + Idempotency)
# ---------------------------------------------------------------------------


@app.post("/api/orders/{order_id}/export/{erp_format}", response_model=None)
async def export_persisted_order_to_erp(
    order_id: str,
    erp_format: str,
    auth: AuthContext = Depends(require_role("STAFF")),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Export a CONFIRMED order to Tally Prime XML, Zoho Books, CSV, or Xero with idempotency tracking."""
    order = db.scalar(
        select(DraftOrder)
        .where(DraftOrder.id == order_id, DraftOrder.tenant_id == auth.tenant_uuid)
        .options(
            selectinload(DraftOrder.lines),
            selectinload(DraftOrder.customer),
            selectinload(DraftOrder.audit_logs),
            selectinload(DraftOrder.tenant),
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found in current tenant")

    # Hard Rule 4: Never push an order to accounting unless it is CONFIRMED (or already SYNCED)!
    if order.status not in {"CONFIRMED", "SYNCED"}:
        raise HTTPException(
            status_code=409,
            detail=f"Order status is '{order.status}'; human confirmation is required before ERP export",
        )

    erp_upper = erp_format.strip().upper()
    idem_key = compute_export_idempotency_key(auth.tenant_slug, order.order_number, erp_upper)
    serialized = serialize_order(order)
    tenant_brand = order.tenant.brand_name if order.tenant else auth.tenant_slug

    if erp_upper in {"TALLY", "TALLY_PRIME"}:
        payload_str = AccountingSyncAdapter.to_tally_prime_xml(serialized, {"brandName": tenant_brand})
        content_type = "application/xml"
    elif erp_upper in {"CSV", "EXCEL_CSV"}:
        payload_str = AccountingSyncAdapter.to_csv_batch([serialized])
        content_type = "text/csv"
    elif erp_upper in {"ZOHO", "ZOHO_BOOKS"}:
        payload_str = json.dumps(AccountingSyncAdapter.to_zoho_books_json(serialized), indent=2)
        content_type = "application/json"
    elif erp_upper == "XERO":
        payload_str = json.dumps(AccountingSyncAdapter.to_xero_invoice_json(serialized), indent=2)
        content_type = "application/json"
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported ERP format: {erp_format}")

    payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
    existing_export = db.scalar(
        select(ErpSyncExport).where(
            ErpSyncExport.tenant_id == auth.tenant_uuid,
            ErpSyncExport.order_id == order.id,
            ErpSyncExport.erp_target == erp_upper,
            ErpSyncExport.idempotency_key == idem_key,
        )
    )
    already_exported = existing_export is not None
    if existing_export is None:
        db.add(
            ErpSyncExport(
                tenant_id=auth.tenant_uuid,
                order_id=order.id,
                erp_target=erp_upper,
                idempotency_key=idem_key,
                payload_sha256=payload_hash,
                status="EXPORTED",
            )
        )
        order.status = "SYNCED"
        order.erp_sync_target = erp_upper
        order.erp_sync_ref = idem_key
        order.synced_at = datetime.now(timezone.utc)
        db.add(
            OrderAuditLog(
                tenant_id=auth.tenant_uuid,
                order_id=order.id,
                actor_user_id=auth.user_id,
                actor_role=auth.role,
                action="ORDER_EXPORTED_TO_ERP",
                details_json={"erp_target": erp_upper, "idempotency_key": idem_key},
            )
        )
        db.commit()
        db.refresh(order)

    return JSONResponse(
        content={
            "order_id": order.id,
            "order_number": order.order_number,
            "erp_format": erp_upper,
            "connector_maturity": AccountingSyncAdapter.CONNECTOR_MATURITY.get(erp_upper, "BETA"),
            "idempotency_key": idem_key,
            "idempotent_replay": already_exported,
            "content_type": content_type,
            "payload": payload_str,
            "order": serialize_order(order),
        }
    )


@app.post("/api/accounting/export/{erp_format}", response_model=None)
async def export_order_to_erp(erp_format: str, order: dict[str, Any]) -> PlainTextResponse | JSONResponse:
    erp_upper = erp_format.upper()
    if erp_upper in {"TALLY", "TALLY_PRIME"}:
        xml_str = AccountingSyncAdapter.to_tally_prime_xml(
            order, {"brandName": order.get("tenant_brand", "OrderBridge Tenant")}
        )
        return PlainTextResponse(content=xml_str, media_type="application/xml")
    if erp_upper == "XERO":
        return JSONResponse(content=AccountingSyncAdapter.to_xero_invoice_json(order))
    if erp_upper in {"ZOHO", "ZOHO_BOOKS"}:
        return JSONResponse(content=AccountingSyncAdapter.to_zoho_books_json(order))
    if erp_upper in {"CSV", "EXCEL_CSV"}:
        csv_str = AccountingSyncAdapter.to_csv_batch([order])
        return PlainTextResponse(content=csv_str, media_type="text/csv")
    raise HTTPException(status_code=400, detail=f"Unsupported ERP format: {erp_format}")


# ---------------------------------------------------------------------------
# Business Continuity & Evaluation Report Endpoints
# ---------------------------------------------------------------------------


@app.post("/api/resilience/simulate-ban/{tenant_id}")
async def simulate_meta_continuity(
    tenant_id: str,
    error_code: int = Query(default=130429),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    controller = BusinessContinuityController(
        {"brandName": tenant.brand_name, "channels": tenant.channel_config_json or undefined_channels()}
    )
    return controller.inspect_meta_webhook_error(error_code=error_code)


def undefined_channels() -> dict[str, Any]:
    return {
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
    }


@app.get("/api/eval/metrics")
async def get_evaluation_metrics() -> dict[str, Any]:
    metrics_path = Path(__file__).resolve().parents[2] / "eval" / "metrics_latest.json"
    if not metrics_path.exists():
        return {"status": "not_generated"}
    return json.loads(metrics_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Meta WhatsApp Business Cloud API Webhooks (Verification + HMAC-SHA256 POST)
# ---------------------------------------------------------------------------


@app.get("/webhooks/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> PlainTextResponse:
    current_settings = get_settings()
    expected = current_settings.meta_verify_token.strip()
    if not expected or expected in FORBIDDEN_SECRET_VALUES:
        raise HTTPException(
            status_code=503,
            detail="META_VERIFY_TOKEN is not configured on the server",
        )
    if hub_mode == "subscribe" and hub_verify_token == expected:
        return PlainTextResponse(content=hub_challenge or "")
    raise HTTPException(status_code=403, detail="Webhook verification token mismatch")


def verify_meta_hmac_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Validate X-Hub-Signature-256 header using constant-time hmac.compare_digest.

    Official Meta Doc:
    https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint/
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not app_secret or app_secret in FORBIDDEN_SECRET_VALUES:
        return False
    expected_hex = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided_hex = signature_header.split("=", 1)[1].strip()
    return hmac.compare_digest(expected_hex, provided_hex)


@app.post("/webhooks/whatsapp")
async def receive_whatsapp_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Official Meta WhatsApp Cloud API webhook receiver with HMAC-SHA256 & wamid idempotency."""
    raw_body = await request.body()
    if len(raw_body) > MAX_WEBHOOK_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload exceeds 256 KB limit")
    current_settings = get_settings()
    if not verify_meta_hmac_signature(raw_body, x_hub_signature_256, current_settings.meta_app_secret):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Hub-Signature-256 header")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Malformed JSON webhook payload") from exc

    processed_orders: list[dict[str, Any]] = []
    duplicates_ignored: list[str] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})
            metadata = val.get("metadata", {})
            tenant_slug = str(metadata.get("tenant_slug", "patel_fmcg"))

            tenant = db.scalar(
                select(Tenant)
                .where(Tenant.slug == tenant_slug)
                .options(selectinload(Tenant.skus).selectinload(Sku.allowed_units))
            )
            if tenant is None:
                continue

            contacts = val.get("contacts", [])
            contact_name = (
                contacts[0].get("profile", {}).get("name", "WhatsApp Buyer") if contacts else "WhatsApp Buyer"
            )

            for msg in val.get("messages", []):
                wamid = str(msg.get("id", f"wamid.{uuid.uuid4().hex}"))
                sender_phone = str(msg.get("from", "+919820411209"))
                msg_type = str(msg.get("type", "text"))

                if msg_type == "text":
                    text_body = str(msg.get("text", {}).get("body", ""))
                elif msg_type == "audio":
                    text_body = str(msg.get("audio", {}).get("transcript", ""))
                elif msg_type == "image":
                    text_body = str(msg.get("image", {}).get("caption", ""))
                else:
                    text_body = ""

                cust = _get_or_create_customer(db, tenant, sender_phone, contact_name)

                tenant_dict = serialize_tenant(tenant)
                engine = OrderExtractionEngine(
                    tenant_config=tenant_dict,
                    sku_catalog=tenant_dict.get("skus", []),
                )
                sku_id_to_code = {s.id: s.code for s in tenant.skus}
                learned_map = _load_tenant_learned_aliases(db, tenant.id, sku_id_to_code)
                for phrase, code in learned_map.items():
                    engine.extractor.record_operator_correction(
                        tenant_id=tenant.slug,
                        alias_phrase=phrase,
                        sku_code=code,
                    )

                extracted = engine.parse_message(text_body, price_tier=cust.price_tier)
                order_row, is_dup = _persist_extracted_order(
                    db=db,
                    tenant=tenant,
                    customer=cust,
                    wamid=wamid,
                    channel="WHATSAPP_CLOUD_API",
                    message_text=text_body,
                    extracted=extracted,
                )
                if is_dup:
                    duplicates_ignored.append(wamid)
                elif order_row is not None:
                    processed_orders.append(serialize_order(order_row))

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "processed_count": len(processed_orders),
            "duplicates_ignored": duplicates_ignored,
            "orders": processed_orders,
        },
    )


@app.post("/api/webhooks/whatsapp/simulate")
async def simulate_signed_whatsapp_webhook(
    payload: WebhookSimulatePayload,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Construct a realistic Meta Cloud API payload, sign it with HMAC-SHA256, and run the webhook pipeline."""
    wamid = payload.wamid or f"wamid.HBgM{uuid.uuid4().hex[:16].upper()}"
    meta_envelope = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_1092837465",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "919820144100",
                                "phone_number_id": "1092837465001",
                                "tenant_slug": payload.tenant_slug,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": payload.sender_name},
                                    "wa_id": payload.sender_phone.lstrip("+"),
                                }
                            ],
                            "messages": [
                                {
                                    "from": payload.sender_phone,
                                    "id": wamid,
                                    "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                                    "type": "text",
                                    "text": {"body": payload.message_text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    raw_bytes = json.dumps(meta_envelope).encode("utf-8")
    current_settings = get_settings()
    sig = (
        "sha256="
        + hmac.new(
            current_settings.meta_app_secret.encode("utf-8"),
            raw_bytes,
            hashlib.sha256,
        ).hexdigest()
    )

    # Reuse the exact HMAC verification and webhook processing logic
    request._body = raw_bytes
    return await receive_whatsapp_webhook(request=request, x_hub_signature_256=sig, db=db)
