"""Wiederholung fehlgeschlagener IMAP-Mails: erste gescheiterte UID und Versuche.

Scheiterte der Import einer Mail (oder lieferte der Server sie nicht aus), lief
der Cursor trotzdem an ihr vorbei - die Mail war verloren. Jetzt bleibt der
Cursor davor stehen, bis sie durch ist oder MAX_ATTEMPTS erreicht sind.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mailboxes", sa.Column("retry_uid", sa.BigInteger(), nullable=True))
    op.add_column(
        "mailboxes",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("mailboxes", "retry_count")
    op.drop_column("mailboxes", "retry_uid")
