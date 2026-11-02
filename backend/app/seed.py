"""
Idempotent database seed script for OrderBridge.
Loads config/tenants.json and populates demo tenants, users, memberships,
SKUs, per-SKU unit conversions, and customers.
Can be run safely multiple times: `python -m app.seed`
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_engine, transactional_session
from .models import Base, Customer, Sku, SkuUnit, Tenant, TenantMembership, User
from .money import percent_to_bps, rupees_to_paise

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "tenants.json"


def hash_phone(phone: str) -> str:
    normalized = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) <= 4:
        return "****"
    return f"+{digits[:2]}******{digits[-4:]}"


def hash_demo_password(password: str) -> str:
    salt = "orderbridge_demo_pbkdf2_salt_v1"
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"pbkdf2_sha256$100000${salt}${dk.hex()}"


def seed_from_dict(session: Session, tenants_data: dict[str, Any]) -> dict[str, int]:
    counts = {"tenants": 0, "skus": 0, "sku_units": 0, "customers": 0, "users": 0}

    # Ensure demo users for RBAC (owner, staff, viewer)
    demo_users = [
        ("owner@orderbridge.in", "Rajesh Patel (Owner)", "OWNER"),
        ("staff@orderbridge.in", "Priya Sharma (Order Desk Staff)", "STAFF"),
        ("viewer@orderbridge.in", "Audit Viewer", "VIEWER"),
    ]
    user_objs: list[tuple[User, str]] = []
    for email, full_name, role in demo_users:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                full_name=full_name,
                password_hash=hash_demo_password(f"OrderBridge#{role.capitalize()}2026!"),
                is_active=True,
            )
            session.add(user)
            session.flush()
            counts["users"] += 1
        user_objs.append((user, role))

    for slug, t_cfg in tenants_data.items():
        tenant = session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is None:
            tenant = Tenant(
                id=t_cfg.get("id", slug),
                slug=slug,
                brand_name=t_cfg["brandName"],
                industry_preset=t_cfg.get("industry", "fmcg_distributor"),
                primary_color=t_cfg.get("primaryColor", "#059669"),
                currency_code=t_cfg.get("currencyCode", "INR"),
                currency_symbol=t_cfg.get("currencySymbol", "₹"),
                tax_regime=t_cfg.get("taxRegime", "GST_INDIA"),
                state_code=t_cfg.get("stateCode", "27"),
                gstin=t_cfg.get("gstin"),
                default_erp=t_cfg.get("defaultErp", "TALLY_PRIME"),
                auto_confirm_enabled=bool(t_cfg.get("autoConfirmEnabled", False)),
                auto_approve_threshold=float(t_cfg.get("autoApproveThreshold", 0.92)),
                fuzzy_match_threshold=float(t_cfg.get("fuzzyMatchThreshold", 0.72)),
                ledger_config_json={
                    "sales_ledger": "GST Sales Account",
                    "cgst_ledger": "Output CGST",
                    "sgst_ledger": "Output SGST",
                    "igst_ledger": "Output IGST",
                    "round_off_ledger": "Round Off",
                },
                channel_config_json={
                    "primary_waba": {"phone": "+91 98201 44100", "status": "HEALTHY", "quality": "GREEN"},
                    "standby_waba": {"phone": "+91 98209 88210", "status": "STANDBY", "quality": "GREEN"},
                    "telegram_bot": {"handle": f"@{slug}_order_bot", "status": "ACTIVE"},
                    "magic_link": {"slug": slug, "status": "ACTIVE"},
                },
            )
            session.add(tenant)
            session.flush()
            counts["tenants"] += 1
        else:
            tenant.brand_name = t_cfg["brandName"]
            tenant.primary_color = t_cfg.get("primaryColor", tenant.primary_color)
            tenant.default_erp = t_cfg.get("defaultErp", tenant.default_erp)

        # Link demo users to tenant
        for user, role in user_objs:
            membership = session.scalar(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == tenant.id,
                    TenantMembership.user_id == user.id,
                )
            )
            if membership is None:
                session.add(
                    TenantMembership(
                        tenant_id=tenant.id,
                        user_id=user.id,
                        role=role,
                    )
                )

        # Upsert SKUs & per-SKU allowed units
        for s_cfg in t_cfg.get("skus", []):
            code = s_cfg["code"]
            sku = session.scalar(select(Sku).where(Sku.tenant_id == tenant.id, Sku.code == code))
            base_paise = int(s_cfg.get("basePricePaise") or rupees_to_paise(int(s_cfg.get("basePrice", 100))))
            wh_paise = int(
                s_cfg.get("wholesalePricePaise") or rupees_to_paise(int(s_cfg.get("wholesalePrice", 100)))
            )
            dist_paise = int(s_cfg.get("distributorPricePaise") or wh_paise)
            tax_bps = int(s_cfg.get("taxRateBps") or percent_to_bps(int(s_cfg.get("taxRate", 18))))

            if sku is None:
                sku = Sku(
                    tenant_id=tenant.id,
                    code=code,
                    name=s_cfg["name"],
                    category=s_cfg.get("category", "General"),
                    base_unit=s_cfg.get("baseUnit", "Pcs"),
                    pack_size_label=s_cfg.get("packSizeLabel"),
                    base_price_paise=base_paise,
                    wholesale_price_paise=wh_paise,
                    distributor_price_paise=dist_paise,
                    tax_rate_bps=tax_bps,
                    hsn_code=s_cfg.get("hsnCode"),
                    moq=int(s_cfg.get("moq", 1)),
                    max_order_qty=int(s_cfg.get("maxOrderQty", 500)),
                    aliases_json=list(s_cfg.get("aliases", [])),
                )
                session.add(sku)
                session.flush()
                counts["skus"] += 1
            else:
                sku.name = s_cfg["name"]
                sku.base_unit = s_cfg.get("baseUnit", sku.base_unit)
                sku.base_price_paise = base_paise
                sku.wholesale_price_paise = wh_paise
                sku.distributor_price_paise = dist_paise
                sku.tax_rate_bps = tax_bps
                sku.aliases_json = list(s_cfg.get("aliases", []))

            allowed_units = s_cfg.get(
                "allowedUnits",
                [{"unit": sku.base_unit, "multiplierNum": 1, "multiplierDen": 1, "isBase": True}],
            )
            for u_cfg in allowed_units:
                u_code = u_cfg["unit"]
                existing_unit = session.scalar(
                    select(SkuUnit).where(
                        SkuUnit.tenant_id == tenant.id,
                        SkuUnit.sku_id == sku.id,
                        SkuUnit.unit_code == u_code,
                    )
                )
                if existing_unit is None:
                    session.add(
                        SkuUnit(
                            tenant_id=tenant.id,
                            sku_id=sku.id,
                            unit_code=u_code,
                            multiplier_num=int(u_cfg.get("multiplierNum", 1)),
                            multiplier_den=int(u_cfg.get("multiplierDen", 1)),
                            is_base_unit=bool(u_cfg.get("isBase", False)),
                        )
                    )
                    counts["sku_units"] += 1

        # Upsert demo customers
        for c_cfg in t_cfg.get("customers", []):
            phone = c_cfg["phone"]
            p_hash = hash_phone(phone)
            cust = session.scalar(
                select(Customer).where(
                    Customer.tenant_id == tenant.id,
                    Customer.whatsapp_phone_hash == p_hash,
                )
            )
            if cust is None:
                session.add(
                    Customer(
                        tenant_id=tenant.id,
                        name=c_cfg["name"],
                        whatsapp_phone_hash=p_hash,
                        whatsapp_phone_masked=mask_phone(phone),
                        whatsapp_phone_e164=phone,
                        state_code=c_cfg.get("stateCode", tenant.state_code),
                        price_tier=c_cfg.get("priceTier", "WHOLESALE"),
                        gstin=c_cfg.get("gstin"),
                        consent_recorded_at=datetime.now(timezone.utc),
                        last_inbound_at=datetime.now(timezone.utc),
                    )
                )
                counts["customers"] += 1

    return counts


def run_seed(create_tables_if_missing: bool = False) -> dict[str, int]:
    if create_tables_if_missing:
        Base.metadata.create_all(bind=get_engine())
    tenants_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    with transactional_session() as session:
        return seed_from_dict(session, tenants_data)


if __name__ == "__main__":
    summary = run_seed(create_tables_if_missing=True)
    print(f"Seeded OrderBridge database successfully: {json.dumps(summary)}")
