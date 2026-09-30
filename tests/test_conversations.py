import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_api import auth, setup_thread
from test_api import service as api_service

from agent_commons.cli import bootstrap
from agent_commons.db import make_engine
from agent_commons.main import create_app, timestamp

service = api_service


def add_agent(client, pid, name="Worker"):
    result = client.post("/v1/actors", json={"name": name, "kind": "agent"}).json()
    assert (
        client.post(
            f"/v1/projects/{pid}/members", json={"actor_id": result["actor"]["id"]}
        ).status_code
        == 201
    )
    return result


def test_handles_owner_namespace_and_collisions(service):
    client, _, owner, _ = service
    assert owner["actor"]["handle"] == "owner"
    assert owner["actor"]["owner"] is None
    first = client.post("/v1/actors", json={"name": "Assistant", "kind": "agent"}).json()["actor"]
    second = client.post("/v1/actors", json={"name": "Assistant", "kind": "agent"}).json()["actor"]
    assert first["handle"] == "owner.assistant"
    assert second["handle"] == "owner.assistant-2"
    assert first["owner"] == {k: owner["actor"][k] for k in ("id", "name", "handle")}
    assert first["owner_id"] == owner["actor"]["id"]
    supplied = {"name": "Custom", "kind": "agent", "handle": "owner.assistant"}
    assert client.post("/v1/actors", json=supplied).status_code == 409
    for handle in ("Uppercase", "other.agent", "invalid_underscore", "-bad"):
        assert client.post("/v1/actors", json={**supplied, "handle": handle}).status_code == 422
    human = client.post("/v1/actors", json={"name": "Owner", "kind": "human"}).json()["actor"]
    assert human["handle"] == "owner-2"
    assert (
        client.post(
            "/v1/actors", json={"name": "Exact", "kind": "human", "handle": "owner"}
        ).status_code
        == 409
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: client.post("/v1/actors", json={"name": "Twin", "kind": "agent"}),
                range(4),
            )
        )
    assert all(result.status_code == 201 for result in results)
    assert {result.json()["actor"]["handle"] for result in results} == {
        "owner.twin",
        "owner.twin-2",
        "owner.twin-3",
        "owner.twin-4",
    }


def test_reply_validation_counts_and_legacy(service):
    client, _, _, _ = service
    _, tid = setup_thread(client)
    _, other_tid = setup_thread(client)
    path = f"/v1/threads/{tid}/messages"
    parent = client.post(path, json={"text": "Parent"}).json()
    reply = client.post(path, json={"text": "Reply", "reply_to": parent["id"]})
    assert reply.status_code == 201 and reply.json()["reply_to"] == parent["id"]
    retry = client.post(
        path,
        json={"text": "Legacy reply", "metadata": {"reply_to": parent["id"]}},
        headers={"Idempotency-Key": "legacy"},
    )
    assert retry.status_code == 201 and retry.json()["reply_to"] == parent["id"]
    assert (
        client.post(
            path,
            json={"text": "Legacy reply", "metadata": {"reply_to": parent["id"]}},
            headers={"Idempotency-Key": "legacy"},
        ).status_code
        == 200
    )
    assert (
        client.post(path, json={"text": "Nested", "reply_to": reply.json()["id"]}).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/threads/{other_tid}/messages", json={"text": "Cross", "reply_to": parent["id"]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            path,
            json={"text": "Conflict", "reply_to": None, "metadata": {"reply_to": parent["id"]}},
        ).status_code
        == 422
    )
    assert (
        client.post(path, json={"text": "Invalid", "metadata": {"reply_to": "invalid"}}).status_code
        == 422
    )
    messages = client.get(path).json()["items"]
    assert messages[0]["reply_count"] == 2
    assert messages[1]["reply_count"] == 0
    incremental = client.get(path, params={"after": parent["sequence"], "limit": 1}).json()
    assert incremental["items"][0]["id"] == reply.json()["id"]
    assert incremental["next_cursor"] == reply.json()["sequence"]


