"""Mitarbeiter, ihre Wohnungen und der Versand des Putzplans per WhatsApp.

Alle vier Tabellen sind Mandantendaten und bekommen Row-Level-Security wie die
Tabellen aus 0002 - Telefonnummern der Mitarbeiter sind personenbezogen.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TABLES = ("staff_members", "staff_units", "cleaning_schedules", "cleaning_dispatches")

#: Wie in 0002: NULL = kein Mandant = keine Zeile (fail closed).
TENANT_EXPR = "NULLIF(current_setting('app.tenant_id', true), '')::integer"


def _tenant_column() -> sa.Column:
    return sa.Column(
        "tenant_id",
        sa.Integer(),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def upgrade() -> None:
    op.create_table(
        "staff_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        _tenant_column(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "staff_units",
        sa.Column("id", sa.Integer(), primary_key=True),
        _tenant_column(),
        sa.Column(
            "staff_id",
            sa.Integer(),
            sa.ForeignKey("staff_members.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "unit_id",
            sa.Integer(),
            sa.ForeignKey("units.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.UniqueConstraint("staff_id", "unit_id", name="uq_staff_units_staff_unit"),
    )

    op.create_table(
        "cleaning_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        _tenant_column(),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("send_weekday", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("send_time", sa.Time(), nullable=False, server_default="18:00"),
        sa.Column("active_since", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_id", name="uq_cleaning_schedules_tenant"),
    )

    op.create_table(
        "cleaning_dispatches",
        sa.Column("id", sa.Integer(), primary_key=True),
        _tenant_column(),
        sa.Column(
            "staff_id",
            sa.Integer(),
            sa.ForeignKey("staff_members.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("tasks", sa.JSON(), nullable=False),
        sa.Column("provider_message_id", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_cleaning_dispatches_staff_week", "cleaning_dispatches", ["staff_id", "week_start"]
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {TENANT_EXPR}) "
            f"WITH CHECK (tenant_id = {TENANT_EXPR})"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
