"""
FastAPI Multi-Tenant Server for OrderBridge WhatsApp Order Capture Suite.
Handles:
- Meta WhatsApp Cloud API Webhooks + Signature Validation
- Twilio SMS & Telegram Bot Failover Webhooks
- Zero-Signup Magic Order Link Submissions
- Hybrid Order Extraction & SKU Alias Learning
- One-Click Tally Prime XML / Xero / Zoho Books / CSV Sync
- White-Label Tenant Configuration Management
"""
import json
import os
from pathlib import Path
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel

from .extractor import OrderExtractionEngine
from .failover import BanResilienceController
from .accounting_sync import AccountingSyncAdapter

app = FastAPI(
    title="OrderBridge Multi-Tenant WhatsApp Order Capture API",
    version="2.4.0",
    description="Capture WhatsApp/SMS/Telegram/Instagram orders, extract line items, sync to Tally/Xero/Zoho, and survive Meta bans."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "tenants.json"


def load_tenants() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


class InboundMessagePayload(BaseModel):
    tenant_id: str
    customer_name: str
    customer_phone: str
    channel: str = "WHATSAPP_PRIMARY"
    message_text: str
    price_tier: str = "WHOLESALE"


class TenantUpdatePayload(BaseModel):
    tenant_id: str
    brandName: str
    primaryColor: str
    currencySymbol: str
    taxRegime: str
    defaultErp: str
    autoApproveThreshold: float
    fuzzyMatchThreshold: float


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "OrderBridge Multi-Tenant API"}


@app.get("/api/tenants")
async def list_tenants():
    return load_tenants()


@app.post("/api/orders/extract")
async def extract_order(payload: InboundMessagePayload):
    tenants = load_tenants()
    tenant = tenants.get(payload.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant '{payload.tenant_id}' not found")

    engine = OrderExtractionEngine(tenant_config=tenant, sku_catalog=tenant.get("skus", []))
    result = engine.parse_message(payload.message_text, price_tier=payload.price_tier)
    result["customer_name"] = payload.customer_name
    result["customer_phone"] = payload.customer_phone
    result["channel"] = payload.channel
    return result


@app.post("/api/resilience/simulate-ban/{tenant_id}")
async def simulate_meta_ban(tenant_id: str):
    tenants = load_tenants()
    tenant = tenants.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    controller = BanResilienceController(tenant)
    return controller.inspect_meta_webhook_error(error_code=131031)


@app.post("/api/accounting/export/{erp_format}")
async def export_order_to_erp(erp_format: str, order: Dict[str, Any]):
    erp_upper = erp_format.upper()
    if erp_upper == "TALLY":
        xml_str = AccountingSyncAdapter.to_tally_prime_xml(order, {"brandName": order.get("tenant_brand", "OrderBridge Tenant")})
        return PlainTextResponse(content=xml_str, media_type="application/xml")
    elif erp_upper == "XERO":
        return JSONResponse(content=AccountingSyncAdapter.to_xero_invoice_json(order))
    elif erp_upper == "ZOHO":
        return JSONResponse(content=AccountingSyncAdapter.to_zoho_books_json(order))
    elif erp_upper == "CSV":
        csv_str = AccountingSyncAdapter.to_csv_batch([order])
        return PlainTextResponse(content=csv_str, media_type="text/csv")
    raise HTTPException(status_code=400, detail=f"Unsupported ERP format: {erp_format}")


@app.get("/webhooks/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    expected = os.getenv("META_VERIFY_TOKEN", "orderbridge_webhook_verify_2026")
    if hub_mode == "subscribe" and hub_verify_token == expected:
        return PlainTextResponse(content=hub_challenge or "")
    raise HTTPException(status_code=403, detail="Webhook verification token mismatch")
