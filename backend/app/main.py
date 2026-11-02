"""
FastAPI Multi-Tenant Server for OrderBridge WhatsApp Order Capture Suite.
Handles:
- Health & Readiness Probes (/health, /ready)
- Meta WhatsApp Cloud API Webhooks + Verification
- Multi-Tenant Catalog & Order Extraction APIs backed by SQLAlchemy 2.0
- One-Click Tally Prime XML / Xero / Zoho Books / CSV Sync
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .accounting_sync import AccountingSyncAdapter
from .config import FORBIDDEN_SECRET_VALUES, get_settings
from .db import check_database_ready, get_db, get_engine
from .extractor import OrderExtractionEngine
from .failover import BanResilienceController
from .models import Base, Sku, Tenant
from .money import bps_to_percent_str, paise_to_rupees_str
from .seed import run_seed


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


class InboundMessagePayload(BaseModel):
    tenant_id: str
    customer_name: str
    customer_phone: str
    channel: str = "WHATSAPP_PRIMARY"
    message_text: str
    price_tier: str = "WHOLESALE"


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
        "autoApproveThreshold": tenant.auto_approve_threshold,
        "fuzzyMatchThreshold": tenant.fuzzy_match_threshold,
        "skus": skus_out,
    }


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


@app.get("/api/tenants")
async def list_tenants(db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(Tenant).options(selectinload(Tenant.skus).selectinload(Sku.allowed_units))
    tenants = list(db.scalars(stmt).all())
    if not tenants:
        run_seed(create_tables_if_missing=True)
        tenants = list(db.scalars(stmt).all())
    return {t.slug: serialize_tenant(t) for t in tenants}


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
    result = engine.parse_message(payload.message_text, price_tier=payload.price_tier)
    result["customer_name"] = payload.customer_name
    result["customer_phone"] = payload.customer_phone
    result["channel"] = payload.channel
    return result


@app.post("/api/resilience/simulate-ban/{tenant_id}")
async def simulate_meta_ban(tenant_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    controller = BanResilienceController(
        {"brandName": tenant.brand_name, "channels": tenant.channel_config_json}
    )
    return controller.inspect_meta_webhook_error(error_code=130429)


@app.post("/api/accounting/export/{erp_format}", response_model=None)
async def export_order_to_erp(erp_format: str, order: dict[str, Any]) -> PlainTextResponse | JSONResponse:
    erp_upper = erp_format.upper()
    if erp_upper == "TALLY":
        xml_str = AccountingSyncAdapter.to_tally_prime_xml(
            order, {"brandName": order.get("tenant_brand", "OrderBridge Tenant")}
        )
        return PlainTextResponse(content=xml_str, media_type="application/xml")
    if erp_upper == "XERO":
        return JSONResponse(content=AccountingSyncAdapter.to_xero_invoice_json(order))
    if erp_upper == "ZOHO":
        return JSONResponse(content=AccountingSyncAdapter.to_zoho_books_json(order))
    if erp_upper == "CSV":
        csv_str = AccountingSyncAdapter.to_csv_batch([order])
        return PlainTextResponse(content=csv_str, media_type="text/csv")
    raise HTTPException(status_code=400, detail=f"Unsupported ERP format: {erp_format}")


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
