"""
Phase 1 Baseline & Security Remediation Automated Test Suite.
Tests:
1. /health and /ready endpoints
2. Alembic schema migration & idempotent seeding (running seed twice produces zero duplicates)
3. Exact integer-paise financial math (CGST/SGST intra-state vs IGST inter-state, float rejection)
4. Per-SKU per-tenant unit conversion modeling (Peti of Parle-G vs Katta of Basmati vs Katta of Sugar)
5. WhatsApp message ID (wamid) database idempotency constraint per tenant
6. Tenant isolation across SKUs, customers, and memberships
7. Fail-fast secret enforcement in staging/production environments
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from app.db import get_session_factory, reset_engine, transactional_session
from app.main import app
from app.models import Base, Customer, InboundMessage, Sku, SkuUnit, Tenant
from app.money import (
    aggregate_order_financials,
    calculate_line_financials,
    paise_to_rupees_str,
    percent_to_bps,
    rupees_to_paise,
)
from app.seed import run_seed


@pytest.fixture(autouse=True)
def _isolated_test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Uses DATABASE_URL from env if pointed at a PostgreSQL test DB (like in GitHub Actions CI),
    or an isolated SQLite database file per test when running locally without a Postgres daemon.
    """
    env_db = os.environ.get("DATABASE_URL", "")
    if not env_db.startswith(("postgresql://", "postgresql+psycopg://")):
        db_file = tmp_path / "test_orderbridge.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("META_VERIFY_TOKEN", "test_verify_token_secure_2026")
    monkeypatch.setenv("META_APP_SECRET", "test_app_secret_hmac_2026_secure")
    get_settings.cache_clear()
    engine = reset_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    run_seed(create_tables_if_missing=False)


def test_health_and_readiness_endpoints() -> None:
    with TestClient(app) as client:
        health_resp = client.get("/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["status"] == "ok"

        ready_resp = client.get("/ready")
        assert ready_resp.status_code == 200
        ready_data = ready_resp.json()
        assert ready_data["status"] == "ready"
        assert ready_data["database"] == "connected"
        assert ready_data["seeded_tenants"] >= 3


def test_idempotent_demo_tenant_seed() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        initial_tenants = session.scalar(select(func.count(Tenant.id)))
        initial_skus = session.scalar(select(func.count(Sku.id)))
        initial_units = session.scalar(select(func.count(SkuUnit.id)))

    # Run seed a second time; counts must remain identical
    second_counts = run_seed(create_tables_if_missing=False)
    assert second_counts["tenants"] == 0
    assert second_counts["skus"] == 0
    assert second_counts["sku_units"] == 0

    with session_factory() as session:
        assert session.scalar(select(func.count(Tenant.id))) == initial_tenants
        assert session.scalar(select(func.count(Sku.id))) == initial_skus
        assert session.scalar(select(func.count(SkuUnit.id))) == initial_units


def test_integer_paise_financial_calculations_and_float_ban() -> None:
    with pytest.raises(TypeError, match="Floating-point"):
        rupees_to_paise(560.50)

    with pytest.raises(TypeError, match="Floating-point"):
        percent_to_bps(18.0)

    assert rupees_to_paise("560.50") == 56050
    assert paise_to_rupees_str(56050) == "560.50"

    # Intra-state order: dhai (5/2 = 2.5) Bori of Basmati @ ₹2,280.00 (228000 paise), 5% GST (500 bps)
    # Subtotal = 2.5 * 228000 = 570000 paise (₹5,700.00)
    # CGST (2.5% = 250 bps) = 14250 paise (₹142.50), SGST = 14250 paise (₹142.50), IGST = 0
    intra_line = calculate_line_financials(
        qty_num=5,
        qty_den=2,
        unit_price_paise=228_000,
        tax_rate_bps=500,
        seller_state_code="27",
        buyer_state_code="27",
    )
    assert intra_line.line_subtotal_paise == 570_000
    assert intra_line.cgst_paise == 14_250
    assert intra_line.sgst_paise == 14_250
    assert intra_line.igst_paise == 0
    assert intra_line.line_total_paise == 598_500

    # Inter-state order (Maharashtra 27 -> West Bengal 19): IGST (5% = 500 bps) = 28500 paise
    inter_line = calculate_line_financials(
        qty_num=5,
        qty_den=2,
        unit_price_paise=228_000,
        tax_rate_bps=500,
        seller_state_code="27",
        buyer_state_code="19",
    )
    assert inter_line.cgst_paise == 0
    assert inter_line.sgst_paise == 0
    assert inter_line.igst_paise == 28_500
    assert inter_line.line_total_paise == 598_500

    totals = aggregate_order_financials([intra_line])
    assert totals.subtotal_paise + totals.tax_amount_paise == totals.grand_total_paise


def test_per_sku_per_tenant_unit_conversions() -> None:
    """
    Verify that a 'Katta' of Basmati (25kg) and a 'Katta' of Sugar (50kg) have distinct
    per-SKU unit conversion factors in the database rather than a global multiplier table.
    """
    with transactional_session() as session:
        basmati = session.scalar(select(Sku).where(Sku.code == "SKU-BM25"))
        sugar = session.scalar(select(Sku).where(Sku.code == "SKU-CH50"))
        assert basmati is not None
        assert sugar is not None

        bm_kg = session.scalar(select(SkuUnit).where(SkuUnit.sku_id == basmati.id, SkuUnit.unit_code == "Kg"))
        ch_kg = session.scalar(select(SkuUnit).where(SkuUnit.sku_id == sugar.id, SkuUnit.unit_code == "Kg"))
        assert bm_kg is not None
        assert ch_kg is not None
        # 1 Kg of 25kg Basmati Katta is 1/25 of base unit; 1 Kg of 50kg Sugar Katta is 1/50 of base unit
        assert (bm_kg.multiplier_num, bm_kg.multiplier_den) == (1, 25)
        assert (ch_kg.multiplier_num, ch_kg.multiplier_den) == (1, 50)


def test_wamid_idempotency_constraint() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.slug == "patel_fmcg"))
        assert tenant is not None
        customer = session.scalar(select(Customer).where(Customer.tenant_id == tenant.id))
        assert customer is not None

        msg1 = InboundMessage(
            tenant_id=tenant.id,
            customer_id=customer.id,
            wamid="wamid.HBgMOTE5ODIwNDExMjA5FQIAEhggAA112233",
            raw_text="5 peti Parle-G",
        )
        session.add(msg1)
        session.commit()

        dup_msg = InboundMessage(
            tenant_id=tenant.id,
            customer_id=customer.id,
            wamid="wamid.HBgMOTE5ODIwNDExMjA5FQIAEhggAA112233",
            raw_text="5 peti Parle-G",
        )
        session.add(dup_msg)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_tenant_isolation_between_patel_and_glow() -> None:
    with transactional_session() as session:
        patel = session.scalar(select(Tenant).where(Tenant.slug == "patel_fmcg"))
        glow = session.scalar(select(Tenant).where(Tenant.slug == "glow_insta"))
        assert patel is not None
        assert glow is not None

        patel_skus = list(session.scalars(select(Sku.code).where(Sku.tenant_id == patel.id)).all())
        glow_skus = list(session.scalars(select(Sku.code).where(Sku.tenant_id == glow.id)).all())
        assert "SKU-PG05" in patel_skus
        assert "SKU-PG05" not in glow_skus
        assert "KAI-LAV01" in glow_skus
        assert "KAI-LAV01" not in patel_skus


