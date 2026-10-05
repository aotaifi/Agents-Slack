"""Upgrade a populated PostgreSQL database revision by revision, then down and up again."""

import json
import uuid
from pathlib import Path

from alembic.config import Config
from conftest import auth, postgres_url
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from agent_commons.main import create_app
from agent_commons.security import digest
from alembic import command

CONFIG = Path(__file__).resolve().parents[1] / "alembic.ini"
REVISIONS = ["0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009"]

OWNER, AGENT = "a0000000-0000-4000-8000-000000000001", "a0000000-0000-4000-8000-000000000002"
PROJECT = "b0000000-0000-4000-8000-000000000001"
CHANNEL = "c0000000-0000-4000-8000-000000000001"
THREAD = "d0000000-0000-4000-8000-000000000001"
M1, M2, M3, M4 = (f"e0000000-0000-4000-8000-00000000000{n}" for n in range(1, 5))
CONNECTION = "f0000000-0000-4000-8000-000000000001"
OWNER_TOKEN, AGENT_TOKEN, CONNECTION_TOKEN = "owner-token", "agent-token", "connection-token"
TEXTS = {
    M1: "Gradient flow converges slowly",
    M2: "Zebrafinch result confirms it",
    M3: "Unrelated note",
    M4: "Follow-up question ☃",
}


def insert_message(db, message_id, sequence, author, body, reply_to=None, legacy_parent=None):
    """Insert a message plus its event, as the revision's own schema allows."""
    metadata = {"reply_to": legacy_parent} if legacy_parent else {}
    db.execute(
        text(
            "INSERT INTO messages (id, thread_id, project_id, author_id, text, mentions, "
            "metadata, sequence, created_at"
            + (", reply_to" if reply_to else "")
            + ") VALUES (:id, :thread, :project, :author, :text, CAST('[]' AS json), "
            "CAST(:metadata AS json), :sequence, now()" + (", :reply_to" if reply_to else "") + ")"
        ),
        dict(id=message_id, thread=THREAD, project=PROJECT, author=author, text=body,
             metadata=json.dumps(metadata), sequence=sequence, reply_to=reply_to),
    )  # fmt: skip
    payload = {"id": message_id, "thread_id": THREAD, "project_id": PROJECT, "text": body,
               "mentions": [], "sequence": sequence, "metadata": metadata,
               "author": {"id": author, "name": "Ana Müller", "kind": "human"}}  # fmt: skip
    db.execute(
        text(
            "INSERT INTO events (project_id, id, type, thread_id, payload, created_at) "
            "VALUES (:project, :id, 'message.created', :thread, CAST(:payload AS json), now())"
        ),
        dict(project=PROJECT, id=sequence, thread=THREAD, payload=json.dumps(payload)),
    )
    db.execute(
        text("UPDATE projects SET cursor = :c WHERE id = :p"), dict(c=sequence, p=PROJECT)
    )


def populate_0001(db):
    for actor, name, kind, owner, admin in (
        (OWNER, "Ana Müller", "human", None, True),
        (AGENT, "Research Bot", "agent", OWNER, False),
    ):
        db.execute(
            text("INSERT INTO actors (id, name, kind, owner_id, is_admin) "
                 "VALUES (:id, :name, :kind, :owner, :admin)"),
            dict(id=actor, name=name, kind=kind, owner=owner, admin=admin),
        )  # fmt: skip
    for actor, raw in ((OWNER, OWNER_TOKEN), (AGENT, AGENT_TOKEN)):
        db.execute(
            text("INSERT INTO tokens (digest, actor_id, revoked) VALUES (:d, :a, false)"),
            dict(d=digest(raw), a=actor),
        )
    db.execute(
        text("INSERT INTO projects (id, name, description, rules, rules_version, cursor) "
             "VALUES (:p, 'Physics', 'Legacy project', 'Be precise', 1, 0)"),
        dict(p=PROJECT),
    )  # fmt: skip
    for actor, role in ((OWNER, "owner"), (AGENT, "member")):
        db.execute(
            text("INSERT INTO memberships (project_id, actor_id, role, muted) "
                 "VALUES (:p, :a, :r, false)"),
            dict(p=PROJECT, a=actor, r=role),
        )  # fmt: skip
    db.execute(
        text("INSERT INTO channels (id, project_id, name, description) "
             "VALUES (:c, :p, 'General', '')"),
        dict(c=CHANNEL, p=PROJECT),
    )  # fmt: skip
    db.execute(
        text("INSERT INTO threads (id, channel_id, project_id, title, created_at) "
             "VALUES (:t, :c, :p, 'Results', now())"),
        dict(t=THREAD, c=CHANNEL, p=PROJECT),
    )  # fmt: skip
    insert_message(db, M1, 1, OWNER, TEXTS[M1])
    # Before 0002 a reply was only recorded in metadata; the migration promotes it.
    insert_message(db, M2, 2, AGENT, TEXTS[M2], legacy_parent=M1)
    insert_message(db, M3, 3, OWNER, TEXTS[M3], legacy_parent="not-a-message")


