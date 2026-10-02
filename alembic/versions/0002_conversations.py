"""Participant handles, one-level replies, and reactions.

Revision ID: 0002
Revises: 0001
"""

import re
import unicodedata

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # Adding columns avoids rebuilding referenced SQLite tables during a live upgrade.
    op.add_column("actors", sa.Column("handle", sa.String(130), nullable=False, server_default=""))
    connection = op.get_bind()
    actors = sa.table(
        "actors",
        sa.column("id"),
        sa.column("name"),
        sa.column("kind"),
        sa.column("owner_id"),
        sa.column("handle"),
    )
    rows = list(connection.execute(sa.select(actors).order_by(actors.c.id)).mappings())
    handles, used = {}, set()
    for row in sorted(rows, key=lambda item: (item["kind"] == "agent", item["id"])):
        name = unicodedata.normalize("NFKD", row["name"]).encode("ascii", "ignore").decode()
        local = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].rstrip("-")
        local = local or "participant"
        prefix = handles.get(row["owner_id"], "participant") + "." if row["kind"] == "agent" else ""
        base, suffix = prefix + local, 2
        candidate = base
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used.add(candidate)
        handles[row["id"]] = candidate
        connection.execute(actors.update().where(actors.c.id == row["id"]).values(handle=candidate))
    if connection.dialect.name != "sqlite":
        op.alter_column("actors", "handle", server_default=None)
    op.create_index("ix_actors_handle", "actors", ["handle"], unique=True)
    if connection.dialect.name == "sqlite":
        op.execute("ALTER TABLE messages ADD COLUMN reply_to VARCHAR(36) REFERENCES messages(id)")
    else:
        op.add_column("messages", sa.Column("reply_to", sa.String(36), nullable=True))
    if connection.dialect.name != "sqlite":
        op.create_foreign_key("fk_messages_reply_to", "messages", "messages", ["reply_to"], ["id"])
    op.create_index("ix_messages_reply_to", "messages", ["reply_to"])
    messages = sa.table(
        "messages",
        sa.column("id"),
        sa.column("thread_id"),
        sa.column("metadata", sa.JSON()),
        sa.column("sequence"),
        sa.column("reply_to"),
    )
    previous = {}
    for row in connection.execute(sa.select(messages).order_by(messages.c.sequence)).mappings():
        parent = (row["metadata"] or {}).get("reply_to")
        valid = parent in previous if isinstance(parent, str) else False
        if (
            valid
            and previous[parent]["thread_id"] == row["thread_id"]
            and not previous[parent]["reply_to"]
        ):
            connection.execute(
                messages.update().where(messages.c.id == row["id"]).values(reply_to=parent)
            )
        else:
            parent = None
        previous[row["id"]] = {"thread_id": row["thread_id"], "reply_to": parent}
    op.create_table(
        "reactions",
        sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id"), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("actors.id"), primary_key=True),
        sa.Column("emoji", sa.String(16), primary_key=True),
    )
    # Historical replay uses the same public identity and message shape as new events.
    identities = {
        row["id"]: {
            "handle": handles[row["id"]],
            "owner": (
                {
                    "id": row["owner_id"],
                    "name": next(x["name"] for x in rows if x["id"] == row["owner_id"]),
                    "handle": handles[row["owner_id"]],
                }
                if row["owner_id"] in handles
                else None
            ),
        }
        for row in rows
    }
    events = sa.table(
        "events",
        sa.column("project_id"),
        sa.column("id"),
        sa.column("type"),
        sa.column("payload", sa.JSON()),
    )

    for row in connection.execute(sa.select(events)).mappings():
        payload = row["payload"]
        # Enrich only API-owned identity fields, preserving arbitrary user metadata.
        for field in ("author", "actor"):
            identity = payload.get(field)
            if isinstance(identity, dict) and identity.get("id") in identities:
                identity.update(identities[identity["id"]])
        if row["type"] == "message.created":
            payload.update(
                reply_to=previous.get(payload.get("id"), {}).get("reply_to"),
                reactions=[],
                reply_count=0,
            )
        connection.execute(
            events.update()
            .where(events.c.project_id == row["project_id"], events.c.id == row["id"])
            .values(payload=payload)
        )


def downgrade():
    op.drop_table("reactions")
    op.drop_index("ix_messages_reply_to", table_name="messages")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_messages_reply_to", "messages", type_="foreignkey")
    op.drop_column("messages", "reply_to")
    op.drop_index("ix_actors_handle", table_name="actors")
    op.drop_column("actors", "handle")
