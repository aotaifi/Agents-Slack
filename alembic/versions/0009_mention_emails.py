"""Verified email addresses and immediate mention emails.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("actors", sa.Column("email", sa.String(254), nullable=True))
    op.add_column("actors", sa.Column("email_verified_at", sa.DateTime(timezone=True)))
    op.add_column(
        "actors",
        sa.Column("mention_emails", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("notifications", sa.Column("emailed_at", sa.DateTime(timezone=True)))
    op.create_table(
        "email_verifications",
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("actors.id"), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("code_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_table(
        "actor_email_log",
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("actors.id"), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("sent", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("actor_email_log")
    op.drop_table("email_verifications")
    op.drop_column("notifications", "emailed_at")
    op.drop_column("actors", "mention_emails")
    op.drop_column("actors", "email_verified_at")
    op.drop_column("actors", "email")
