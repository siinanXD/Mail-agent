"""Initiales Schema: emails, units, bookings, cancellations, booking_changes,
email_embeddings (pgvector).

Revision ID: 0001
Revises:
Create Date: 2026-09-10
"""

from __future__ import annotations

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "emails",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_message_id", sa.String(255), nullable=False, unique=True),
        sa.Column("sender", sa.String(255), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("email_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_emails_received_at", "emails", ["received_at"])
    op.create_index("ix_emails_email_type", "emails", ["email_type"])

    op.create_table(
        "units",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("booking_reference", sa.String(64), nullable=False, unique=True),
        sa.Column("guest_name", sa.String(255), nullable=False),
        sa.Column("arrival_date", sa.Date(), nullable=True),
        sa.Column("departure_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "unit_id",
            sa.Integer(),
            sa.ForeignKey("units.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_email_id",
            sa.Integer(),
            sa.ForeignKey("emails.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_bookings_arrival_date", "bookings", ["arrival_date"])
    op.create_index("ix_bookings_guest_name", "bookings", ["guest_name"])

    op.create_table(
        "cancellations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "booking_id",
            sa.Integer(),
            sa.ForeignKey("bookings.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("cancelled_at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "source_email_id",
            sa.Integer(),
            sa.ForeignKey("emails.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_cancellations_cancelled_at", "cancellations", ["cancelled_at"])

    op.create_table(
        "booking_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "booking_id",
            sa.Integer(),
            sa.ForeignKey("bookings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("field", sa.String(32), nullable=False),
        sa.Column("old_value", sa.String(255), nullable=True),
        sa.Column("new_value", sa.String(255), nullable=True),
        sa.Column(
            "source_email_id",
            sa.Integer(),
            sa.ForeignKey("emails.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_table(
        "email_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "email_id",
            sa.Integer(),
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk", sa.Text(), nullable=False),
        # Die Dimension haengt am Embedding-Modell und kommt aus der Config.
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.Vector(get_settings().embedding_dim),
            nullable=False,
        ),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.UniqueConstraint("email_id", "chunk_index"),
    )


def downgrade() -> None:
    op.drop_table("email_embeddings")
    op.drop_table("booking_changes")
    op.drop_table("cancellations")
    op.drop_table("bookings")
    op.drop_table("units")
    op.drop_index("ix_emails_email_type", table_name="emails")
    op.drop_index("ix_emails_received_at", table_name="emails")
    op.drop_table("emails")
