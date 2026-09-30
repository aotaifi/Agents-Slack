"""Single-use project invitations.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("inviter_id", sa.String(36), sa.ForeignKey("actors.id"), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("claim_digest", sa.String(64), nullable=True),
        sa.Column("accepted_actor_id", sa.String(36), sa.ForeignKey("actors.id"), nullable=True),
        sa.Column("issued_token_digest", sa.String(64), nullable=True),
    )
    op.create_index("ix_invitations_project_id", "invitations", ["project_id"])


def downgrade():
    op.drop_table("invitations")
