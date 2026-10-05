from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import auth, setup_thread
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_commons.cli import bootstrap
from agent_commons.db import Base, make_engine
from agent_commons.main import create_app
from agent_commons.models import Token


def test_isolation_revocation_and_moderation(service):
    client, app, owner, _ = service
    pid, tid = setup_thread(client)
    agent = client.post("/v1/actors", json={"name": "Assistant", "kind": "agent"}).json()
    stranger = client.post("/v1/actors", json={"name": "Stranger", "kind": "human"}).json()
    headers = auth(agent["token"])
    assert client.get(f"/v1/projects/{pid}/events", headers=headers).status_code == 404
    assert client.get(f"/v1/threads/{tid}", headers=headers).status_code == 404
    assert client.get(f"/v1/projects/{pid}", headers=auth(stranger["token"])).status_code == 404
    assert (
        client.post(
            f"/v1/projects/{pid}/members", json={"actor_id": agent["actor"]["id"], "role": "owner"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/projects/{pid}/members", json={"actor_id": agent["actor"]["id"]}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/actors", json={"name": "nested", "kind": "agent"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/threads/{tid}/messages", json={"text": "Ready"}, headers=headers
        ).status_code
        == 201
    )
    member_url = f"/v1/projects/{pid}/members/{agent['actor']['id']}"
    assert client.patch(member_url, json={"muted": True}).status_code == 200
    assert (
        client.post(
            f"/v1/threads/{tid}/messages", json={"text": "Muted"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/projects/{pid}/channels", json={"name": "Hidden"}, headers=headers
        ).status_code
        == 403
    )
    assert client.get(f"/v1/threads/{tid}/messages", headers=headers).status_code == 200
    assert (
        client.patch(
            f"/v1/projects/{pid}/members/{owner['actor']['id']}", json={"muted": True}
        ).status_code
        == 403
    )
    assert client.delete(f"/v1/projects/{pid}/members/{owner['actor']['id']}").status_code == 409
    assert client.delete(member_url).status_code == 204
    assert client.get(f"/v1/projects/{pid}/events", headers=headers).status_code == 404
    assert client.post(f"/v1/actors/{agent['actor']['id']}/revoke").status_code == 204
    assert client.get("/v1/me", headers=headers).status_code == 401
    with app.state.session_factory() as db:
        tokens = list(db.scalars(select(Token)))
        assert all(x.digest != agent["token"] for x in tokens)
        assert all(len(x.digest) == 64 for x in tokens)


def test_cursor_idempotency_persistence_and_rate_limit(service):
    client, app, owner, url = service
    pid, tid = setup_thread(client)
    path = f"/v1/threads/{tid}/messages"
    body = {"text": "Result", "mentions": [owner["actor"]["id"]], "metadata": {"run": 42}}
    first = client.post(path, json=body, headers={"Idempotency-Key": "run-42"})
    assert first.status_code == 201
    retry = client.post(path, json=body, headers={"Idempotency-Key": "run-42"})
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert (
        client.post(
            path, json={"text": "Different"}, headers={"Idempotency-Key": "run-42"}
        ).status_code
        == 409
    )
    assert client.post(path, json={"text": "  "}).status_code == 422
    assert client.post(path, json={"text": "Oops", "author_id": "spoof"}).status_code == 422
    assert client.post(path, json={"text": "Oops", "mentions": ["unknown"]}).status_code == 422
    assert (
        client.post(path, json={"text": "Oops", "metadata": {"x": "x" * 8192}}).status_code == 422
    )
    app.state.posting_limit = 2
    second = client.post(path, json={"text": "Second"})
    assert second.status_code == 201
    blocked = client.post(path, json={"text": "Third"})
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    assert client.post(path, json=body, headers={"Idempotency-Key": "run-42"}).status_code == 200
    assert client.put(f"/v1/projects/{pid}/rules", json={"text": "Be precise"}).status_code == 200
    events, after = [], 0
    while True:
        page = client.get(f"/v1/projects/{pid}/events", params={"after": after, "limit": 2}).json()
        events.extend(page["items"])
        if page["next_cursor"] is None:
            break
        after = page["next_cursor"]
    assert [e["id"] for e in events] == list(range(1, page["cursor"] + 1))
    message_events = [e for e in events if e["type"] == "message.created"]
    assert len(message_events) == 2
    assert first.json()["sequence"] == message_events[0]["id"]
    assert first.json()["id"] == message_events[0]["payload"]["id"]
    assert owner["token"] not in str(events)
    assert (
        client.get(f"/v1/projects/{pid}/events", params={"after": page["cursor"]}).json()["items"]
        == []
    )
    first_page = client.get(path, params={"limit": 1}).json()
    assert first_page["next_cursor"] == first.json()["sequence"]
    assert (
        client.get(path, params={"after": first_page["next_cursor"]}).json()["items"][0]["id"]
        == second.json()["id"]
    )
    restarted = create_app(url)
    with TestClient(restarted) as other:
        other.headers.update(auth(owner["token"]))
        assert len(other.get(path).json()["items"]) == 2
        assert (
            other.post(path, json=body, headers={"Idempotency-Key": "run-42"}).json()
            == first.json()
        )
    restarted.state.engine.dispose()


def test_concurrent_idempotent_writes(service):
    client, _, _, _ = service
    pid, tid = setup_thread(client)
    path = f"/v1/threads/{tid}/messages"
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: client.post(
                    path, json={"text": "Same"}, headers={"Idempotency-Key": "concurrent"}
                ),
                range(6),
            )
        )
    assert sorted(r.status_code for r in results) == [200] * 5 + [201]
    assert len({r.json()["id"] for r in results}) == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda i: client.post(path, json={"text": f"Result {i}"}), range(6))
        )
    assert all(r.status_code == 201 for r in results)
    messages = client.get(path).json()["items"]
    assert len(messages) == 7
    assert len({m["sequence"] for m in messages}) == 7
    events = client.get(f"/v1/projects/{pid}/events").json()
    assert [e["id"] for e in events["items"]] == list(range(1, events["cursor"] + 1))


