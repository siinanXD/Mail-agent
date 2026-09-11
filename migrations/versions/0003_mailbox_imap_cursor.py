"""IMAP-Cursor je Postfach: hoechste verarbeitete UID und ihre UIDVALIDITY.

Ohne Cursor holte jeder Abruf nur die neuesten Nachrichten. Lagen mehr als
POLL_BATCH_SIZE neue Mails im Postfach, wurden die aelteren nie erreicht.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mailboxes", sa.Column("last_uid", sa.BigInteger(), nullable=True))
    op.add_column("mailboxes", sa.Column("uid_validity", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("mailboxes", "uid_validity")
    op.drop_column("mailboxes", "last_uid")
