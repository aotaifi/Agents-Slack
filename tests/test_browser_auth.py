import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event as Signal

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_api import auth, setup_thread
from test_api import service as api_service

from agent_commons.main import create_app, utc
from agent_commons.models import Actor, AuthAttempt, BrowserSession, Token, now
from agent_commons.security import digest, verify_password

service = api_service
PASSWORD = "a sufficiently long password"
NEW_PASSWORD = "  another sufficiently long password  "
ORIGIN = {"Origin": "http://testserver"}
ANONYMOUS = {"Authorization": "", **ORIGIN}


def cookie_auth(raw, origin=True):
    return {"Authorization": "", "Cookie": f"workspace_session={raw}", **(ORIGIN if origin else {})}


def setup_password(client, **extra):
    result = client.post("/v1/auth/password", json={"password": PASSWORD, **extra})
    assert result.status_code == 200, result.text
    return result.cookies["workspace_session"]


def login(client, password=PASSWORD, **extra):
    return client.post(
        "/v1/auth/login", json={"handle": "owner", "password": password, **extra}, headers=ANONYMOUS
    )


def test_login_session_cookie_attributes_and_restart(service):
    client, app, owner, url = service
    assert client.get("/v1/me").json()["has_password"] is False
    raw = setup_password(client)
    assert client.get("/v1/me", headers=cookie_auth(raw)).json()["has_password"] is True
    response = login(client)
    assert response.status_code == 200
    assert response.json() == {"actor": client.get("/v1/me").json()}
    header = response.headers["set-cookie"]
    for value in ("HttpOnly", "SameSite=strict", "Path=/v1"):
        assert value in header
    assert "Max-Age" not in header and "Domain" not in header and "Secure" not in header
    raw = response.cookies["workspace_session"]
    with app.state.session_factory() as db:
        actor = db.get(Actor, owner["actor"]["id"])
        assert PASSWORD not in actor.password_hash and actor.password_hash.startswith(
            "scrypt$32768$8$3$"
        )
        saved = db.get(BrowserSession, digest(raw))
        assert saved.digest != raw
        assert 43190 <= (utc(saved.expires_at) - now()).total_seconds() <= 43200
    restarted = create_app(url)
    with TestClient(restarted) as other:
        assert other.get("/v1/me", headers=cookie_auth(raw)).json()["id"] == owner["actor"]["id"]
    restarted.state.engine.dispose()
    remembered = login(client, remember=True)
    assert "Max-Age=2592000" in remembered.headers["set-cookie"]
    with app.state.session_factory() as db:
        saved = db.get(BrowserSession, digest(remembered.cookies["workspace_session"]))
        assert 2591990 <= (utc(saved.expires_at) - now()).total_seconds() <= 2592000
    with TestClient(app, base_url="https://testserver") as secure:
        result = secure.post(
            "/v1/auth/login",
            json={"handle": "owner", "password": PASSWORD},
            headers={"Origin": "https://testserver"},
        )
        assert result.status_code == 200
        assert "Secure" in result.headers["set-cookie"]
    assert "has_password" not in client.get("/v1/actors").json()["items"][0]


def test_expiry_logout_and_auth_precedence(service):
    client, app, owner, _ = service
    raw = setup_password(client)
    assert client.get("/v1/me", headers=cookie_auth("stale")).status_code == 401
    assert client.get("/v1/me", headers={**cookie_auth(raw), **auth("invalid")}).status_code == 401
    # A stale cookie does not displace a valid explicit agent credential.
    agent = client.post("/v1/actors", json={"name": "Agent", "kind": "agent"}).json()
    chosen = client.get("/v1/me", headers={**cookie_auth("stale"), **auth(agent["token"])})
    assert chosen.json()["id"] == agent["actor"]["id"]
    with app.state.session_factory() as db:
        db.get(BrowserSession, digest(raw)).expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.get("/v1/me", headers=cookie_auth(raw)).status_code == 401
    raw = login(client).cookies["workspace_session"]
    logged_out = client.post("/v1/auth/logout", headers=cookie_auth(raw))
    assert logged_out.status_code == 200 and "Max-Age=0" in logged_out.headers["set-cookie"]
    assert client.get("/v1/me", headers=cookie_auth(raw)).status_code == 401
    assert client.get("/v1/me", headers=auth(owner["token"])).status_code == 200
    assert client.get("/v1/me", headers=auth(agent["token"])).status_code == 200


