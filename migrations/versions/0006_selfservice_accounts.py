"""Selbstregistrierung: bestaetigte Adressen, dauerhafte Sitzungen, Einmalcodes.

Bisher legte nur die Verwaltung Nutzer an (``python -m app.admin create-user``),
Sitzungen lagen im Prozessspeicher und gingen bei jedem Neustart verloren.

Bestehende Nutzer gelten sofort als bestaetigt - ihre Adresse hat ein Mensch
eingetragen. Ohne das waeren sie nach dem Upgrade ausgesperrt.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("verified_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE users SET verified_at = created_at WHERE verified_at IS NULL")

    op.create_table(
        "login_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False, index=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "verification_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("code_hash", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("verification_codes")
    op.drop_table("login_sessions")
    op.drop_column("users", "verified_at")
