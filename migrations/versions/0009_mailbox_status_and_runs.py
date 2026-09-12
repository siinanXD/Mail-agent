"""Verbindungsstatus je Postfach und ein Protokoll der Agentenlaeufe.

Bisher sah man am Postfach nur ``last_error`` aus dem letzten Mailabruf - also
frueestens nach dem naechsten geplanten Abruf, ob die Verbindung ueberhaupt
steht. ``status`` kommt dagegen aus einem eigenen, billigen Verbindungstest
(Login, Ordner waehlen, fertig) und sagt jederzeit, ob das Postfach verbunden
ist.

``agent_runs`` protokolliert jeden Arbeitsgang, damit der Kunde sieht, was im
Hintergrund passiert.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mailboxes",
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="unknown"
        ),
    )
    op.add_column("mailboxes", sa.Column("status_message", sa.Text(), nullable=True))
    op.add_column("mailboxes", sa.Column("last_check_at", sa.DateTime(), nullable=True))

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "mailbox_id",
            sa.Integer(),
            sa.ForeignKey("mailboxes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bookings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancellations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("agent_runs")
    op.drop_column("mailboxes", "last_check_at")
    op.drop_column("mailboxes", "status_message")
    op.drop_column("mailboxes", "status")