def test_bootstrap_once_private_file_and_no_api(service, tmp_path):
    client, _, _, url = service
    with pytest.raises(ValueError, match="already exists"):
        bootstrap("Another admin", database_url=url)
    assert client.post("/v1/bootstrap", json={"name": "Attacker"}).status_code == 404
    new_url = f"sqlite:///{tmp_path}/new.db"
    engine = make_engine(new_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    output = tmp_path / "credentials.json"
    result = bootstrap("Initial owner", output, new_url)
    assert output.stat().st_mode & 0o777 == 0o600
    assert result["token"] in output.read_text()
    with pytest.raises(ValueError, match="already exists"):
        bootstrap("Again", database_url=new_url)


def test_pending_writer_cannot_advance_visible_cursor(service):
    from threading import Event as Signal

    from sqlalchemy import text

    from agent_commons.models import Event, Project

    client, app, _, _ = service
    pid, tid = setup_thread(client)
    snapshot = client.get(f"/v1/projects/{pid}/events").json()["cursor"]
    with app.state.session_factory() as transaction:
        if app.state.engine.dialect.name == "sqlite":
            transaction.execute(text("BEGIN IMMEDIATE"))
        p = transaction.scalar(select(Project).where(Project.id == pid).with_for_update())
        p.cursor += 1
        transaction.add(
            Event(
                project_id=pid,
                id=p.cursor,
                type="rules.updated",
                payload={"text": "pending", "version": 1},
            )
        )
        transaction.flush()
        started = Signal()

        def post():
            started.set()
            return client.post(f"/v1/threads/{tid}/messages", json={"text": "Queued"})

        with ThreadPoolExecutor(max_workers=1) as pool:
            queued = pool.submit(post)
            assert started.wait(timeout=5)
            # Neither the pending counter nor later writer can become a checkpoint.
            replay = client.get(f"/v1/projects/{pid}/events", params={"after": snapshot}).json()
            assert replay["cursor"] == snapshot and replay["items"] == []
            transaction.commit()
            result = queued.result(timeout=10)
        assert result.status_code == 201
        assert result.json()["sequence"] == snapshot + 2
    replay = client.get(f"/v1/projects/{pid}/events", params={"after": snapshot}).json()
    assert [e["id"] for e in replay["items"]] == [snapshot + 1, snapshot + 2]


def test_migration_round_trip(tmp_path, monkeypatch):
    from alembic.config import Config
    from sqlalchemy import inspect

    from alembic import command

    url = f"sqlite:///{tmp_path}/migration.db"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = make_engine(url)
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    result = bootstrap("Migrated owner", database_url=url)
    migrated = create_app(url)
    with TestClient(migrated) as client:
        client.headers.update(auth(result["token"]))
        _, tid = setup_thread(client)
        assert (
            client.post(f"/v1/threads/{tid}/messages", json={"text": "Migrated"}).status_code == 201
        )
    migrated.state.engine.dispose()
    command.downgrade(config, "base")
    assert not set(Base.metadata.tables) & set(inspect(engine).get_table_names())
    engine.dispose()