def test_reaction_access_idempotency_events_and_rate(service):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    worker = add_agent(client, pid)
    outsider = client.post("/v1/actors", json={"name": "Outside", "kind": "agent"}).json()
    message = client.post(f"/v1/threads/{tid}/messages", json={"text": "React"}).json()
    path = f"/v1/messages/{message['id']}/reactions"
    assert (
        client.put(path, json={"emoji": "👍"}, headers=auth(outsider["token"])).status_code == 404
    )
    assert client.put(path, json={"emoji": "🚀"}).status_code == 422
    before = client.get(f"/v1/projects/{pid}/events").json()["cursor"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: client.put(path, json={"emoji": "👍"}, headers=auth(worker["token"])),
                range(6),
            )
        )
    assert all(r.status_code == 200 for r in results)
    assert all(r.json()["reactions"][0]["count"] == 1 for r in results)
    events = client.get(f"/v1/projects/{pid}/events", params={"after": before}).json()["items"]
    assert [event["type"] for event in events] == ["reaction.added"]
    assert events[0]["payload"]["actor"]["handle"] == "owner.worker"
    app.state.posting_limit = 1
    # Repeating does not consume or require a fresh rate allowance.
    assert client.put(path, json={"emoji": "👍"}, headers=auth(worker["token"])).status_code == 200
    assert client.put(path, json={"emoji": "✅"}, headers=auth(worker["token"])).status_code == 429
    app.state.posting_limit = 60
    assert client.put(path, json={"emoji": "👍"}).json()["reactions"][0]["count"] == 2
    member_url = f"/v1/projects/{pid}/members/{worker['actor']['id']}"
    assert client.patch(member_url, json={"muted": True}).status_code == 200
    assert (
        client.request(
            "DELETE", path, json={"emoji": "👍"}, headers=auth(worker["token"])
        ).status_code
        == 403
    )
    assert client.patch(member_url, json={"muted": False}).status_code == 200
    before = client.get(f"/v1/projects/{pid}/events").json()["cursor"]
    for _ in range(2):
        removed = client.request(
            "DELETE", path, json={"emoji": "👍"}, headers=auth(worker["token"])
        )
        assert removed.status_code == 200 and removed.json()["reactions"][0]["count"] == 1
    events = client.get(f"/v1/projects/{pid}/events", params={"after": before}).json()["items"]
    assert [event["type"] for event in events] == ["reaction.removed"]
    assert client.delete(member_url).status_code == 204
    assert client.put(path, json={"emoji": "👀"}, headers=auth(worker["token"])).status_code == 404


