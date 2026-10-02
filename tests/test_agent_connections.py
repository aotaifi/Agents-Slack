from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select
from test_api import auth, setup_thread
from test_api import service as api_service

from agent_commons.models import AgentConnection, Membership, Token, now
from agent_commons.security import digest

service = api_service


def setup_agent(client, project_ids, name="Worker"):
    agent = client.post("/v1/actors", json={"name": name, "kind": "agent"}).json()
    for project_id in project_ids:
        assert (
            client.post(
                f"/v1/projects/{project_id}/members", json={"actor_id": agent["actor"]["id"]}
            ).status_code
            == 201
        )
    return agent


def connect(client, agent, project_id, *, headers=None, label="Claude session"):
    result = client.post(
        "/v1/agent-connections",
        json={"actor_id": agent["actor"]["id"], "project_id": project_id, "label": label},
        headers=headers,
    )
    assert result.status_code == 201, result.text
    return result.json()


def claim(client, connection, session_id="session-one"):
    return client.post(
        f"/v1/agent-connections/{connection['connection']['id']}/claim",
        json={"session_id": session_id},
        headers=auth(connection["token"]),
    )


def session_headers(connection, session_id="session-one"):
    return {**auth(connection["token"]), "X-Workspace-Session": session_id}


def test_connection_token_scope_reads_and_no_child_credentials(service):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    other_pid, other_tid = setup_thread(client)
    agent = setup_agent(client, [pid, other_pid])
    connection = connect(client, agent, pid)
    headers = auth(connection["token"])
    assert connection["connection"]["project"] == {"id": pid, "name": "Physics"}
    assert not connection["connection"]["bound"] and not connection["connection"]["active"]
    me = client.get("/v1/me", headers=headers).json()
    assert me["connection"] == connection["connection"]
    assert client.get("/v1/projects", headers=headers).json()["items"] == [
        client.get(f"/v1/projects/{pid}").json()
    ]
    assert client.get(f"/v1/threads/{tid}/messages", headers=headers).status_code == 200
    for path in (
        f"/v1/projects/{other_pid}",
        f"/v1/projects/{other_pid}/events",
        f"/v1/projects/{other_pid}/inbox",
        f"/v1/threads/{other_tid}/context",
        f"/v1/threads/{other_tid}/messages",
    ):
        assert client.get(path, headers=headers).status_code == 404
    assert (
        client.post(
            "/v1/agent-connections", json={"project_id": pid, "label": "Child"}, headers=headers
        ).status_code
        == 403
    )
    assert client.get("/v1/agent-connections", headers=headers).status_code == 403
    second = connect(client, agent, pid)
    assert (
        client.get(
            f"/v1/agent-connections/{second['connection']['id']}", headers=headers
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/v1/agent-connections/{connection['connection']['id']}", headers=headers
        ).json()
        == connection["connection"]
    )
    with app.state.session_factory() as db:
        credential = db.get(Token, digest(connection["token"]))
        assert credential.connection_id == connection["connection"]["id"]
        assert credential.actor_id == agent["actor"]["id"]
        assert credential.digest != connection["token"]
    invitation = client.post(f"/v1/projects/{pid}/invitations", json={}).json()
    for endpoint in ("preview", "accept"):
        assert (
            client.post(
                f"/v1/invitations/{endpoint}", json={"code": invitation["code"]}, headers=headers
            ).status_code
            == 403
        )


def test_claim_binding_renewal_and_all_project_write_guards(service):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    agent = setup_agent(client, [pid])
    connection = connect(client, agent, pid)
    path = f"/v1/threads/{tid}/messages"
    assert (
        client.post(path, json={"text": "No lease"}, headers=auth(connection["token"])).status_code
        == 403
    )
    first = claim(client, connection)
    assert first.status_code == 200 and first.json()["connection"]["bound"]
    assert first.json()["connection"]["active"] and first.json()["connection"]["last_seen_at"]
    assert "session_digest" not in first.text and "session-one" not in first.text
    assert claim(client, connection, "session-two").status_code == 409
    for headers in (auth(connection["token"]), session_headers(connection, "other")):
        assert client.post(path, json={"text": "Mismatch"}, headers=headers).status_code == 403
    correct = session_headers(connection)
    message = client.post(path, json={"text": "Claimed"}, headers=correct)
    assert message.status_code == 201
    reaction_path = f"/v1/messages/{message.json()['id']}/reactions"
    assert (
        client.put(
            reaction_path, json={"emoji": "👍"}, headers=auth(connection["token"])
        ).status_code
        == 403
    )
    assert client.put(reaction_path, json={"emoji": "👍"}, headers=correct).status_code == 200
    assert (
        client.request("DELETE", reaction_path, json={"emoji": "👍"}, headers=correct).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/projects/{pid}/channels", json={"name": "Scoped"}, headers=correct
        ).status_code
        == 201
    )
    renewed = claim(client, connection)
    assert renewed.status_code == 200
    assert (
        renewed.json()["connection"]["lease_expires_at"]
        >= first.json()["connection"]["lease_expires_at"]
    )
    with app.state.session_factory() as db:
        saved = db.get(AgentConnection, connection["connection"]["id"])
        assert saved.session_digest == digest("session-one")
        saved.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.post(path, json={"text": "Expired"}, headers=correct).status_code == 403
    assert claim(client, connection, "new-session-after-expiry").status_code == 409
    assert claim(client, connection).status_code == 200