def populate_0002(db):
    insert_message(db, M4, 4, AGENT, TEXTS[M4], reply_to=M1)
    for actor, emoji in ((AGENT, "\U0001f44d"), (OWNER, "\U0001f389")):
        db.execute(
            text("INSERT INTO reactions (message_id, actor_id, emoji) VALUES (:m, :a, :e)"),
            dict(m=M1, a=actor, e=emoji),
        )


def populate_0005(db):
    db.execute(
        text("INSERT INTO agent_connections (id, actor_id, project_id, label, created_at, revoked) "
             "VALUES (:id, :a, :p, 'laptop', now(), false)"),
        dict(id=CONNECTION, a=AGENT, p=PROJECT),
    )  # fmt: skip
    db.execute(
        text("INSERT INTO tokens (digest, actor_id, revoked, connection_id) "
             "VALUES (:d, :a, false, :c)"),
        dict(d=digest(CONNECTION_TOKEN), a=AGENT, c=CONNECTION),
    )  # fmt: skip


def core_data(db, revision, posted=0):
    """Everything inserted so far, in a form that must be identical after every step."""
    authors = [(M1, OWNER), (M2, AGENT), (M3, OWNER), (M4, AGENT)]
    count = 3 if revision == "0001" else 4  # M4 is added once the 0002 schema exists
    expected = [(m, TEXTS[m], n, a) for n, (m, a) in enumerate(authors[:count], 1)]
    rows = db.execute(text("SELECT id, text, sequence, author_id FROM messages ORDER BY sequence"))
    messages = [tuple(r) for r in rows]
    assert messages[:count] == expected and len(messages) == count + posted
    assert db.scalar(text("SELECT count(*) FROM tokens")) == 2 + (revision >= "0005")
    assert db.scalar(text("SELECT cursor FROM projects")) == count + posted
    ids = [r[0] for r in db.execute(text("SELECT id FROM events ORDER BY id"))]
    assert ids == list(range(1, count + posted + 1))
    assert db.execute(text("SELECT role FROM memberships ORDER BY role")).all() == [
        ("member",),
        ("owner",),
    ]
    assert db.scalar(text("SELECT title FROM threads")) == "Results"
    assert db.scalar(text("SELECT rules FROM projects")) == "Be precise"


def check_conversation_backfill(db):
    handles = dict(db.execute(text("SELECT id, handle FROM actors")).all())
    assert handles == {OWNER: "ana-muller", AGENT: "ana-muller.research-bot"}
    replies = dict(db.execute(text("SELECT id, reply_to FROM messages")).all())
    assert replies[M2] == M1 and replies[M3] is None and replies[M1] is None
    payloads = {
        row[0]: row[1] for row in db.execute(text("SELECT id, payload FROM events ORDER BY id"))
    }
    assert payloads[2]["reply_to"] == M1 and payloads[3]["reply_to"] is None
    assert payloads[2]["reactions"] == [] and payloads[2]["reply_count"] == 0
    assert payloads[1]["author"]["handle"] == "ana-muller"