def test_inbox_filter_scan_and_cursor_pages(service):
    client, _, _, _ = service
    pid, tid = setup_thread(client)
    worker = add_agent(client, pid)
    headers = auth(worker["token"])
    path = f"/v1/threads/{tid}/messages"
    ignored = client.post(path, json={"text": "Ignore"}).json()
    mention = client.post(
        path, json={"text": "Mention", "mentions": [worker["actor"]["id"]]}
    ).json()
    client.put(f"/v1/messages/{mention['id']}/reactions", json={"emoji": "👀"})
    self_message = client.post(
        path, json={"text": "Self", "mentions": [worker["actor"]["id"]]}, headers=headers
    ).json()
    second = client.post(path, json={"text": "Next", "mentions": [worker["actor"]["id"]]}).json()
    inbox_path = f"/v1/projects/{pid}/inbox"
    first = client.get(inbox_path, params={"limit": 1}, headers=headers).json()
    assert [e["payload"]["id"] for e in first["items"]] == [mention["id"]]
    assert first["next_cursor"] == mention["sequence"]
    next_page = client.get(
        inbox_path, params={"after": first["next_cursor"], "limit": 1}, headers=headers
    ).json()
    assert [e["payload"]["id"] for e in next_page["items"]] == [second["id"]]
    assert next_page["next_cursor"] is None
    assert next_page["cursor"] == second["sequence"]
    followed = client.get(inbox_path, params=[("followed_thread_ids", tid)], headers=headers).json()
    assert [e["payload"]["id"] for e in followed["items"]] == [
        ignored["id"],
        mention["id"],
        second["id"],
    ]
    assert self_message["id"] not in str(followed)
    _, other_tid = setup_thread(client)
    assert (
        client.get(
            inbox_path, params={"followed_thread_ids": other_tid}, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.get(
            inbox_path, params={"followed_thread_ids": "invalid"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.get(inbox_path, params={"after": followed["cursor"]}, headers=headers).json()[
            "items"
        ]
        == []
    )


def test_context_recent_trigger_parent_budget_access_and_timezone(service):
    client, _, _, _ = service
    pid, tid = setup_thread(client)
    path = f"/v1/threads/{tid}/messages"
    parent = client.post(path, json={"text": "p" * 2000}).json()
    trigger = client.post(path, json={"text": "t" * 2000, "reply_to": parent["id"]}).json()
    recent = [client.post(path, json={"text": str(i) * 2000}).json() for i in range(4)]
    assert client.put(f"/v1/projects/{pid}/rules", json={"text": "r" * 3000}).status_code == 200
    context_path = f"/v1/threads/{tid}/context"
    context = client.get(
        context_path, params={"limit": 2, "trigger_message_id": trigger["id"], "max_chars": 1000}
    ).json()
    assert [m["id"] for m in context["messages"]] == [m["id"] for m in recent[-2:]]
    assert context["has_older"]
    assert context["trigger_message"]["id"] == trigger["id"]
    assert context["parent_message"]["id"] == parent["id"]
    values = [
        context["rules"],
        context["trigger_message"],
        context["parent_message"],
        *context["messages"],
    ]
    assert sum(len(item["text"]) for item in values) <= 1000
    assert all(item["truncated"] for item in values)
    assert client.get(context_path).json()["trigger_message"] is None
    assert not client.get(context_path).json()["has_older"]
    assert client.get(context_path, params={"max_chars": 999}).status_code == 422
    assert client.get(context_path, params={"limit": 101}).status_code == 422
    _, other_tid = setup_thread(client)
    assert (
        client.get(
            f"/v1/threads/{other_tid}/context", params={"trigger_message_id": trigger["id"]}
        ).status_code
        == 404
    )
    outsider = client.post("/v1/actors", json={"name": "Outside", "kind": "agent"}).json()
    assert client.get(context_path, headers=auth(outsider["token"])).status_code == 404
    offset = datetime(2026, 9, 30, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert timestamp(offset) == "2026-09-30T12:00:00+00:00"


def test_upgrade_existing_handles_replies_and_old_idempotency(tmp_path, monkeypatch):
    from alembic.config import Config

    from alembic import command

    url = f"sqlite:///{tmp_path}/upgrade.db"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0001")
    engine = make_engine(url)
    actor_id = "00000000-0000-0000-0000-000000000001"
    worker_id = "00000000-0000-0000-0000-000000000002"
    parent_id = "00000000-0000-0000-0000-000000000003"
    reply_id = "00000000-0000-0000-0000-000000000004"
    from agent_commons.security import digest

    invalid_legacy = [
        ("bad-key", "00000000-0000-0000-0000-000000000005", "malformed"),
        (
            "missing-key",
            "00000000-0000-0000-0000-000000000006",
            "99999999-9999-9999-9999-999999999999",
        ),
        ("nested-key", "00000000-0000-0000-0000-000000000007", reply_id),
    ]
    request = {"text": "Old", "mentions": [], "metadata": {}}
    fingerprint = hashlib.sha256(
        json.dumps(
            {"thread_id": "thread", **request}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    with engine.begin() as db:
        for aid, kind, owner in ((actor_id, "human", None), (worker_id, "agent", actor_id)):
            db.execute(
                text(
                    "INSERT INTO actors(id,name,kind,owner_id,is_admin) "
                    "VALUES (:id,'Same',:kind,:owner,false)"
                ),
                {"id": aid, "kind": kind, "owner": owner},
            )
        db.execute(
            text("INSERT INTO tokens(digest,actor_id,revoked) VALUES (:digest,:id,false)"),
            {"digest": digest("old-token"), "id": actor_id},
        )
        db.execute(text("INSERT INTO projects VALUES ('project','Old','','',0,5)"))
        db.execute(
            text("INSERT INTO memberships VALUES ('project',:id,'owner',false)"), {"id": actor_id}
        )
        db.execute(text("INSERT INTO channels VALUES ('channel','project','Old','')"))
        db.execute(
            text(
                "INSERT INTO threads VALUES "
                "('thread','channel','project','Old','2026-09-30 00:00:00')"
            )
        )
        for mid, sequence, metadata in [
            (parent_id, 1, {}),
            (reply_id, 2, {"reply_to": parent_id}),
            *[
                (mid, seq, {"reply_to": ref})
                for seq, (_, mid, ref) in enumerate(invalid_legacy, start=3)
            ],
        ]:
            db.execute(
                text(
                    "INSERT INTO messages VALUES "
                    "(:id,'thread','project',:actor,'Old','[]',:metadata,:sequence,"
                    "'2026-09-30 00:00:00')"
                ),
                {
                    "id": mid,
                    "actor": actor_id,
                    "metadata": json.dumps(metadata),
                    "sequence": sequence,
                },
            )
            payload = {
                "id": mid,
                "thread_id": "thread",
                "author": {"id": actor_id, "name": "Same", "kind": "human", "owner_id": None},
                "mentions": [],
                "metadata": {"id": {"arbitrary": True}},
            }
            db.execute(
                text(
                    "INSERT INTO events VALUES "
                    "('project',:sequence,'message.created','thread',:payload,"
                    "'2026-09-30 00:00:00')"
                ),
                {"sequence": sequence, "payload": json.dumps(payload)},
            )
        db.execute(
            text(
                "INSERT INTO idempotency VALUES (:actor,'project','old-key',:fingerprint,:message)"
            ),
            {"actor": actor_id, "fingerprint": fingerprint, "message": parent_id},
        )
        for key, mid, ref in invalid_legacy:
            legacy_body = {**request, "metadata": {"reply_to": ref}}
            legacy_fingerprint = hashlib.sha256(
                json.dumps(
                    {"thread_id": "thread", **legacy_body}, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            db.execute(
                text(
                    "INSERT INTO idempotency VALUES (:actor,'project',:key,:fingerprint,:message)"
                ),
                {"actor": actor_id, "key": key, "fingerprint": legacy_fingerprint, "message": mid},
            )
    command.upgrade(config, "head")
    with engine.connect() as db:
        handles = dict(db.execute(text("SELECT id,handle FROM actors")).all())
        assert handles == {actor_id: "same", worker_id: "same.same"}
        assert (
            db.execute(
                text("SELECT reply_to FROM messages WHERE id=:id"), {"id": reply_id}
            ).scalar()
            == parent_id
        )
    app = create_app(url)
    with TestClient(app) as client:
        client.headers.update(auth("old-token"))
        result = client.post(
            "/v1/threads/thread/messages", json=request, headers={"Idempotency-Key": "old-key"}
        )
        assert result.status_code == 200 and result.json()["id"] == parent_id
        assert result.json()["reply_count"] == 1
        for key, mid, ref in invalid_legacy:
            legacy_body = {**request, "metadata": {"reply_to": ref}}
            retried = client.post(
                "/v1/threads/thread/messages", json=legacy_body, headers={"Idempotency-Key": key}
            )
            assert retried.status_code == 200 and retried.json()["id"] == mid
            assert client.post("/v1/threads/thread/messages", json=legacy_body).status_code == 422
        events = client.get("/v1/projects/project/events").json()["items"]
        assert events[0]["payload"]["author"]["handle"] == "same"
        assert events[1]["payload"]["reply_to"] == parent_id
    app.state.engine.dispose()
    engine.dispose()
    with pytest.raises(ValueError, match="already exists"):
        bootstrap("Again", database_url=url)


def test_inbox_empty_bounded_scan_advances_without_losing_matches(service):
    from sqlalchemy import select

    from agent_commons.models import Event, Project

    client, app, _, _ = service
    pid, tid = setup_thread(client)
    worker = add_agent(client, pid)
    baseline = client.get(f"/v1/projects/{pid}/events").json()["cursor"]
    with app.state.session_factory() as db:
        project = db.scalar(select(Project).where(Project.id == pid).with_for_update())
        for _ in range(1001):
            project.cursor += 1
            db.add(Event(project_id=pid, id=project.cursor, type="rules.updated", payload={}))
        db.commit()
    matched = client.post(
        f"/v1/threads/{tid}/messages",
        json={"text": "After the ignored page", "mentions": [worker["actor"]["id"]]},
    ).json()
    path = f"/v1/projects/{pid}/inbox"
    first = client.get(path, params={"after": baseline}, headers=auth(worker["token"])).json()
    assert first["items"] == []
    assert first["next_cursor"] == baseline + 1000
    assert first["cursor"] == matched["sequence"]
    second = client.get(
        path, params={"after": first["next_cursor"]}, headers=auth(worker["token"])
    ).json()
    assert [event["payload"]["id"] for event in second["items"]] == [matched["id"]]
    assert second["next_cursor"] is None
