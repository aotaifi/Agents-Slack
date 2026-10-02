"""Human password login and revocable persisted browser sessions.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("actors", sa.Column("password_hash", sa.String(300), nullable=True))
    op.create_table(
        "browser_sessions",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("actors.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_browser_sessions_actor_id", "browser_sessions", ["actor_id"])
    op.create_table(
        "auth_attempts",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("auth_attempts")
    op.drop_table("browser_sessions")
    op.drop_column("actors", "password_hash")
