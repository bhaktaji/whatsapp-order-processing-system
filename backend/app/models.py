"""
SQLAlchemy 2.0 Relational Schema for OrderBridge.
Implements:
- Multi-tenant isolation (tenant_id foreign keys + composite unique indexes)
- Integer paise financial columns (BigInteger) and basis-point tax rates (Integer)
- Per-SKU unit conversions (SkuUnit) with rational multipliers (multiplier_num / multiplier_den)
- Role-Based Access Control (User, TenantMembership: OWNER | STAFF | VIEWER)
- WhatsApp message idempotency (UNIQUE(tenant_id, wamid) on InboundMessage)
- 24-hour customer service window tracking (Customer.last_inbound_at)
- Order lifecycle & immutable audit trail (OrderAuditLog)
- Tenant-isolated alias learning loop with provenance & undo (LearnedAlias)
- Idempotent accounting export tracking (ErpSyncExport)
- Durable DB job queue (ProcessingJob)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    brand_name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry_preset: Mapped[str] = mapped_column(String(100), nullable=False, default="fmcg_distributor")
    primary_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#059669")
    currency_code: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    currency_symbol: Mapped[str] = mapped_column(String(8), nullable=False, default="₹")
    tax_regime: Mapped[str] = mapped_column(String(32), nullable=False, default="GST_INDIA")
    state_code: Mapped[str] = mapped_column(String(8), nullable=False, default="27")
    gstin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    default_erp: Mapped[str] = mapped_column(String(32), nullable=False, default="TALLY_PRIME")

    # Hard Rule 4: Human in the loop; auto-confirm is OFF by default
    auto_confirm_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    clarification_replies_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_approve_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.92)
    fuzzy_match_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.72)
    debounce_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=90)

    # LLM budget in integer paise (e.g. ₹5,000 = 500,000 paise)
    monthly_llm_cap_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=500_000)
    monthly_llm_spent_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # DPDP retention policy
    message_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    media_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)

    ledger_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    channel_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    feature_flags_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    skus: Mapped[list[Sku]] = relationship("Sku", back_populates="tenant", cascade="all, delete-orphan")
    customers: Mapped[list[Customer]] = relationship(
        "Customer", back_populates="tenant", cascade="all, delete-orphan"
    )
    orders: Mapped[list[DraftOrder]] = relationship(
        "DraftOrder", back_populates="tenant", cascade="all, delete-orphan"
    )
    memberships: Mapped[list[TenantMembership]] = relationship(
        "TenantMembership", back_populates="tenant", cascade="all, delete-orphan"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    memberships: Mapped[list[TenantMembership]] = relationship(
        "TenantMembership", back_populates="user", cascade="all, delete-orphan"
    )


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_tenant_user_membership"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="STAFF")  # OWNER | STAFF | VIEWER
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="memberships")
    user: Mapped[User] = relationship("User", back_populates="memberships")


class Sku(Base):
    __tablename__ = "skus"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_tenant_sku_code"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="General")
    base_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="Pcs")
    pack_size_label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Integer paise prices (1 INR = 100 paise)
    base_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wholesale_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    distributor_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Basis-point tax rate (e.g. 1800 = 18.00% GST, 500 = 5.00% GST)
    tax_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)
    hsn_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    moq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_order_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    stock_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="skus")
    allowed_units: Mapped[list[SkuUnit]] = relationship(
        "SkuUnit", back_populates="sku", cascade="all, delete-orphan"
    )


class SkuUnit(Base):
    """
    Per-SKU per-tenant unit multiplier table (Phase 2.7).
    Example:
      - SKU-PG05 (Parle-G Family Pack): base_unit="Peti", unit_code="Peti" (1/1), "Dozen" (1/2), "Pkt" (1/24)
      - SKU-BM25 (Basmati 25kg Katta): base_unit="Katta", unit_code="Katta" (1/1), "Bori" (1/1), "Kg" (1/25), "Quintal" (4/1)
    """

    __tablename__ = "sku_units"
    __table_args__ = (UniqueConstraint("tenant_id", "sku_id", "unit_code", name="uq_tenant_sku_unit"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skus.id", ondelete="CASCADE"), nullable=False, index=True
    )
    unit_code: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # Canonical unit name e.g. "Peti", "Kg"
    multiplier_num: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    multiplier_den: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price_override_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_base_unit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    sku: Mapped[Sku] = relationship("Sku", back_populates="allowed_units")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "whatsapp_phone_hash", name="uq_tenant_customer_phone"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    business_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    whatsapp_phone_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    whatsapp_phone_masked: Mapped[str] = mapped_column(String(32), nullable=False)
    whatsapp_phone_e164: Mapped[str] = mapped_column(String(32), nullable=False)
    state_code: Mapped[str] = mapped_column(String(8), nullable=False, default="27")
    price_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="WHOLESALE")
    gstin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(32), nullable=False, default="hinglish")

    # WhatsApp 24-hour customer service window & DPDP consent tracking
    consent_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_wamid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="customers")
    orders: Mapped[list[DraftOrder]] = relationship("DraftOrder", back_populates="customer")


class InboundMessage(Base):
    """
    Stores every incoming webhook message with strict idempotency on (tenant_id, wamid).
    """

    __tablename__ = "inbound_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "wamid", name="uq_tenant_inbound_wamid"),
        Index("ix_inbound_debounce", "tenant_id", "customer_id", "debounce_group_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wamid: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="WHATSAPP_PRIMARY")
    message_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reply_to_wamid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    media_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    media_mime_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    detected_script: Mapped[str] = mapped_column(String(32), nullable=False, default="LATIN")
    detected_language: Mapped[str] = mapped_column(String(32), nullable=False, default="hinglish")
    detected_intent: Mapped[str] = mapped_column(String(32), nullable=False, default="NEW_ORDER")
    debounce_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("draft_orders.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DraftOrder(Base):
    __tablename__ = "draft_orders"
    __table_args__ = (UniqueConstraint("tenant_id", "order_number", name="uq_tenant_order_number"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_number: Mapped[str] = mapped_column(String(64), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(32), nullable=False, default="WHATSAPP_PRIMARY")
    raw_message_text: Mapped[str] = mapped_column(Text, nullable=False)
    detected_intent: Mapped[str] = mapped_column(String(32), nullable=False, default="NEW_ORDER")
    detected_language: Mapped[str] = mapped_column(String(32), nullable=False, default="hinglish")
    is_voice_transcript: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_vision_extracted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Lifecycle: DRAFT -> NEEDS_REVIEW -> CONFIRMED -> SYNCED / FAILED
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="NEEDS_REVIEW", index=True)
    overall_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    review_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    delivery_notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Exact integer paise financial totals
    subtotal_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cgst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sgst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    igst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax_amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    grand_total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    confirmed_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    erp_sync_target: Mapped[str | None] = mapped_column(String(32), nullable=True)
    erp_sync_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="orders")
    customer: Mapped[Customer] = relationship("Customer", back_populates="orders")
    lines: Mapped[list[DraftOrderLine]] = relationship(
        "DraftOrderLine", back_populates="order", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list[OrderAuditLog]] = relationship(
        "OrderAuditLog", back_populates="order", cascade="all, delete-orphan"
    )


class DraftOrderLine(Base):
    __tablename__ = "draft_order_lines"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("draft_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_segment: Mapped[str] = mapped_column(Text, nullable=False)
    span_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    sku_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("skus.id", ondelete="SET NULL"), nullable=True
    )
    sku_code: Mapped[str] = mapped_column(String(64), nullable=False, default="UNKNOWN")
    sku_name: Mapped[str] = mapped_column(String(255), nullable=False, default="UNKNOWN")

    # Rational quantity (qty_num / qty_den) + display float for UI convenience
    qty_num: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    qty_den: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    qty_display: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="Pcs")
    base_unit_qty_num: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    base_unit_qty_den: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Integer paise & basis points
    unit_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    line_subtotal_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cgst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sgst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    igst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    line_tax_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    line_total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    match_method: Mapped[str] = mapped_column(String(32), nullable=False, default="UNRESOLVED")
    requires_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    review_flags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    candidates_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)

    order: Mapped[DraftOrder] = relationship("DraftOrder", back_populates="lines")


class OrderAuditLog(Base):
    __tablename__ = "order_audit_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("draft_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False, default="SYSTEM")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    order: Mapped[DraftOrder] = relationship("DraftOrder", back_populates="audit_logs")


class LearnedAlias(Base):
    """
    Stores staff-approved alias/unit learnings strictly scoped to a single tenant,
    with full provenance and undo support (Phase 2.11).
    """

    __tablename__ = "learned_aliases"
    __table_args__ = (UniqueConstraint("tenant_id", "normalized_phrase", name="uq_tenant_learned_phrase"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    normalized_phrase: Mapped[str] = mapped_column(String(255), nullable=False)
    sku_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skus.id", ondelete="CASCADE"), nullable=False, index=True
    )
    default_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ErpSyncExport(Base):
    """
    Idempotency ledger for accounting exports so an order is never double-posted.
    """

    __tablename__ = "erp_sync_exports"
    __table_args__ = (
        UniqueConstraint("tenant_id", "order_id", "erp_target", "idempotency_key", name="uq_erp_export_idem"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("draft_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    erp_target: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="EXPORTED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProcessingJob(Base):
    """
    Transactional database job queue for async webhook processing and 90-second per-sender debouncing.
    """

    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