def check_app(url, expect_new_sequence):
    app = create_app(url)
    try:
        with TestClient(app) as client:
            client.headers.update(auth(OWNER_TOKEN))
            thread = client.get(f"/v1/threads/{THREAD}")
            assert thread.status_code == 200 and thread.json()["title"] == "Results"
            messages = client.get(f"/v1/threads/{THREAD}/messages").json()["items"]
            assert [m["id"] for m in messages] == [M1, M2, M3, M4]
            assert [m["text"] for m in messages] == [TEXTS[m] for m in (M1, M2, M3, M4)]
            assert [m["reply_to"] for m in messages] == [None, M1, None, M1]
            assert sorted(r["emoji"] for r in messages[0]["reactions"]) == sorted(
                ["\U0001f44d", "\U0001f389"]
            )
            assert messages[0]["reply_count"] == 2
            found = client.get(f"/v1/projects/{PROJECT}/search", params={"q": "zebrafinch"})
            assert found.status_code == 200
            assert [i["message"]["id"] for i in found.json()["items"]] == [M2]
            replay = client.get(f"/v1/projects/{PROJECT}/events", params={"after": 0}).json()
            assert [e["id"] for e in replay["items"]][:4] == [1, 2, 3, 4]
            posted = client.post(f"/v1/threads/{THREAD}/messages", json={"text": "After upgrade"})
            assert posted.status_code == 201
            assert posted.json()["sequence"] == expect_new_sequence
            assert (
                client.get(f"/v1/projects/{PROJECT}/search", params={"q": "upgrade"}).status_code
                == 200
            )
    finally:
        app.state.engine.dispose()


def search_index_exists(db):
    return bool(
        db.scalar(text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_messages_text_search'"))
    )


def test_upgrade_populated_database_step_by_step_then_down_and_up(monkeypatch):
    base = make_url(postgres_url())
    name = f"wsmigrate_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    url = base.set(database=name).render_as_string(hide_password=False)
    engine = None
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        monkeypatch.setenv("DATABASE_URL", url)
        config = Config(str(CONFIG))
        engine = create_engine(url)

        command.upgrade(config, "0001")
        with engine.begin() as db:
            populate_0001(db)
        with engine.begin() as db:
            core_data(db, "0001")

        for revision in REVISIONS:
            command.upgrade(config, revision)
            with engine.begin() as db:
                assert db.scalar(text("SELECT version_num FROM alembic_version")) == revision
                if revision == "0002":
                    check_conversation_backfill(db)
                    populate_0002(db)
                if revision == "0005":
                    populate_0005(db)
            with engine.begin() as db:
                core_data(db, revision)
                if revision == "0009":
                    check_mention_email_columns(db)
                assert search_index_exists(db) == (revision >= "0008")

        command.upgrade(config, "head")  # already there: must be a no-op
        check_app(url, expect_new_sequence=5)

        command.downgrade(config, "0007")
        with engine.begin() as db:
            assert not db.scalar(
                text("SELECT count(*) FROM information_schema.columns WHERE column_name='email'")
            )
            core_data(db, "0007", posted=1)
            assert not search_index_exists(db)
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
        command.upgrade(config, "head")
        with engine.begin() as db:
            assert search_index_exists(db)
            core_data(db, "0008", posted=1)
            check_mention_email_columns(db)
            assert db.scalar(text("SELECT count(*) FROM reactions")) == 2
            assert db.scalar(text("SELECT count(*) FROM agent_connections")) == 1
        check_app_after_roundtrip(url)
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def check_mention_email_columns(db):
    """Existing actors keep working: no address, mention emails on by default."""
    actors = db.execute(
        text("SELECT email, email_verified_at, mention_emails FROM actors")
    ).all()
    assert actors and all(tuple(row) == (None, None, True) for row in actors)
    assert db.scalar(text("SELECT count(*) FROM notifications WHERE emailed_at IS NOT NULL")) == 0
    for table in ("email_verifications", "actor_email_log"):
        assert db.scalar(text(f"SELECT count(*) FROM {table}")) == 0


def check_app_after_roundtrip(url):
    app = create_app(url)
    try:
        with TestClient(app) as client:
            client.headers.update(auth(OWNER_TOKEN))
            found = client.get(f"/v1/projects/{PROJECT}/search", params={"q": "zebrafinch"})
            assert [i["message"]["id"] for i in found.json()["items"]] == [M2]
            messages = client.get(f"/v1/threads/{THREAD}/messages").json()["items"]
            assert [m["id"] for m in messages][:4] == [M1, M2, M3, M4]
            assert len(messages) == 5 and messages[-1]["text"] == "After upgrade"
    finally:
        app.state.engine.dispose()
