import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from conftest import auth, setup_thread
from sqlalchemy import func, select

from agent_commons.models import Actor, Invitation, Membership, now
from agent_commons.security import digest

ANONYMOUS = {"Authorization": ""}


def invite(client, pid, role="guest", headers=None):
    response = client.post(f"/v1/projects/{pid}/invitations", json={"role": role}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def accept(client, invitation, name="Alex", headers=ANONYMOUS, **extra):
    return client.post(
        "/v1/invitations/accept",
        json={
            "code": invitation["code"],
            "claim_secret": secrets.token_urlsafe(32),
            **({"name": name} if name else {}),
            **extra,
        },
        headers=headers,
    )


def test_new_guest_can_read_post_without_owner_permissions(service):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    invitation = invite(client, pid)
    preview = client.post(
        "/v1/invitations/preview", json={"code": invitation["code"]}, headers=ANONYMOUS
    )
    assert preview.json()["project"]["id"] == pid
    assert "code" not in preview.json()
    result = accept(client, invitation, name=" Alex Kim ", handle="alex-kim")
    assert result.status_code == 201, result.text
    identity = result.json()
    assert identity["actor"]["name"] == "Alex Kim"
    assert identity["actor"]["handle"] == "alex-kim"
    assert not identity["actor"]["is_admin"]
    assert identity["role"] == "guest"
    guest = auth(identity["token"])
    assert client.get("/v1/me", headers=guest).json()["id"] == identity["actor"]["id"]
    assert client.get(f"/v1/threads/{tid}/messages", headers=guest).status_code == 200
    posted = client.post(f"/v1/threads/{tid}/messages", json={"text": "Hello"}, headers=guest)
    assert posted.status_code == 201
    assert (
        client.put(
            f"/v1/messages/{posted.json()['id']}/reactions", json={"emoji": "👍"}, headers=guest
        ).status_code
        == 200
    )
    assert client.post(f"/v1/projects/{pid}/invitations", json={}, headers=guest).status_code == 403
    assert (
        client.put(f"/v1/projects/{pid}/rules", json={"text": "Change"}, headers=guest).status_code
        == 403
    )
    assert (
        client.put(
            f"/v1/projects/{pid}/members/{identity['actor']['id']}/role",
            json={"role": "owner"},
            headers=guest,
        ).status_code
        == 403
    )
    assert accept(client, invitation, name="Another").status_code == 410
    with app.state.session_factory() as db:
        saved = db.get(Invitation, invitation["id"])
        assert saved.digest == digest(invitation["code"])
        assert saved.digest != invitation["code"] and saved.used_at is not None
    listing = client.get(f"/v1/projects/{pid}/invitations").json()
    assert listing["items"][0]["used"]
    assert invitation["code"] not in str(listing)
    assert invitation["code"] not in client.get(f"/v1/projects/{pid}/events").text
    assert identity["token"] not in client.get(f"/v1/projects/{pid}/events").text


def test_invited_owner_can_invite_without_becoming_administrator(service):
    client, _, _, _ = service
    pid, _ = setup_thread(client)
    owner_invite = invite(client, pid, "owner")
    owner = accept(client, owner_invite).json()
    headers = auth(owner["token"])
    second = invite(client, pid, headers=headers)
    assert accept(client, second, name="Second").status_code == 201
    assert (
        client.post(
            "/v1/actors", json={"name": "Uninvited", "kind": "human"}, headers=headers
        ).status_code
        == 403
    )
    unrelated, _ = setup_thread(client)
    assert (
        client.post(f"/v1/projects/{unrelated}/invitations", json={}, headers=headers).status_code
        == 404
    )


def test_existing_identity_join_and_handle_collision_rollback(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    existing = client.post("/v1/actors", json={"name": "Alex", "kind": "human"}).json()
    invitation = invite(client, pid)
    with app.state.session_factory() as db:
        count = db.scalar(select(func.count()).select_from(Actor))
    assert accept(client, invitation, name="Collision", handle="alex").status_code == 409
    assert accept(client, invitation, name=" ").status_code == 422
    assert accept(client, invitation, name="Invalid", handle="UPPER").status_code == 422
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Actor)) == count
        assert db.get(Invitation, invitation["id"]).used_at is None
    headers = auth(existing["token"])
    assert accept(client, invitation, headers=headers).status_code == 422
    accepted = accept(client, invitation, name=None, headers=headers)
    assert accepted.status_code == 201
    assert accepted.json()["token"] is None
    assert accepted.json()["actor"]["id"] == existing["actor"]["id"]
    another = invite(client, pid, "owner")
    assert accept(client, another, name=None, headers=headers).status_code == 409
    assert client.get(f"/v1/projects/{pid}/members").json()["items"][1:]


