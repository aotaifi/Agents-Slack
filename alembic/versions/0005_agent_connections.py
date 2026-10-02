"""Project-scoped agent credentials and permanent session bindings.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("actors.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("session_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_connections_actor_id", "agent_connections", ["actor_id"])
    op.create_index("ix_agent_connections_project_id", "agent_connections", ["project_id"])
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE tokens ADD COLUMN connection_id VARCHAR(36) "
            "REFERENCES agent_connections(id)"
        )
    else:
        op.add_column("tokens", sa.Column("connection_id", sa.String(36), nullable=True))
        op.create_foreign_key(
            "fk_tokens_connection_id", "tokens", "agent_connections", ["connection_id"], ["id"]
        )
    op.create_index("ix_tokens_connection_id", "tokens", ["connection_id"])


def downgrade():
    op.drop_index("ix_tokens_connection_id", table_name="tokens")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_tokens_connection_id", "tokens", type_="foreignkey")
    op.drop_column("tokens", "connection_id")
    op.drop_table("agent_connections")
