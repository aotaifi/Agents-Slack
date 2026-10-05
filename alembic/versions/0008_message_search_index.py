"""Add a PostgreSQL full-text index for project message search.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_messages_text_search ON messages "
            "USING gin (to_tsvector('simple', text))"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX ix_messages_text_search")
