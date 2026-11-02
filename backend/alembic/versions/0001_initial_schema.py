"""Initial OrderBridge multi-tenant PostgreSQL schema with integer paise and per-SKU units

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-02 08:40:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("brand_name", sa.String(length=200), nullable=False),
        sa.Column("industry_preset", sa.String(length=100), nullable=False),
        sa.Column("primary_color", sa.String(length=20), nullable=False),
        sa.Column("currency_code", sa.String(length=8), nullable=False),
        sa.Column("currency_symbol", sa.String(length=8), nullable=False),
        sa.Column("tax_regime", sa.String(length=32), nullable=False),
        sa.Column("state_code", sa.String(length=8), nullable=False),
        sa.Column("gstin", sa.String(length=32), nullable=True),
        sa.Column("default_erp", sa.String(length=32), nullable=False),
        sa.Column("auto_confirm_enabled", sa.Boolean(), nullable=False),
        sa.Column("clarification_replies_enabled", sa.Boolean(), nullable=False),
        sa.Column("auto_approve_threshold", sa.Float(), nullable=False),
        sa.Column("fuzzy_match_threshold", sa.Float(), nullable=False),
        sa.Column("debounce_window_seconds", sa.Integer(), nullable=False),
        sa.Column("monthly_llm_cap_paise", sa.BigInteger(), nullable=False),
        sa.Column("monthly_llm_spent_paise", sa.BigInteger(), nullable=False),
        sa.Column("message_retention_days", sa.Integer(), nullable=False),
        sa.Column("media_retention_days", sa.Integer(), nullable=False),
        sa.Column("ledger_config_json", sa.JSON(), nullable=False),
        sa.Column("channel_config_json", sa.JSON(), nullable=False),
        sa.Column("feature_flags_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_tenant_user_membership"),
    )
    op.create_index("ix_tenant_memberships_tenant_id", "tenant_memberships", ["tenant_id"])
    op.create_index("ix_tenant_memberships_user_id", "tenant_memberships", ["user_id"])

    op.create_table(
        "skus",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("base_unit", sa.String(length=32), nullable=False),
        sa.Column("pack_size_label", sa.String(length=64), nullable=True),
        sa.Column("base_price_paise", sa.BigInteger(), nullable=False),
        sa.Column("wholesale_price_paise", sa.BigInteger(), nullable=False),
        sa.Column("distributor_price_paise", sa.BigInteger(), nullable=False),
        sa.Column("tax_rate_bps", sa.Integer(), nullable=False),
        sa.Column("hsn_code", sa.String(length=32), nullable=True),
        sa.Column("moq", sa.Integer(), nullable=False),
        sa.Column("max_order_qty", sa.Integer(), nullable=False),
        sa.Column("stock_qty", sa.Integer(), nullable=False),
        sa.Column("aliases_json", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_tenant_sku_code"),
    )
    op.create_index("ix_skus_tenant_id", "skus", ["tenant_id"])

    op.create_table(
        "sku_units",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("sku_id", sa.String(length=64), nullable=False),
        sa.Column("unit_code", sa.String(length=32), nullable=False),
        sa.Column("multiplier_num", sa.Integer(), nullable=False),
        sa.Column("multiplier_den", sa.Integer(), nullable=False),
        sa.Column("unit_price_override_paise", sa.BigInteger(), nullable=True),
        sa.Column("is_base_unit", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["sku_id"], ["skus.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "sku_id", "unit_code", name="uq_tenant_sku_unit"),
    )
    op.create_index("ix_sku_units_tenant_id", "sku_units", ["tenant_id"])
    op.create_index("ix_sku_units_sku_id", "sku_units", ["sku_id"])

    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("business_name", sa.String(length=200), nullable=True),
        sa.Column("whatsapp_phone_hash", sa.String(length=128), nullable=False),
        sa.Column("whatsapp_phone_masked", sa.String(length=32), nullable=False),
        sa.Column("whatsapp_phone_e164", sa.String(length=32), nullable=False),
        sa.Column("state_code", sa.String(length=8), nullable=False),
        sa.Column("price_tier", sa.String(length=32), nullable=False),
        sa.Column("gstin", sa.String(length=32), nullable=True),
        sa.Column("preferred_language", sa.String(length=32), nullable=False),
        sa.Column("consent_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_wamid", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "whatsapp_phone_hash", name="uq_tenant_customer_phone"),
    )
    op.create_index("ix_customers_tenant_id", "customers", ["tenant_id"])
    op.create_index("ix_customers_whatsapp_phone_hash", "customers", ["whatsapp_phone_hash"])

    op.create_table(
        "draft_orders",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("order_number", sa.String(length=64), nullable=False),
        sa.Column("source_channel", sa.String(length=32), nullable=False),
        sa.Column("raw_message_text", sa.Text(), nullable=False),
        sa.Column("detected_intent", sa.String(length=32), nullable=False),
        sa.Column("detected_language", sa.String(length=32), nullable=False),
        sa.Column("is_voice_transcript", sa.Boolean(), nullable=False),
        sa.Column("is_vision_extracted", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("overall_confidence", sa.Float(), nullable=False),
        sa.Column("requires_human_review", sa.Boolean(), nullable=False),
        sa.Column("review_reasons_json", sa.JSON(), nullable=False),
        sa.Column("delivery_notes", sa.String(length=255), nullable=True),
        sa.Column("payment_ref", sa.String(length=255), nullable=True),
        sa.Column("subtotal_paise", sa.BigInteger(), nullable=False),
        sa.Column("cgst_paise", sa.BigInteger(), nullable=False),
        sa.Column("sgst_paise", sa.BigInteger(), nullable=False),
        sa.Column("igst_paise", sa.BigInteger(), nullable=False),
        sa.Column("tax_amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("grand_total_paise", sa.BigInteger(), nullable=False),
        sa.Column("confirmed_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("confirmed_at", sa.String(length=64), nullable=True),
        sa.Column("erp_sync_target", sa.String(length=32), nullable=True),
        sa.Column("erp_sync_ref", sa.String(length=128), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "order_number", name="uq_tenant_order_number"),
    )
    op.create_index("ix_draft_orders_tenant_id", "draft_orders", ["tenant_id"])
    op.create_index("ix_draft_orders_customer_id", "draft_orders", ["customer_id"])
    op.create_index("ix_draft_orders_status", "draft_orders", ["status"])

    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("wamid", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("reply_to_wamid", sa.String(length=128), nullable=True),
        sa.Column("media_url", sa.String(length=512), nullable=True),
        sa.Column("media_mime_type", sa.String(length=64), nullable=True),
        sa.Column("media_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detected_script", sa.String(length=32), nullable=False),
        sa.Column("detected_language", sa.String(length=32), nullable=False),
        sa.Column("detected_intent", sa.String(length=32), nullable=False),
        sa.Column("debounce_group_id", sa.String(length=64), nullable=True),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["draft_orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "wamid", name="uq_tenant_inbound_wamid"),
    )
    op.create_index("ix_inbound_messages_tenant_id", "inbound_messages", ["tenant_id"])
    op.create_index("ix_inbound_messages_customer_id", "inbound_messages", ["customer_id"])
    op.create_index(
        "ix_inbound_debounce",
        "inbound_messages",
        ["tenant_id", "customer_id", "debounce_group_id"],
    )

    op.create_table(
        "draft_order_lines",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("raw_segment", sa.Text(), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.String(length=64), nullable=True),
        sa.Column("sku_code", sa.String(length=64), nullable=False),
        sa.Column("sku_name", sa.String(length=255), nullable=False),
        sa.Column("qty_num", sa.Integer(), nullable=False),
        sa.Column("qty_den", sa.Integer(), nullable=False),
        sa.Column("qty_display", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("base_unit_qty_num", sa.Integer(), nullable=False),
        sa.Column("base_unit_qty_den", sa.Integer(), nullable=False),
        sa.Column("unit_price_paise", sa.BigInteger(), nullable=False),
        sa.Column("tax_rate_bps", sa.Integer(), nullable=False),
        sa.Column("line_subtotal_paise", sa.BigInteger(), nullable=False),
        sa.Column("cgst_paise", sa.BigInteger(), nullable=False),
        sa.Column("sgst_paise", sa.BigInteger(), nullable=False),
        sa.Column("igst_paise", sa.BigInteger(), nullable=False),
        sa.Column("line_tax_paise", sa.BigInteger(), nullable=False),
        sa.Column("line_total_paise", sa.BigInteger(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("match_method", sa.String(length=32), nullable=False),
        sa.Column("requires_review", sa.Boolean(), nullable=False),
        sa.Column("review_flags_json", sa.JSON(), nullable=False),
        sa.Column("candidates_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["draft_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sku_id"], ["skus.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_draft_order_lines_order_id", "draft_order_lines", ["order_id"])
    op.create_index("ix_draft_order_lines_tenant_id", "draft_order_lines", ["tenant_id"])

    op.create_table(
        "order_audit_logs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["draft_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_audit_logs_tenant_id", "order_audit_logs", ["tenant_id"])
    op.create_index("ix_order_audit_logs_order_id", "order_audit_logs", ["order_id"])

    op.create_table(
        "learned_aliases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("normalized_phrase", sa.String(length=255), nullable=False),
        sa.Column("sku_id", sa.String(length=64), nullable=False),
        sa.Column("default_unit", sa.String(length=32), nullable=True),
        sa.Column("source_order_id", sa.String(length=64), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["sku_id"], ["skus.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "normalized_phrase", name="uq_tenant_learned_phrase"),
    )
    op.create_index("ix_learned_aliases_tenant_id", "learned_aliases", ["tenant_id"])
    op.create_index("ix_learned_aliases_sku_id", "learned_aliases", ["sku_id"])

    op.create_table(
        "erp_sync_exports",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("erp_target", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["draft_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "order_id",
            "erp_target",
            "idempotency_key",
            name="uq_erp_export_idem",
        ),
    )
    op.create_index("ix_erp_sync_exports_tenant_id", "erp_sync_exports", ["tenant_id"])
    op.create_index("ix_erp_sync_exports_order_id", "erp_sync_exports", ["order_id"])

    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_processing_jobs_tenant_id", "processing_jobs", ["tenant_id"])
    op.create_index("ix_processing_jobs_job_type", "processing_jobs", ["job_type"])
    op.create_index("ix_processing_jobs_status", "processing_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("processing_jobs")
    op.drop_table("erp_sync_exports")
    op.drop_table("learned_aliases")
    op.drop_table("order_audit_logs")
    op.drop_table("draft_order_lines")
    op.drop_table("inbound_messages")
    op.drop_table("draft_orders")
    op.drop_table("customers")
    op.drop_table("sku_units")
    op.drop_table("skus")
    op.drop_table("tenant_memberships")
    op.drop_table("users")
    op.drop_table("tenants")