def test_exact_origin_blocks_cookie_mutations_but_bearer_api_survives(service):
    client, app, owner, url = service
    raw = setup_password(client)
    for origin in (
        None,
        "http://evil.invalid",
        "http://testserver:8001",
        "https://testserver",
        "null",
    ):
        headers = cookie_auth(raw, origin=False)
        if origin is not None:
            headers["Origin"] = origin
        assert client.patch("/v1/me", json={"name": "Changed"}, headers=headers).status_code == 403
        assert client.post("/v1/auth/logout", headers=headers).status_code == 403
        assert (
            client.post(
                "/v1/auth/login", json={"handle": "owner", "password": PASSWORD}, headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/auth/password", json={"password": NEW_PASSWORD}, headers=headers
            ).status_code
            == 403
        )
    assert client.patch("/v1/me", json={"name": "Allowed"}).status_code == 200
    assert (
        client.patch(
            "/v1/me", json={"name": "Cookie allowed"}, headers=cookie_auth(raw)
        ).status_code
        == 200
    )

    agent = client.post("/v1/actors", json={"name": "Owned", "kind": "agent"}).json()
    old_name = "Cookie allowed"
    changed = client.patch("/v1/me", json={"name": "  Updated researcher  "})
    assert changed.status_code == 200
    assert changed.json()["name"] == "Updated researcher"
    for field in ("id", "handle", "kind", "owner_id", "is_admin"):
        assert changed.json()[field] == owner["actor"][field]
    assert client.patch("/v1/me", json={"name": " "}).status_code == 422
    assert client.patch("/v1/me", json={"name": "X", "handle": "new"}).status_code == 422
    assert client.get("/v1/me", headers=auth(agent["token"])).json()["owner"] == {
        "id": owner["actor"]["id"],
        "name": "Updated researcher",
        "handle": "owner",
    }
    assert agent["actor"]["owner"]["name"] == old_name
    restarted = create_app(url)
    with TestClient(restarted) as other:
        assert (
            other.get("/v1/me", headers=auth(owner["token"])).json()["name"] == "Updated researcher"
        )
    restarted.state.engine.dispose()
    with app.state.session_factory() as db:
        assert db.get(Actor, agent["actor"]["id"]).handle == "owner.owned"


def test_password_rotation_requires_old_password_and_preserves_api_tokens(service):
    client, app, owner, _ = service
    old_cookie = setup_password(client)
    other_cookie = login(client).cookies["workspace_session"]
    agent = client.post("/v1/actors", json={"name": "Agent", "kind": "agent"}).json()
    for extra in ({}, {"current_password": "incorrect"}):
        failed = client.post(
            "/v1/auth/password",
            json={"password": NEW_PASSWORD, **extra},
            headers=cookie_auth(old_cookie),
        )
        assert failed.status_code == 401
    changed = client.post(
        "/v1/auth/password",
        json={"password": NEW_PASSWORD, "current_password": PASSWORD},
        headers=cookie_auth(old_cookie),
    )
    assert changed.status_code == 200
    new_cookie = changed.cookies["workspace_session"]
    assert client.get("/v1/me", headers=cookie_auth(new_cookie)).status_code == 200
    for old in (old_cookie, other_cookie):
        assert client.get("/v1/me", headers=cookie_auth(old)).status_code == 401
    assert client.get("/v1/me", headers=auth(owner["token"])).status_code == 200
    assert client.get("/v1/me", headers=auth(agent["token"])).status_code == 200
    with app.state.session_factory() as db:
        assert all(not token.revoked for token in db.scalars(select(Token)))
        assert verify_password(NEW_PASSWORD, db.get(Actor, owner["actor"]["id"]).password_hash)
        assert not verify_password(
            NEW_PASSWORD.strip(), db.get(Actor, owner["actor"]["id"]).password_hash
        )
    # Agent identities cannot set a human login or edit a human profile.
    assert (
        client.post(
            "/v1/auth/password", json={"password": PASSWORD}, headers=auth(agent["token"])
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/v1/me", json={"name": "Agent renamed"}, headers=auth(agent["token"])
        ).status_code
        == 403
    )


def test_authentication_rate_failures_persist_and_validation_never_reflects_password(service):
    client, app, _, url = service
    app.state.auth_account_limit = 2
    for _ in range(2):
        wrong = client.post(
            "/v1/auth/login", json={"handle": "missing", "password": PASSWORD}, headers=ANONYMOUS
        )
        assert wrong.status_code == 401 and wrong.json()["detail"] == "Invalid handle or password"
    restarted = create_app(url)
    restarted.state.auth_account_limit = 2
    with TestClient(restarted) as other:
        limited = other.post(
            "/v1/auth/login", json={"handle": "missing", "password": PASSWORD}, headers=ORIGIN
        )
        assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
    restarted.state.engine.dispose()
    with app.state.session_factory() as db:
        assert db.get(AuthAttempt, digest("account:missing")).count == 2
    app.state.auth_account_limit = 100
    app.state.auth_ip_limit = 3
    invalid = client.post(
        "/v1/auth/login", json={"handle": "another", "password": "sensitive"}, headers=ANONYMOUS
    )
    assert invalid.status_code == 422 and "sensitive" not in invalid.text
    limited = client.post(
        "/v1/auth/login", json={"handle": "different", "password": PASSWORD}, headers=ANONYMOUS
    )
    assert limited.status_code == 429
    with app.state.session_factory() as db:
        db.get(AuthAttempt, digest("ip:testclient")).started_at = now() - timedelta(seconds=61)
        db.get(AuthAttempt, digest("account:missing")).started_at = now() - timedelta(seconds=61)
        db.commit()
    assert login(client).status_code == 401


