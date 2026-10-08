"""Add the seven margin-feed tables (cost / margin / COGS).

Revision ID: 0011_margin_feed
Revises: 0010_acct_override_audit
Create Date: 2026-10-08

margin-feed (``.kiro/specs/margin-feed/design.md``, "Data model"). Margin is
tenant-admin-only data (D1), so it gets its own typed tables instead of
``es_documents``: nothing that reads the document store (agent search tools,
rebuild, outbox projections) can reach it. All tables are new and empty, so
there is no backfill.

* ``margin_cost_entries``: immutable admin cost input (purchase lots,
  overrides, adders). Partial uniques give one active entry per natural key
  and one active price per BOL.
* ``margin_settings``: per-tenant window, staleness, floor and timezone, plus
  the ``feed_activated_at`` watermark that keeps the gap sweep off sources
  from before the feed was enabled.
* ``margin_records``: versioned margin per (stage, source key). The
  ``ck_mr_cost_null_iff_none`` CHECK makes "missing cost stored as 0"
  impossible at the database (AC-8). ``alert_state`` is the RevenueGuard
  work queue, served by the partial index ``ix_mr_alert_pending``.
* ``margin_alerts``, ``margin_recompute_runs``, ``margin_weekly_reports`` and
  ``margin_skipped_sources``: admin alerts, recompute runs (one running per
  tenant), weekly reports, and durable phase-1 skips.

JSON columns are ``jsonb``, as new tables prefer since 0008; they are stored,
not queried. CHECK constraints are inline; partial indexes use
``postgresql_where``.

The revision id is kept under 32 characters, the width of Alembic's default
``alembic_version.version_num`` column. If another ``0011`` revision lands on
the same parent first, re-parent this one; never merge two heads.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011_margin_feed"
down_revision: Union[str, None] = "0010_acct_override_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "margin_cost_entries",
        sa.Column("entry_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("product_code", sa.String(64), nullable=False),
        sa.Column("terminal_id", sa.String(128), nullable=True),
        sa.Column("supplier_name", sa.String(128), nullable=True),
        _ts("effective_at"),
        _ts("effective_to", nullable=True),
        sa.Column("unit_cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("gallons_milli", sa.BigInteger(), nullable=True),
        sa.Column("adder_type", sa.String(16), nullable=True),
        sa.Column("bol_id", sa.String(128), nullable=True),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("notes", sa.String(500), nullable=True),
        sa.Column("natural_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("supersedes_id", sa.String(64), nullable=True),
        sa.Column("superseded_by_id", sa.String(64), nullable=True),
        sa.Column("status_reason", sa.String(500), nullable=True),
        sa.Column("status_changed_by", sa.String(255), nullable=True),
        _ts("status_changed_at", nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("import_batch_id", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        _ts("created_at"),
        sa.CheckConstraint(
            "(kind <> 'purchase' OR (gallons_milli IS NOT NULL AND terminal_id IS NOT NULL "
            "AND effective_to IS NULL)) "
            "AND (kind = 'purchase' OR (gallons_milli IS NULL AND bol_id IS NULL)) "
            "AND ((kind = 'adder') = (adder_type IS NOT NULL))",
            name="ck_mce_kind_fields",
        ),
    )
    op.create_index(
        "uq_mce_active_natural_key",
        "margin_cost_entries",
        ["tenant_id", "natural_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_mce_active_bol",
        "margin_cost_entries",
        ["tenant_id", "bol_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND bol_id IS NOT NULL"),
    )
    op.create_index(
        "ix_mce_lookup",
        "margin_cost_entries",
        ["tenant_id", "kind", "product_code", "terminal_id", "effective_at"],
    )

    op.create_table(
        "margin_settings",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("wac_window_days", sa.Integer(), nullable=False),
        sa.Column("rack_staleness_days", sa.Integer(), nullable=False),
        sa.Column("floor_micros", sa.BigInteger(), nullable=False),
        sa.Column("product_floors", postgresql.JSONB(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        _ts("feed_activated_at", nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        _ts("updated_at", nullable=True),
    )

    op.create_table(
        "margin_records",
        sa.Column("record_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("source_key", sa.String(256), nullable=False),
        sa.Column("order_id", sa.String(128), nullable=True),
        sa.Column("invoice_id", sa.String(128), nullable=True),
        sa.Column("line_index", sa.Integer(), nullable=True),
        sa.Column("line_id", sa.String(128), nullable=True),
        sa.Column("customer_id", sa.String(128), nullable=True),
        sa.Column("account_id", sa.String(128), nullable=True),
        sa.Column("product_code", sa.String(64), nullable=False),
        sa.Column("terminal_id", sa.String(128), nullable=True),
        sa.Column("gallons_ugal", sa.BigInteger(), nullable=False),
        sa.Column("unit_price_micros", sa.BigInteger(), nullable=False),
        sa.Column("revenue_cents", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("product_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("adders_micros", sa.BigInteger(), nullable=True),
        sa.Column("landed_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("margin_cents", sa.BigInteger(), nullable=True),
        sa.Column("margin_per_gallon_micros", sa.BigInteger(), nullable=True),
        sa.Column("margin_bp", sa.BigInteger(), nullable=True),
        sa.Column("no_cost_reason", sa.String(40), nullable=True),
        sa.Column("flag_missing_cost", sa.Boolean(), nullable=False),
        sa.Column("flag_negative_margin", sa.Boolean(), nullable=False),
        sa.Column("flag_below_floor", sa.Boolean(), nullable=False),
        sa.Column("flag_terminal_unattributed", sa.Boolean(), nullable=False),
        sa.Column("floor_micros_used", sa.BigInteger(), nullable=False),
        sa.Column("cost_snapshot", postgresql.JSONB(), nullable=False),
        _ts("as_of"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        _ts("frozen_at", nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("recompute_run_id", sa.String(64), nullable=True),
        _ts("computed_at"),
        sa.Column("alert_state", sa.String(16), nullable=False, server_default="none"),
        sa.CheckConstraint(
            "(method = 'none') = (landed_cost_micros IS NULL) "
            "AND (method = 'none') = (product_cost_micros IS NULL) "
            "AND (method = 'none') = (cost_cents IS NULL) "
            "AND (method = 'none') = (margin_cents IS NULL) "
            "AND (method = 'none') = (margin_per_gallon_micros IS NULL) "
            "AND (method <> 'none' OR margin_bp IS NULL)",
            name="ck_mr_cost_null_iff_none",
        ),
        sa.CheckConstraint(
            "alert_state IN ('none', 'pending', 'done', 'expired')",
            name="ck_mr_alert_state",
        ),
        sa.CheckConstraint(
            "flag_missing_cost = (method = 'none')", name="ck_mr_missing_flag"
        ),
        sa.UniqueConstraint(
            "tenant_id", "stage", "source_key", "version", name="uq_mr_version"
        ),
    )
    op.create_index(
        "uq_mr_active",
        "margin_records",
        ["tenant_id", "stage", "source_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_mr_tenant_asof", "margin_records", ["tenant_id", "as_of", "record_id"]
    )
    op.create_index(
        "ix_mr_tenant_cust_prod",
        "margin_records",
        ["tenant_id", "customer_id", "product_code", "stage", "as_of"],
    )
    op.create_index("ix_mr_tenant_order", "margin_records", ["tenant_id", "order_id"])
    op.create_index(
        "ix_mr_alert_pending",
        "margin_records",
        ["tenant_id", "computed_at"],
        postgresql_where=sa.text("alert_state = 'pending'"),
    )

    op.create_table(
        "margin_alerts",
        sa.Column("alert_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("alert_type", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("dedupe_key", sa.String(256), nullable=False),
        sa.Column("record_id", sa.String(64), nullable=True),
        sa.Column("order_id", sa.String(128), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("proposal_id", sa.String(128), nullable=True),
        sa.Column("customer_id", sa.String(128), nullable=True),
        sa.Column("product_code", sa.String(64), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        _ts("created_at"),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        _ts("resolved_at", nullable=True),
        sa.Column("resolution_note", sa.String(500), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "alert_type", "dedupe_key", name="uq_malert_dedupe"
        ),
    )
    op.create_index(
        "ix_malert_tenant_status",
        "margin_alerts",
        ["tenant_id", "status", "created_at"],
    )

    op.create_table(
        "margin_recompute_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("stages", postgresql.JSONB(), nullable=False),
        sa.Column("only_missing", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
        _ts("started_at"),
        _ts("finished_at", nullable=True),
        _ts("heartbeat_at"),
        sa.Column("digest_state", sa.String(16), nullable=False, server_default="none"),
    )
    op.create_index(
        "uq_mrun_running",
        "margin_recompute_runs",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_mrun_digest_pending",
        "margin_recompute_runs",
        ["tenant_id"],
        postgresql_where=sa.text("digest_state = 'pending'"),
    )

    op.create_table(
        "margin_weekly_reports",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("iso_week", sa.String(8), primary_key=True),
        _ts("period_start"),
        _ts("period_end"),
        sa.Column("revenue_cents", sa.BigInteger(), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=False),
        sa.Column("margin_cents", sa.BigInteger(), nullable=False),
        sa.Column("revenue_cents_missing_cost", sa.BigInteger(), nullable=False),
        sa.Column("records_total", sa.BigInteger(), nullable=False),
        sa.Column("gallons_ugal_total", sa.BigInteger(), nullable=False),
        sa.Column("flag_counts", postgresql.JSONB(), nullable=False),
        sa.Column("missing_cost_share_bp", sa.BigInteger(), nullable=True),
        _ts("generated_at"),
    )

    op.create_table(
        "margin_skipped_sources",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("stage", sa.String(16), primary_key=True),
        sa.Column("source_key", sa.String(256), primary_key=True),
        sa.Column("order_id", sa.String(128), nullable=True),
        sa.Column("invoice_id", sa.String(128), nullable=True),
        sa.Column("line_index", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("error_type", sa.String(64), nullable=False),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        sa.Column("seen_count", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    """Drop all seven margin tables.

    WARNING: this deletes every margin record, cost entry, alert, recompute
    run, weekly report, skipped-source row and per-tenant margin setting
    (including the activation watermark). The data is not recoverable from
    any other store. Take a backup first if it matters.
    """
    op.drop_table("margin_skipped_sources")
    op.drop_table("margin_weekly_reports")
    op.drop_index("ix_mrun_digest_pending", table_name="margin_recompute_runs")
    op.drop_index("uq_mrun_running", table_name="margin_recompute_runs")
    op.drop_table("margin_recompute_runs")
    op.drop_index("ix_malert_tenant_status", table_name="margin_alerts")
    op.drop_table("margin_alerts")
    op.drop_index("ix_mr_alert_pending", table_name="margin_records")
    op.drop_index("ix_mr_tenant_order", table_name="margin_records")
    op.drop_index("ix_mr_tenant_cust_prod", table_name="margin_records")
    op.drop_index("ix_mr_tenant_asof", table_name="margin_records")
    op.drop_index("uq_mr_active", table_name="margin_records")
    op.drop_table("margin_records")
    op.drop_table("margin_settings")
    op.drop_index("ix_mce_lookup", table_name="margin_cost_entries")
    op.drop_index("uq_mce_active_bol", table_name="margin_cost_entries")
    op.drop_index("uq_mce_active_natural_key", table_name="margin_cost_entries")
    op.drop_table("margin_cost_entries")
