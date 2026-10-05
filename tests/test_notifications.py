from concurrent.futures import ThreadPoolExecutor
from threading import Event as Signal
from types import SimpleNamespace

import pytest
from conftest import auth, setup_thread
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from agent_commons.main import create_app
from agent_commons.models import (
    Actor,
    Channel,
    Event,
    Membership,
    Message,
    Notification,
    Project,
    Thread,
)


def recipient(client, projects):
    result = client.post("/v1/actors", json={"name": "Alex", "kind": "human"}).json()
    for project_id in projects:
        assert (
            client.post(
                f"/v1/projects/{project_id}/members", json={"actor_id": result["actor"]["id"]}
            ).status_code
            == 201
        )
    return result


def mention(client, thread_id, target, text="Please review", **extra):
    response = client.post(
        f"/v1/threads/{thread_id}/messages",
        json={
            "text": text,
            "mentions": [target["actor"]["id"]],
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def inbox(client, target, **params):
    response = client.get("/v1/notifications", params=params, headers=auth(target["token"]))
    assert response.status_code == 200, response.text
    return response.json()


def test_atomic_mentions_deduplicate_retries_and_ignore_self_agent_reactions(service):
    client, app, owner, _ = service
    pid, tid = setup_thread(client)
    target = recipient(client, [pid])
    agent = client.post("/v1/actors", json={"name": "Agent", "kind": "agent"}).json()
    client.post(f"/v1/projects/{pid}/members", json={"actor_id": agent["actor"]["id"]})
    assert inbox(client, target) == {
        "items": [],
        "next_cursor": None,
        "cursor": None,
        "unread_count": 0,
    }
    body = {
        "text": "λ" * 301,
        "mentions": [
            target["actor"]["id"],
            target["actor"]["id"],
            owner["actor"]["id"],
            agent["actor"]["id"],
        ],
        "metadata": {"private": "omitted"},
    }
    path = f"/v1/threads/{tid}/messages"
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(path, json=body, headers={"Idempotency-Key": "mention-once"}),
                range(4),
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
    message = responses[0].json()
    item = inbox(client, target)["items"][0]
    assert item["message"]["id"] == message["id"]
    assert item["message"]["text"] == "λ" * 300 and item["message"]["truncated"]
    assert "metadata" not in item["message"] and "private" not in str(item)
    assert item["message"]["author"]["id"] == owner["actor"]["id"]
    assert client.get("/v1/notifications").json()["unread_count"] == 0
    assert (
        client.put(f"/v1/messages/{message['id']}/reactions", json={"emoji": "👍"}).status_code
        == 200
    )
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 1
    # An agent's explicit mention of a human produces a human notification.
    posted = client.post(
        path,
        json={
            "text": "Agent review",
            "mentions": [target["actor"]["id"]],
            "reply_to": message["id"],
        },
        headers=auth(agent["token"]),
    )
    assert posted.status_code == 201
    assert inbox(client, target)["items"][0]["message"]["reply_to"] == message["id"]
    with app.state.session_factory() as db:
        before_messages = db.scalar(select(func.count()).select_from(Message))
        before_notifications = db.scalar(select(func.count()).select_from(Notification))
        before_cursor = db.get(Project, pid).cursor

    def failed_insert(connection, cursor, statement, parameters, context, many):
        if statement.lower().startswith("insert into notifications"):
            raise RuntimeError("notification storage unavailable")

    event.listen(app.state.engine, "before_cursor_execute", failed_insert)
    try:
        with pytest.raises(RuntimeError, match="notification storage unavailable"):
            mention(client, tid, target, "Must roll back")
    finally:
        event.remove(app.state.engine, "before_cursor_execute", failed_insert)
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == before_messages
        assert db.scalar(select(func.count()).select_from(Notification)) == before_notifications
        assert db.get(Project, pid).cursor == before_cursor


def test_newest_pagination_filters_and_read_all_snapshot(service):
    client, _, _, _ = service
    pid, tid = setup_thread(client)
    target = recipient(client, [pid])
    messages = [mention(client, tid, target, f"Mention {index}") for index in range(3)]
    first = inbox(client, target, limit=2)
    assert [item["message"]["id"] for item in first["items"]] == [
        m["id"] for m in messages[-1:0:-1]
    ]
    assert first["cursor"] == first["items"][0]["id"] and first["unread_count"] == 3
    page = inbox(client, target, limit=2, before=first["next_cursor"])
    assert [item["message"]["id"] for item in page["items"]] == [messages[0]["id"]]
    assert page["next_cursor"] is None and page["cursor"] == first["cursor"]
    oldest = page["items"][0]["id"]
    read = client.patch(
        f"/v1/notifications/{oldest}", json={"read": True}, headers=auth(target["token"])
    )
    repeated = client.patch(
        f"/v1/notifications/{oldest}", json={"read": True}, headers=auth(target["token"])
    )
    assert read.status_code == 200 and repeated.json()["read_at"] == read.json()["read_at"]
    filtered = inbox(client, target, unread_only=True, before=oldest)
    assert filtered["items"] == [] and filtered["cursor"] == first["cursor"]
    assert filtered["unread_count"] == 2
    latest = mention(client, tid, target, "After displayed snapshot")
    response = client.post(
        "/v1/notifications/read-all",
        json={"through": first["cursor"]},
        headers=auth(target["token"]),
    )
    assert response.json() == {"updated": 2}
    assert client.post(
        "/v1/notifications/read-all",
        json={"through": first["cursor"]},
        headers=auth(target["token"]),
    ).json() == {"updated": 0}
    remaining = inbox(client, target, unread_only=True)
    assert remaining["unread_count"] == 1 and remaining["items"][0]["message"]["id"] == latest["id"]
    assert inbox(client, target)["items"][-1]["read_at"] == read.json()["read_at"]
    unread = client.patch(
        f"/v1/notifications/{oldest}", json={"read": False}, headers=auth(target["token"])
    )
    assert unread.json()["read_at"] is None and inbox(client, target)["unread_count"] == 2
    for params in ({"limit": 0}, {"limit": 101}, {"before": 0}):
        assert (
            client.get(
                "/v1/notifications", params=params, headers=auth(target["token"])
            ).status_code
            == 422
        )
    assert (
        client.post(
            "/v1/notifications/read-all", json={"through": 0}, headers=auth(target["token"])
        ).status_code
        == 422
    )


def test_membership_scope_hides_counts_updates_and_restores_retained_state(service):
    client, _, _, _ = service
    first_pid, first_tid = setup_thread(client)
    second_pid, second_tid = setup_thread(client)
    target = recipient(client, [first_pid, second_pid])
    mention(client, first_tid, target, "Private first project")
    first = inbox(client, target)["items"][0]
    client.patch(
        f"/v1/notifications/{first['id']}", json={"read": True}, headers=auth(target["token"])
    )
    mention(client, second_tid, target, "Visible second project")
    second = inbox(client, target)["items"][0]
    assert client.patch(f"/v1/notifications/{first['id']}", json={"read": False}).status_code == 404
    path = f"/v1/projects/{first_pid}/members/{target['actor']['id']}"
    assert client.delete(path).status_code == 204
    visible = inbox(client, target)
    assert [item["id"] for item in visible["items"]] == [second["id"]]
    assert visible["unread_count"] == 1
    assert (
        client.patch(
            f"/v1/notifications/{first['id']}", json={"read": False}, headers=auth(target["token"])
        ).status_code
        == 404
    )
    assert client.post(
        "/v1/notifications/read-all",
        json={"through": visible["cursor"]},
        headers=auth(target["token"]),
    ).json() == {"updated": 1}
    assert (
        client.post(
            f"/v1/projects/{first_pid}/members", json={"actor_id": target["actor"]["id"]}
        ).status_code
        == 201
    )
    restored = inbox(client, target)
    assert restored["unread_count"] == 0 and restored["items"][-1]["read_at"] is not None
    assert (
        client.patch(
            f"/v1/notifications/{first['id']}", json={"read": False}, headers=auth(target["token"])
        ).status_code
        == 200
    )
    assert inbox(client, target)["unread_count"] == 1
    agent = client.post("/v1/actors", json={"name": "No human inbox", "kind": "agent"}).json()
    headers = auth(agent["token"])
    assert client.get("/v1/notifications", headers=headers).status_code == 403
    assert (
        client.patch(
            f"/v1/notifications/{first['id']}", json={"read": True}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/notifications/read-all", json={"through": visible["cursor"]}, headers=headers
        ).status_code
        == 403
    )


def test_cookie_origin_read_state_persistence_current_names_and_no_public_events(service):
    client, app, owner, url = service
    pid, tid = setup_thread(client)
    target = recipient(client, [pid])
    message = mention(client, tid, target)
    item = inbox(client, target)["items"][0]
    password = client.post(
        "/v1/auth/password",
        json={"password": "long personal password"},
        headers=auth(target["token"]),
    )
    raw = password.cookies["workspace_session"]
    cookie = {"Authorization": "", "Cookie": f"workspace_session={raw}"}
    assert client.get("/v1/notifications", headers=cookie).status_code == 200
    assert (
        client.patch(
            f"/v1/notifications/{item['id']}", json={"read": True}, headers=cookie
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/notifications/read-all", json={"through": item["id"]}, headers=cookie
        ).status_code
        == 403
    )
    event_cursor = client.get(f"/v1/projects/{pid}/events").json()["cursor"]
    marked = client.patch(
        f"/v1/notifications/{item['id']}",
        json={"read": True},
        headers={**cookie, "Origin": "http://testserver"},
    )
    assert marked.status_code == 200 and marked.json()["read_at"]
    assert client.get(f"/v1/projects/{pid}/events").json()["cursor"] == event_cursor
    client.patch(f"/v1/projects/{pid}", json={"name": "Renamed project"})
    client.patch(f"/v1/channels/{item['channel']['id']}", json={"name": "Renamed channel"})
    client.patch("/v1/me", json={"name": "Renamed author"})
    restarted = create_app(url)
    with TestClient(restarted) as other:
        restored = other.get("/v1/notifications", headers=auth(target["token"])).json()
        assert restored["unread_count"] == 0
        saved = restored["items"][0]
        assert saved["read_at"] == marked.json()["read_at"]
        assert saved["project"]["name"] == "Renamed project"
        assert saved["channel"]["name"] == "Renamed channel"
        assert saved["message"]["author"]["name"] == "Renamed author"
        assert saved["message"]["author"]["id"] == owner["actor"]["id"]
        assert saved["message"]["id"] == message["id"]
    restarted.state.engine.dispose()
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 1


def test_cross_project_notification_ids_commit_in_recipient_order(service):
    client, app, owner, _ = service
    first_pid, first_tid = setup_thread(client)
    second_pid, second_tid = setup_thread(client)
    target = recipient(client, [first_pid, second_pid])
    mention(client, first_tid, target, "Baseline")
    snapshot = inbox(client, target)["cursor"]
    waiting = Signal()
    sqlite = app.state.engine.dialect.name == "sqlite"

    def before_sql(connection, cursor, statement, parameters, context, many):
        if (sqlite and statement == "BEGIN IMMEDIATE") or (
            not sqlite and "FOR NO KEY UPDATE" in statement
        ):
            waiting.set()

    with app.state.session_factory() as db:
        if sqlite:
            db.execute(text("BEGIN IMMEDIATE"))
        project = db.scalar(select(Project).where(Project.id == first_pid).with_for_update())
        db.scalar(
            select(Actor).where(Actor.id == target["actor"]["id"]).with_for_update(key_share=True)
        )
        project.cursor += 1
        pending_message = Message(
            thread_id=first_tid,
            project_id=first_pid,
            author_id=owner["actor"]["id"],
            text="Pending lower notification ID",
            mentions=[target["actor"]["id"]],
            sequence=project.cursor,
        )
        db.add(pending_message)
        db.flush()
        pending_notification = Notification(
            actor_id=target["actor"]["id"], project_id=first_pid, message_id=pending_message.id
        )
        db.add(pending_notification)
        db.add(
            Event(
                project_id=first_pid,
                id=project.cursor,
                type="message.created",
                thread_id=first_tid,
                payload={"id": pending_message.id},
            )
        )
        db.flush()
        lower_id = pending_notification.id
        event.listen(app.state.engine, "before_cursor_execute", before_sql)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                later = pool.submit(mention, client, second_tid, target, "Later other project")
                assert waiting.wait(timeout=5)
                assert not later.done()
                assert inbox(client, target)["cursor"] == snapshot
                db.commit()
                assert later.result(timeout=10)["text"] == "Later other project"
        finally:
            event.remove(app.state.engine, "before_cursor_execute", before_sql)
    visible = inbox(client, target)
    assert visible["items"][0]["id"] > lower_id > snapshot
    assert client.post(
        "/v1/notifications/read-all", json={"through": lower_id}, headers=auth(target["token"])
    ).json() == {"updated": 2}
    assert (
        inbox(client, target, unread_only=True)["items"][0]["message"]["text"]
        == "Later other project"
    )
    # Reciprocal human mentions must not deadlock on author FK KEY SHARE locks.
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(mention, client, first_tid, target, "Owner to Alex")
        second = pool.submit(
            client.post,
            f"/v1/threads/{second_tid}/messages",
            json={
                "text": "Alex to owner",
                "mentions": [owner["actor"]["id"]],
            },
            headers=auth(target["token"]),
        )
        assert first.result(timeout=10)["text"] == "Owner to Alex"
        assert second.result(timeout=10).status_code == 201


def test_migration_retains_old_messages_without_backfilling_notifications(tmp_path, monkeypatch):
    from alembic.config import Config

    from agent_commons.db import make_engine, session_factory
    from agent_commons.security import issue_token
    from alembic import command

    url = f"sqlite:///{tmp_path}/notifications-migration.db"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0005")
    engine = make_engine(url)
    owner = {"actor": {"id": "00000000-0000-4000-8000-0000000000a1"}}
    with engine.begin() as connection:
        # The current ORM has columns that revision 0005 does not, so insert directly.
        for actor_id, name, handle, admin in (
            (owner["actor"]["id"], "Owner", "owner", 1),
            ("00000000-0000-4000-8000-0000000000a2", "Historical author", "historical-author", 0),
        ):
            connection.execute(
                text(
                    "INSERT INTO actors (id, name, handle, kind, is_admin) "
                    "VALUES (:id, :name, :handle, 'human', :admin)"
                ),
                {"id": actor_id, "name": name, "handle": handle, "admin": admin},
            )
    with session_factory(engine)() as db:
        author = SimpleNamespace(id="00000000-0000-4000-8000-0000000000a2")
        project = Project(name="Historical project", cursor=1)
        db.add(project)
        db.flush()
        author_token = issue_token(db, author)
        owner["token"] = issue_token(db, SimpleNamespace(id=owner["actor"]["id"]))
        channel = Channel(project_id=project.id, name="Historical channel")
        db.add(channel)
        db.flush()
        thread = Thread(project_id=project.id, channel_id=channel.id, title="Historical thread")
        db.add(thread)
        db.flush()
        tid = thread.id
        db.add_all(
            [
                Membership(project_id=project.id, actor_id=owner["actor"]["id"], role="owner"),
                Membership(project_id=project.id, actor_id=author.id),
            ]
        )
        db.add(
            Message(
                thread_id=tid,
                project_id=project.id,
                author_id=author.id,
                text="Old mention",
                mentions=[owner["actor"]["id"]],
                sequence=1,
            )
        )
        db.commit()
    command.upgrade(config, "head")
    with session_factory(engine)() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 1
        assert db.scalar(select(func.count()).select_from(Notification)) == 0
    migrated = create_app(url)
    with TestClient(migrated) as client:
        assert (
            client.post(
                f"/v1/threads/{tid}/messages",
                json={"text": "New mention", "mentions": [owner["actor"]["id"]]},
                headers=auth(author_token),
            ).status_code
            == 201
        )
        listed = client.get("/v1/notifications", headers=auth(owner["token"])).json()
        assert listed["items"][0]["id"] == 1 and listed["unread_count"] == 1
    migrated.state.engine.dispose()
    command.downgrade(config, "0005")
    with engine.connect() as db:
        assert db.execute(text("SELECT COUNT(*) FROM messages")).scalar() == 2
    engine.dispose()
