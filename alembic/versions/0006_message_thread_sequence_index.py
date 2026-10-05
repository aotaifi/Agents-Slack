"""Index messages by thread and sequence for cursor-paged thread reads.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_messages_thread_id_sequence", "messages", ["thread_id", "sequence"])


def downgrade():
    op.drop_index("ix_messages_thread_id_sequence", table_name="messages")