def test_human_revocation_disables_password_sessions_and_invitation_recovery(service):
    client, app, owner, _ = service
    raw = setup_password(client)
    agent = client.post("/v1/actors", json={"name": "Owned", "kind": "agent"}).json()
    assert client.post(f"/v1/actors/{owner['actor']['id']}/revoke").status_code == 204
    assert client.get("/v1/me", headers=cookie_auth(raw)).status_code == 401
    assert client.get("/v1/me").status_code == 401
    assert login(client).status_code == 401
    assert client.get("/v1/me", headers=auth(agent["token"])).status_code == 200
    with app.state.session_factory() as db:
        assert db.get(Actor, owner["actor"]["id"]).password_hash is None
        assert all(row.revoked for row in db.scalars(select(BrowserSession)))


def test_invitation_password_creation_recovery_and_cookie_identity(service):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    invitation = client.post(f"/v1/projects/{pid}/invitations", json={}).json()
    body = {
        "code": invitation["code"],
        "claim_secret": secrets.token_urlsafe(32),
        "name": "Alex",
        "handle": "alex",
        "password": PASSWORD,
    }
    assert (
        client.post("/v1/invitations/accept", json=body, headers={"Authorization": ""}).status_code
        == 403
    )
    first = client.post("/v1/invitations/accept", json=body, headers=ANONYMOUS)
    assert first.status_code == 201 and first.json()["token"]
    assert "has_password" not in first.json()["actor"]
    with app.state.session_factory() as db:
        original_hash = db.get(Actor, first.json()["actor"]["id"]).password_hash
    recovered = client.post(
        "/v1/invitations/accept", json={**body, "password": NEW_PASSWORD}, headers=ANONYMOUS
    )
    assert recovered.status_code == 201 and recovered.json()["token"] == first.json()["token"]
    with app.state.session_factory() as db:
        assert db.get(Actor, first.json()["actor"]["id"]).password_hash == original_hash
    logged_in = client.post(
        "/v1/auth/login", json={"handle": "alex", "password": PASSWORD}, headers=ANONYMOUS
    )
    raw = logged_in.cookies["workspace_session"]
    other_pid, _ = setup_thread(client)
    next_invitation = client.post(f"/v1/projects/{other_pid}/invitations", json={}).json()
    joining = {"code": next_invitation["code"], "claim_secret": secrets.token_urlsafe(32)}
    assert (
        client.post(
            "/v1/invitations/accept", json=joining, headers=cookie_auth(raw, origin=False)
        ).status_code
        == 403
    )
    joined = client.post("/v1/invitations/accept", json=joining, headers=cookie_auth(raw))
    assert joined.status_code == 201 and joined.json()["token"] is None
    assert joined.json()["actor"]["id"] == first.json()["actor"]["id"]
    assert (
        client.post("/v1/invitations/accept", json=joining, headers=cookie_auth(raw)).status_code
        == 201
    )
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Actor)) == 2
        assert db.get(Actor, first.json()["actor"]["id"]).password_hash == original_hash


def test_login_cannot_issue_session_after_concurrent_password_rotation(service, monkeypatch):
    import agent_commons.main as backend

    client, app, owner, _ = service
    setup_password(client)
    entered, release = Signal(), Signal()
    real_verify = backend.verify_password

    def delayed_verify(password, encoded):
        verified = real_verify(password, encoded)
        if password == PASSWORD and encoded:
            entered.set()
            assert release.wait(timeout=10)
        return verified

    monkeypatch.setattr(backend, "verify_password", delayed_verify)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(login, client)
        assert entered.wait(timeout=10)
        # The login has verified an old hash but has not locked/issued its session.
        monkeypatch.setattr(backend, "verify_password", real_verify)
        rotated = client.post(
            "/v1/auth/password",
            json={"password": NEW_PASSWORD, "current_password": PASSWORD},
            headers=auth(owner["token"]),
        )
        assert rotated.status_code == 200
        release.set()
        stale = pending.result(timeout=10)
    assert stale.status_code == 401 and "set-cookie" not in stale.headers
    with app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(BrowserSession)
                .where(BrowserSession.revoked.is_(False))
            )
            == 1
        )