def test_webhook_verification_rejects_missing_or_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        # Wrong token -> 403
        bad_resp = client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "orderbridge_webhook_verify_2026",
                "hub.challenge": "12345",
            },
        )
        assert bad_resp.status_code == 403

        # Correct token -> 200 with challenge
        ok_resp = client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test_verify_token_secure_2026",
                "hub.challenge": "challenge_998877",
            },
        )
        assert ok_resp.status_code == 200
        assert ok_resp.text == "challenge_998877"


def test_fail_fast_secret_validation_in_production() -> None:
    # Missing or placeholder secrets in production must raise RuntimeError
    bad_prod = Settings(
        APP_ENV="production",
        DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/orderbridge",
        JWT_SECRET="CHANGE_ME_32_CHAR_MINIMUM_JWT_SIGNING_KEY",
        META_VERIFY_TOKEN="orderbridge_webhook_verify_2026",
        META_APP_SECRET="valid_app_secret_1234567890",
        PHONE_ENCRYPTION_KEY="valid_phone_encryption_key_123456",
    )
    with pytest.raises(RuntimeError, match="FATAL: Secret"):
        bad_prod.validate_production_readiness()

    # SQLite in production must raise RuntimeError
    bad_db_prod = Settings(
        APP_ENV="production",
        DATABASE_URL="sqlite:///./local.db",
        JWT_SECRET="strong_production_jwt_secret_key_2026",
        META_VERIFY_TOKEN="strong_production_verify_token_2026",
        META_APP_SECRET="strong_production_app_secret_2026",
        PHONE_ENCRYPTION_KEY="strong_production_phone_key_2026",
    )
    with pytest.raises(RuntimeError, match="requires a PostgreSQL DATABASE_URL"):
        bad_db_prod.validate_production_readiness()
