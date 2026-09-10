"""Mandantenfaehigkeit: tenants, users, mailboxes, tenant_id ueberall, RLS.

Bestehende Daten wandern in den Mandanten "Standard" (id 1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DEFAULT_TENANT_ID = 1

#: Tabellen mit Mandantendaten - hier greift Row-Level-Security.
TENANT_TABLES = (
    "emails",
    "units",
    "bookings",
    "cancellations",
    "booking_changes",
    "email_embeddings",
)

#: Bisher global eindeutig, kuenftig nur je Mandant.
#: (Tabelle, alter Constraint-Name aus 0001, Spalte)
PER_TENANT_UNIQUE = (
    ("emails", "emails_provider_message_id_key", "provider_message_id"),
    ("bookings", "bookings_booking_reference_key", "booking_reference"),
    ("units", "units_normalized_name_key", "normalized_name"),
)

#: NULLIF, weil ein zurueckgesetzter Wert als '' statt NULL ankommen kann.
#: NULL = kein Mandant = keine Zeile (fail closed).
TENANT_EXPR = "NULLIF(current_setting('app.tenant_id', true), '')::integer"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        f"INSERT INTO tenants (id, name, slug) VALUES ({DEFAULT_TENANT_ID}, 'Standard', 'standard')"
    )
    op.execute("SELECT setval('tenants_id_seq', (SELECT max(id) FROM tenants))")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "mailboxes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("folder", sa.String(128), nullable=False, server_default="INBOX"),
        sa.Column("use_ssl", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("since_date", sa.Date(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_polled_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )

    # tenant_id nachziehen: erst nullable, befuellen, dann NOT NULL + FK.
    for table in TENANT_TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} SET tenant_id = {DEFAULT_TENANT_ID}")
        op.alter_column(table, "tenant_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_tenant", table, "tenants", ["tenant_id"], ["id"], ondelete="CASCADE"
        )
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    for table, old_name, column in PER_TENANT_UNIQUE:
        op.drop_constraint(old_name, table, type_="unique")
        op.create_unique_constraint(
            f"uq_{table}_tenant_{column}", table, ["tenant_id", column]
        )

    # Row-Level-Security. FORCE ist noetig, weil die Anwendung als Eigentuemer
    # der Tabellen verbindet - ohne FORCE wuerde RLS fuer sie gar nicht gelten.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {TENANT_EXPR}) "
            f"WITH CHECK (tenant_id = {TENANT_EXPR})"
        )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    for table, old_name, column in PER_TENANT_UNIQUE:
        op.drop_constraint(f"uq_{table}_tenant_{column}", table, type_="unique")
        op.create_unique_constraint(old_name, table, [column])

    for table in TENANT_TABLES:
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_constraint(f"fk_{table}_tenant", table, type_="foreignkey")
        op.drop_column(table, "tenant_id")

    op.drop_table("mailboxes")
    op.drop_table("users")
    op.drop_table("tenants")