def test_expiration_withdrawal_and_issuer_authority(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    expired = invite(client, pid)
    with app.state.session_factory() as db:
        db.get(Invitation, expired["id"]).expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert accept(client, expired).status_code == 410
    withdrawn = invite(client, pid)
    path = f"/v1/projects/{pid}/invitations/{withdrawn['id']}"
    assert client.delete(path).status_code == 204
    assert accept(client, withdrawn).status_code == 410
    owner = accept(client, invite(client, pid, "owner"), name="Coowner").json()
    pending = invite(client, pid, headers=auth(owner["token"]))
    assert client.delete(f"/v1/projects/{pid}/members/{owner['actor']['id']}").status_code == 204
    assert accept(client, pending).status_code == 410
    assert (
        client.post(
            "/v1/invitations/preview", json={"code": pending["code"]}, headers=ANONYMOUS
        ).status_code
        == 410
    )
    unrelated, _ = setup_thread(client)
    assert (
        client.delete(f"/v1/projects/{unrelated}/invitations/{withdrawn['id']}").status_code == 404
    )


def test_single_use_concurrent_acceptance_is_atomic(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    invitation = invite(client, pid)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(
            pool.map(lambda index: accept(client, invitation, name=f"Researcher {index}"), range(6))
        )
    assert sorted(r.status_code for r in responses) == [201, 410, 410, 410, 410, 410]
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Actor)) == 2
        assert db.scalar(select(func.count()).select_from(Membership)) == 2


