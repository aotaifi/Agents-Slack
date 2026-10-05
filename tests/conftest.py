"""Shared fixtures and helpers for the whole suite."""

import os
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from stubs import serve

from agent_commons import security
from agent_commons.cli import bootstrap
from agent_commons.db import Base, make_engine
from agent_commons.main import create_app


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    """Hash passwords at minimal cost in tests; production constants are unchanged."""
    monkeypatch.setattr(security, "SCRYPT_N", 16)
    monkeypatch.setattr(security, "SCRYPT_R", 1)
    monkeypatch.setattr(security, "SCRYPT_P", 1)
    # The unknown-identity hash is computed at import time with production cost.
    monkeypatch.setattr(
        security, "_DUMMY_HASH", security.hash_password("dummy password for unknown identity")
    )


@pytest.fixture
def stub_server():
    """Factory: `stub_server(HandlerClass)` serves it and returns the base URL."""
    with ExitStack() as stack:
        yield lambda handler: stack.enter_context(serve(handler))


def postgres_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    return url


@pytest.fixture(params=["sqlite", "postgres"])
def service(request, tmp_path):
    url = postgres_url() if request.param == "postgres" else f"sqlite:///{tmp_path}/test.db"
    engine = make_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()
    credentials = bootstrap("Owner", database_url=url)
    app = create_app(url)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {credentials['token']}"
        yield client, app, credentials, url
    app.state.engine.dispose()


def setup_thread(client):
    p = client.post("/v1/projects", json={"name": "Physics"}).json()
    c = client.post(f"/v1/projects/{p['id']}/channels", json={"name": "General"}).json()
    t = client.post(f"/v1/channels/{c['id']}/threads", json={"title": "Results"}).json()
    return p["id"], t["id"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}