def test_competing_leases_are_atomic_and_release_keeps_permanent_binding(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    agent = setup_agent(client, [pid])
    first, second = connect(client, agent, pid), connect(client, agent, pid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda item: claim(client, item), [first, second]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = first if responses[0].status_code == 200 else second
    loser = second if winner is first else first
    release_path = f"/v1/agent-connections/{winner['connection']['id']}/release"
    assert (
        client.post(
            release_path, json={"session_id": "wrong"}, headers=auth(winner["token"])
        ).status_code
        == 409
    )
    for _ in range(2):
        released = client.post(
            release_path, json={"session_id": "session-one"}, headers=auth(winner["token"])
        )
        assert released.status_code == 200
        assert (
            released.json()["connection"]["bound"] and not released.json()["connection"]["active"]
        )
    assert claim(client, winner, "new").status_code == 409
    assert claim(client, loser).status_code == 200
    assert claim(client, winner).status_code == 409
    with app.state.session_factory() as db:
        db.get(AgentConnection, loser["connection"]["id"]).lease_expires_at = now() - timedelta(
            seconds=1
        )
        db.commit()
    assert claim(client, winner).status_code == 200


def test_creation_ownership_membership_and_visibility(service):
    client, app, owner, _ = service
    pid, _ = setup_thread(client)
    agent = setup_agent(client, [pid])
    human = client.post("/v1/actors", json={"name": "Other human", "kind": "human"}).json()
    other_agent = client.post(
        "/v1/actors",
        json={"name": "Owned elsewhere", "kind": "agent"},
        headers=auth(human["token"]),
    ).json()
    client.post(f"/v1/projects/{pid}/members", json={"actor_id": human["actor"]["id"]})
    client.post(f"/v1/projects/{pid}/members", json={"actor_id": other_agent["actor"]["id"]})
    body = {"actor_id": agent["actor"]["id"], "project_id": pid, "label": "Session"}
    assert (
        client.post("/v1/agent-connections", json=body, headers=auth(human["token"])).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/agent-connections", json={**body, "actor_id": owner["actor"]["id"]}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/agent-connections",
            json={**body, "actor_id": other_agent["actor"]["id"]},
            headers=auth(agent["token"]),
        ).status_code
        == 403
    )
    assert client.post("/v1/agent-connections", json={**body, "label": " "}).status_code == 422
    connection = connect(client, agent, pid)
    theirs = connect(client, other_agent, pid, headers=auth(human["token"]))
    assert [row["id"] for row in client.get("/v1/agent-connections").json()["items"]] == [
        connection["connection"]["id"]
    ]
    assert client.get(f"/v1/agent-connections/{theirs['connection']['id']}").status_code == 404
    assert client.delete(f"/v1/agent-connections/{theirs['connection']['id']}").status_code == 404
    other_pid, _ = setup_thread(client)
    assert (
        client.post("/v1/agent-connections", json={**body, "project_id": other_pid}).status_code
        == 404
    )
    with app.state.session_factory() as db:
        db.delete(db.get(Membership, (pid, owner["actor"]["id"])))
        db.commit()
    assert client.get("/v1/agent-connections", params={"project_id": pid}).json()["items"] == []
    assert client.get(f"/v1/agent-connections/{connection['connection']['id']}").status_code == 404
    assert client.post("/v1/agent-connections", json=body).status_code == 404


def test_moderation_release_membership_removal_and_revoke(service):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    agent = setup_agent(client, [pid])
    connection = connect(client, agent, pid)
    assert claim(client, connection).status_code == 200
    membership = f"/v1/projects/{pid}/members/{agent['actor']['id']}"
    assert client.patch(membership, json={"muted": True}).status_code == 200
    assert claim(client, connection).status_code == 403
    assert (
        client.get(f"/v1/threads/{tid}/messages", headers=auth(connection["token"])).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/threads/{tid}/messages",
            json={"text": "Muted"},
            headers=session_headers(connection),
        ).status_code
        == 403
    )
    # A muted member can still relinquish the lease.
    assert (
        client.post(
            f"/v1/agent-connections/{connection['connection']['id']}/release",
            json={"session_id": "session-one"},
            headers=auth(connection["token"]),
        ).status_code
        == 200
    )
    assert client.patch(membership, json={"muted": False}).status_code == 200
    assert claim(client, connection).status_code == 200
    assert (
        client.delete(
            f"/v1/agent-connections/{connection['connection']['id']}", headers=auth(agent["token"])
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/v1/agent-connections/{connection['connection']['id']}").status_code == 204
    )
    assert client.get("/v1/me", headers=auth(connection["token"])).status_code == 401
    assert client.get("/v1/me", headers=auth(agent["token"])).status_code == 200
    with app.state.session_factory() as db:
        saved = db.get(AgentConnection, connection["connection"]["id"])
        assert saved.revoked and saved.lease_expires_at is None and saved.session_digest
        assert db.get(Token, digest(connection["token"])).revoked
    next_connection = connect(client, agent, pid)
    assert claim(client, next_connection).status_code == 200
    assert client.delete(membership).status_code == 204
    assert claim(client, next_connection).status_code == 404
    assert (
        client.get(f"/v1/projects/{pid}/events", headers=auth(next_connection["token"])).status_code
        == 404
    )


def test_actor_revoke_closes_every_connection_without_changing_unscoped_protocol(service):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    other_pid, other_tid = setup_thread(client)
    agent = setup_agent(client, [pid, other_pid])
    # An old unscoped agent can still mint its own connection or write without a lease.
    created = client.post(
        "/v1/agent-connections",
        json={"project_id": pid, "label": "Self-created"},
        headers=auth(agent["token"]),
    )
    assert created.status_code == 201
    first = created.json()
    second = connect(client, agent, other_pid)
    assert claim(client, first).status_code == 200
    assert claim(client, second).status_code == 200
    for thread in (tid, other_tid):
        assert (
            client.post(
                f"/v1/threads/{thread}/messages",
                json={"text": "Legacy unscoped"},
                headers=auth(agent["token"]),
            ).status_code
            == 201
        )
    assert len(client.get("/v1/projects", headers=auth(agent["token"])).json()["items"]) == 2
    assert "connection" not in client.get("/v1/me", headers=auth(agent["token"])).json()
    assert client.post(f"/v1/actors/{agent['actor']['id']}/revoke").status_code == 204
    for token in (first["token"], second["token"], agent["token"]):
        assert client.get("/v1/me", headers=auth(token)).status_code == 401
    with app.state.session_factory() as db:
        rows = list(
            db.scalars(
                select(AgentConnection).where(AgentConnection.actor_id == agent["actor"]["id"])
            )
        )
        assert len(rows) == 2 and all(row.revoked and row.lease_expires_at is None for row in rows)


def test_waiting_connection_creation_cannot_replace_a_revoked_source_credential(service):
    from threading import Event as Signal

    from sqlalchemy import event, func, text, update

    from agent_commons.security import credential_lock_key

    client, app, _, _ = service
    pid, _ = setup_thread(client)
    agent = setup_agent(client, [pid])
    waiting = Signal()
    sqlite = app.state.engine.dialect.name == "sqlite"

    def before_sql(connection, cursor, statement, parameters, context, many):
        if (sqlite and statement == "BEGIN IMMEDIATE") or (
            not sqlite and "pg_advisory_xact_lock" in statement
        ):
            waiting.set()

    with app.state.session_factory() as db:
        if sqlite:
            db.execute(text("BEGIN IMMEDIATE"))
        else:
            db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": credential_lock_key(agent["actor"]["id"])},
            )
        event.listen(app.state.engine, "before_cursor_execute", before_sql)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    client.post,
                    "/v1/agent-connections",
                    json={"project_id": pid, "label": "Queued creation"},
                    headers=auth(agent["token"]),
                )
                assert waiting.wait(timeout=5)
                db.execute(
                    update(Token).where(Token.actor_id == agent["actor"]["id"]).values(revoked=True)
                )
                db.commit()
                result = pending.result(timeout=10)
            assert result.status_code == 401
        finally:
            event.remove(app.state.engine, "before_cursor_execute", before_sql)
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentConnection)) == 0
