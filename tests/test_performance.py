import json

from conftest import auth, setup_thread
from sqlalchemy import event, func, select

from agent_commons.main import actor_json, message_json, messages_json, reactions_json, timestamp
from agent_commons.models import Actor, Message


def legacy_message_json(db, m):
    """The original per-message serializer, kept as the reference output."""
    return {
        **{
            k: getattr(m, k)
            for k in ("id", "thread_id", "project_id", "text", "mentions", "sequence")
        },
        "author": actor_json(db.get(Actor, m.author_id)),
        "metadata": m.data,
        "reply_to": m.reply_to,
        "reactions": reactions_json(db, m.id),
        "reply_count": db.scalar(
            select(func.count()).select_from(Message).where(Message.reply_to == m.id)
        ),
        "created_at": timestamp(m.created_at),
    }


def populate(client, app, pid, tid, count):
    app.state.posting_limit = 10_000
    agent = client.post("/v1/actors", json={"name": "Helper", "kind": "agent"}).json()
    client.post(f"/v1/projects/{pid}/members", json={"actor_id": agent["actor"]["id"]})
    headers = auth(agent["token"])
    ids = []
    for i in range(count):
        m = client.post(f"/v1/threads/{tid}/messages", json={"text": f"message {i}"}).json()
        ids.append(m["id"])
        if i % 2 == 0:
            client.post(
                f"/v1/threads/{tid}/messages",
                json={"text": f"reply {i}", "reply_to": m["id"]},
                headers=headers,
            )
        if i % 3 == 0:
            for emoji in ("👍", "❓"):
                assert (
                    client.put(
                        f"/v1/messages/{m['id']}/reactions", json={"emoji": emoji}
                    ).status_code
                    == 200
                )
            client.put(f"/v1/messages/{m['id']}/reactions", json={"emoji": "👍"}, headers=headers)
    return ids


def count_statements(app, run):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = app.state.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        run()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


def test_thread_messages_query_count_is_constant(service):
    client, app, _, _ = service
    pid, small = setup_thread(client)
    channel = client.get(f"/v1/projects/{pid}/channels").json()["items"][0]["id"]
    large = client.post(f"/v1/channels/{channel}/threads", json={"title": "Large"}).json()["id"]
    populate(client, app, pid, small, 5)
    populate(client, app, pid, large, 50)
    counts = {}
    for name, tid, expected in (("small", small, 5), ("large", large, 50)):

        def run(tid=tid, expected=expected):
            body = client.get(f"/v1/threads/{tid}/messages?limit=500").json()
            assert sum(1 for m in body["items"] if m["reply_to"] is None) == expected
            assert any(m["reactions"] for m in body["items"])
            assert any(m["reply_count"] for m in body["items"])

        counts[name] = count_statements(app, run)
    print("statement counts", counts)
    assert counts["large"] <= counts["small"]


def test_batch_serialization_matches_per_message(service):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    populate(client, app, pid, tid, 12)
    with app.state.session_factory() as db:
        rows = list(db.scalars(select(Message).order_by(Message.sequence)))
        assert len(rows) > 12
        batch = messages_json(db, rows)
        single = [legacy_message_json(db, m) for m in rows]
        assert [message_json(db, m) for m in rows] == single
        assert json.dumps(batch) == json.dumps(single)
        assert messages_json(db, []) == []


def test_events_beyond_cursor_returns_cursor_only(service):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    populate(client, app, pid, tid, 3)
    body = client.get(f"/v1/projects/{pid}/events?after=2147483647&limit=1").json()
    assert body["items"] == [] and body["next_cursor"] is None
    full = client.get(f"/v1/projects/{pid}/events?limit=500").json()
    assert body["cursor"] == full["cursor"] > 0
    assert full["items"][-1]["id"] == full["cursor"]