def test_role_changes_protect_last_owner_and_agents(service):
    client, _, owner, _ = service
    pid, _ = setup_thread(client)
    role_path = f"/v1/projects/{pid}/members/{owner['actor']['id']}/role"
    assert client.put(role_path, json={"role": "guest"}).status_code == 409
    invited = accept(client, invite(client, pid), name="Guest").json()
    guest_role = f"/v1/projects/{pid}/members/{invited['actor']['id']}/role"
    assert client.put(guest_role, json={"role": "owner"}).json()["role"] == "owner"
    pending = invite(client, pid)
    assert client.put(role_path, json={"role": "guest"}).status_code == 200
    assert accept(client, pending, name="Late").status_code == 410
    headers = auth(invited["token"])
    assert client.put(guest_role, json={"role": "guest"}, headers=headers).status_code == 409
    agent = client.post("/v1/actors", json={"name": "Agent", "kind": "agent"}).json()
    add = f"/v1/projects/{pid}/members"
    assert (
        client.post(
            add, json={"actor_id": agent["actor"]["id"], "role": "guest"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(add, json={"actor_id": agent["actor"]["id"]}, headers=headers).status_code
        == 201
    )
    assert (
        client.put(
            f"{add}/{agent['actor']['id']}/role", json={"role": "owner"}, headers=headers
        ).status_code
        == 403
    )


def test_invalid_auth_and_agent_cannot_claim_human_invitation(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    invitation = invite(client, pid)
    agent = client.post("/v1/actors", json={"name": "Agent", "kind": "agent"}).json()
    assert accept(client, invitation, name=None, headers=auth(agent["token"])).status_code == 403
    assert accept(client, invitation, headers=auth("invalid")).status_code == 401
    assert (
        client.post(
            "/v1/invitations/accept", json={"code": "x" * 43, "name": "Unknown"}, headers=ANONYMOUS
        ).status_code
        == 404
    )
    with app.state.session_factory() as db:
        assert db.get(Invitation, invitation["id"]).used_at is None
    assert client.post(f"/v1/projects/{pid}/invitations", json={"role": "admin"}).status_code == 422
    assert (
        client.post(f"/v1/projects/{pid}/invitations", json={"expires_in_hours": 169}).status_code
        == 422
    )


def test_lost_response_recovery_requires_private_secret_and_is_stable(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    invitation = invite(client, pid)
    secret = secrets.token_urlsafe(32)
    body = {"code": invitation["code"], "claim_secret": secret, "name": "Recoverable"}
    first = client.post("/v1/invitations/accept", json=body, headers=ANONYMOUS)
    assert first.status_code == 201
    # Simulate a client that never received the first committed response. Independent
    # simultaneous retries recover the same token, without creating actors or events.
    cursor = client.get(f"/v1/projects/{pid}/events").json()["cursor"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: client.post("/v1/invitations/accept", json=body, headers=ANONYMOUS),
                range(4),
            )
        )
    assert all(r.status_code == 201 and r.json() == first.json() for r in responses)
    assert client.get(f"/v1/projects/{pid}/events").json()["cursor"] == cursor
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Actor)) == 2
        saved = db.get(Invitation, invitation["id"])
        assert saved.claim_digest == digest(secret)
        assert saved.issued_token_digest == digest(first.json()["token"])
        assert secret not in str(saved.__dict__) and first.json()["token"] not in str(
            saved.__dict__
        )
    assert accept(client, invitation, name="Imposter").status_code == 410
    preview = client.post(
        "/v1/invitations/preview",
        json={"code": invitation["code"], "claim_secret": secret},
        headers=ANONYMOUS,
    )
    assert preview.json()["accepted"] is True
    assert (
        client.post(
            "/v1/invitations/preview", json={"code": invitation["code"]}, headers=ANONYMOUS
        ).status_code
        == 410
    )
    assert client.post(f"/v1/actors/{first.json()['actor']['id']}/revoke").status_code == 204
    assert client.post("/v1/invitations/accept", json=body, headers=ANONYMOUS).status_code == 410


def test_existing_identity_recovery_cannot_restore_old_role_or_hijack_identity(service):
    client, _, _, _ = service
    pid, _ = setup_thread(client)
    existing = client.post("/v1/actors", json={"name": "Existing", "kind": "human"}).json()
    invitation = invite(client, pid, "owner")
    secret = secrets.token_urlsafe(32)
    body = {"code": invitation["code"], "claim_secret": secret}
    headers = auth(existing["token"])
    first = client.post("/v1/invitations/accept", json=body, headers=headers)
    assert first.status_code == 201 and first.json()["token"] is None
    assert (
        client.put(
            f"/v1/projects/{pid}/members/{existing['actor']['id']}/role", json={"role": "guest"}
        ).status_code
        == 200
    )
    assert (
        client.post("/v1/invitations/accept", json=body, headers=headers).json()["role"] == "guest"
    )
    assert client.post("/v1/invitations/accept", json=body, headers=ANONYMOUS).status_code == 401
    assert client.post("/v1/invitations/accept", json=body).status_code == 403
    assert client.delete(f"/v1/projects/{pid}/members/{existing['actor']['id']}").status_code == 204
    assert client.post("/v1/invitations/accept", json=body, headers=headers).status_code == 410


def expire(app, invitation):
    with app.state.session_factory() as db:
        db.get(Invitation, invitation["id"]).expires_at = now() - timedelta(minutes=5)
        db.commit()


def listing(client, pid, **params):
    response = client.get(f"/v1/projects/{pid}/invitations", params=params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_invitation_state_filter_limit_and_listing_fields(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    pending = invite(client, pid)
    withdrawn = invite(client, pid, "owner")
    assert client.delete(f"/v1/projects/{pid}/invitations/{withdrawn['id']}").status_code == 204
    expired = invite(client, pid)
    expire(app, expired)
    accepted = invite(client, pid)
    assert accept(client, accepted, name="Alex Kim", handle="alex-kim").status_code == 201
    default = listing(client, pid)
    assert [i["id"] for i in default] == [i["id"] for i in listing(client, pid, state="all")]
    assert [i["id"] for i in default] == [
        accepted["id"],
        expired["id"],
        withdrawn["id"],
        pending["id"],
    ]
    assert [i["id"] for i in listing(client, pid, state="open")] == [pending["id"]]
    closed = listing(client, pid, state="closed")
    assert {i["id"] for i in closed} == {accepted["id"], expired["id"], withdrawn["id"]}
    assert closed[0]["id"] == accepted["id"]  # most recent change first
    by_id = {i["id"]: i for i in closed}
    assert by_id[accepted["id"]]["used"] and by_id[accepted["id"]]["used_at"]
    assert by_id[accepted["id"]]["accepted_by"]["handle"] == "alex-kim"
    assert by_id[withdrawn["id"]]["revoked"] and by_id[withdrawn["id"]]["accepted_by"] is None
    assert not by_id[expired["id"]]["used"] and not by_id[expired["id"]]["revoked"]
    assert len(listing(client, pid, state="closed", limit=2)) == 2
    assert len(listing(client, pid, limit=1)) == 1
    for bad in ({"state": "other"}, {"limit": 0}, {"limit": 101}):
        assert client.get(f"/v1/projects/{pid}/invitations", params=bad).status_code == 422
    secrets_text = str([default, closed])
    for value in (pending, withdrawn, expired, accepted):
        assert value["code"] not in secrets_text


def test_clear_invitation_history_deletes_only_closed_unused_of_this_project(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    other, _ = setup_thread(client)
    pending = invite(client, pid)
    withdrawn = invite(client, pid)
    assert client.delete(f"/v1/projects/{pid}/invitations/{withdrawn['id']}").status_code == 204
    expired = invite(client, pid)
    expire(app, expired)
    accepted = invite(client, pid)
    assert accept(client, accepted, name="Kept").status_code == 201
    foreign_withdrawn = invite(client, other)
    assert (
        client.delete(f"/v1/projects/{other}/invitations/{foreign_withdrawn['id']}").status_code
        == 204
    )
    foreign_expired = invite(client, other)
    expire(app, foreign_expired)
    path = f"/v1/projects/{pid}/invitations"
    assert client.delete(path).status_code == 422
    assert client.delete(path, params={"state": "all"}).status_code == 422
    assert client.delete(path, params={"state": "open"}).status_code == 422
    assert len(listing(client, pid)) == 4
    cleared = client.delete(path, params={"state": "closed"})
    assert cleared.status_code == 200 and cleared.json() == {"deleted": 2}
    assert {i["id"] for i in listing(client, pid)} == {pending["id"], accepted["id"]}
    assert {i["id"] for i in listing(client, other)} == {
        foreign_withdrawn["id"],
        foreign_expired["id"],
    }
    assert client.delete(path, params={"state": "closed"}).json() == {"deleted": 0}
    with app.state.session_factory() as db:
        saved = db.get(Invitation, accepted["id"])
        assert saved.used_at is not None and saved.accepted_actor_id
        assert db.scalar(select(func.count()).select_from(Membership)) >= 2
    # The retained invitations still work; the deleted ones are gone for good.
    assert (
        client.post(
            "/v1/invitations/preview", json={"code": pending["code"]}, headers=ANONYMOUS
        ).status_code
        == 200
    )
    for gone in (withdrawn, expired):
        assert (
            client.post(
                "/v1/invitations/preview", json={"code": gone["code"]}, headers=ANONYMOUS
            ).status_code
            == 404
        )
        assert accept(client, gone, name="Late").status_code == 404
        assert client.delete(f"/v1/projects/{pid}/invitations/{gone['id']}").status_code == 404
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Actor).where(Actor.name == "Late")) == 0


def test_clear_invitation_history_access_control(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    withdrawn = invite(client, pid)
    assert client.delete(f"/v1/projects/{pid}/invitations/{withdrawn['id']}").status_code == 204
    guest = accept(client, invite(client, pid), name="Guest").json()
    path = f"/v1/projects/{pid}/invitations"
    closed = {"state": "closed"}
    assert client.get(path, headers=auth(guest["token"])).status_code == 403
    assert client.delete(path, params=closed, headers=auth(guest["token"])).status_code == 403
    outsider = client.post("/v1/projects", json={"name": "Else"}).json()["id"]
    assert client.delete(f"/v1/projects/{outsider}/invitations", params=closed).status_code == 200
    # A project the caller does not belong to looks absent.
    stranger = accept(client, invite(client, outsider, "owner"), name="Stranger").json()
    assert client.delete(path, params=closed, headers=auth(stranger["token"])).status_code == 404
    assert client.delete(path, params=closed, headers=auth("invalid")).status_code == 401
    agent = client.post("/v1/actors", json={"name": "Agent", "kind": "agent"}).json()
    agent_headers = auth(agent["token"])
    assert client.delete(path, params=closed, headers=agent_headers).status_code == 404
    added = client.post(f"/v1/projects/{pid}/members", json={"actor_id": agent["actor"]["id"]})
    assert added.status_code in (200, 201)
    assert client.delete(path, params=closed, headers=agent_headers).status_code == 403
    assert client.get(path, headers=agent_headers).status_code == 403
    assert len(listing(client, pid, state="closed")) == 2  # untouched by every refusal
    assert client.delete(path, params=closed).json() == {"deleted": 1}


def test_clear_invitation_history_requires_same_origin_for_browser_sessions(service):
    client, _, _, _ = service
    pid, _ = setup_thread(client)
    withdrawn = invite(client, pid)
    assert client.delete(f"/v1/projects/{pid}/invitations/{withdrawn['id']}").status_code == 204
    password = "a sufficiently long password"
    raw = client.post("/v1/auth/password", json={"password": password}).cookies["workspace_session"]
    path = f"/v1/projects/{pid}/invitations"
    headers = {"Authorization": "", "Cookie": f"workspace_session={raw}"}
    closed = {"state": "closed"}
    assert client.delete(path, params=closed, headers=headers).status_code == 403
    cross = {**headers, "Origin": "https://evil.example"}
    assert client.delete(path, params=closed, headers=cross).status_code == 403
    assert len(listing(client, pid, state="closed")) == 1
    same = {**headers, "Origin": "http://testserver"}
    assert client.delete(path, params=closed, headers=same).json() == {"deleted": 1}
