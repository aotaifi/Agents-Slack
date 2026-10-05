import importlib.util
import json
from pathlib import Path

import pytest
from conftest import auth

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "register-researcher.py"
spec = importlib.util.spec_from_file_location("register_researcher", SCRIPT)
onboarding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(onboarding)


def write_credentials(path, token, **extra):
    path.write_text(json.dumps({"token": token, **extra}))
    path.chmod(0o600)
    return path


@pytest.fixture
def adapter(service, monkeypatch):
    client, _, owner, _ = service
    calls = []

    class ApiClient:
        def __init__(self, url, token):
            self.token = token

        def request(self, method, path, *, data=None):
            calls.append((method, path, data))
            response = client.request(method, "/v1/" + path, json=data, headers=auth(self.token))
            if response.status_code >= 400:
                raise onboarding.ApiError(response.status_code, response.json()["detail"])
            return response.json()

    monkeypatch.setattr(onboarding, "Client", ApiClient)
    return client, owner, calls


def test_registration_private_file_and_no_automatic_membership(adapter, tmp_path, capsys):
    client, owner, calls = adapter
    administrator = write_credentials(tmp_path / "admin.json", owner["token"])
    output = tmp_path / "private" / "researchers" / "alex.json"
    onboarding.main(
        [
            "--url",
            "http://lab.invalid",
            "--admin-credentials",
            str(administrator),
            "--name",
            "Alex Kim",
            "--handle",
            "alex-kim",
            "--output",
            str(output),
        ]
    )
    credentials = json.loads(output.read_text())
    assert set(credentials) == {"actor", "token"}
    assert credentials["actor"]["name"] == "Alex Kim"
    assert credentials["actor"]["handle"] == "alex-kim"
    assert credentials["actor"]["kind"] == "human"
    assert not credentials["actor"]["is_admin"]
    assert credentials["actor"]["owner"] is None
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert output.parent.parent.stat().st_mode & 0o777 == 0o700
    assert calls == [
        ("GET", "me", None),
        (
            "POST",
            "actors",
            {
                "name": "Alex Kim",
                "kind": "human",
                "handle": "alex-kim",
            },
        ),
    ]
    stdout = capsys.readouterr().out
    assert credentials["token"] not in stdout and owner["token"] not in stdout
    assert "alex-kim" in stdout and credentials["actor"]["id"] in stdout
    personal_headers = auth(credentials["token"])
    assert client.get("/v1/projects", headers=personal_headers).json() == {"items": []}
    project = client.post("/v1/projects", json={"name": "Alex's project"}, headers=personal_headers)
    assert project.status_code == 201
    members = client.get(
        f"/v1/projects/{project.json()['id']}/members", headers=personal_headers
    ).json()
    assert members["items"][0]["role"] == "owner"
    assert members["items"][0]["actor"]["id"] == credentials["actor"]["id"]


def test_existing_output_refused_before_any_api_request(adapter, tmp_path):
    client, owner, calls = adapter
    administrator = write_credentials(tmp_path / "admin.json", owner["token"])
    output = tmp_path / "preserve.json"
    output.write_text("do not replace")
    before = client.get("/v1/actors").json()
    with pytest.raises(FileExistsError):
        onboarding.register_researcher("http://lab.invalid", administrator, "Alex", output)
    assert calls == []
    assert output.read_text() == "do not replace"
    assert client.get("/v1/actors").json() == before


@pytest.mark.parametrize("kind", ["human", "agent"])
def test_live_admin_check_rejects_other_tokens_despite_file_claim(adapter, tmp_path, kind):
    client, _, calls = adapter
    credentials = client.post("/v1/actors", json={"name": "Not admin", "kind": kind}).json()
    misleading = write_credentials(
        tmp_path / "claimed-admin.json",
        credentials["token"],
        actor={"kind": "human", "is_admin": True},
    )
    output = tmp_path / "not-created.json"
    before = client.get("/v1/actors").json()
    with pytest.raises(ValueError, match="human administrator"):
        onboarding.register_researcher("http://lab.invalid", misleading, "Alex", output)
    assert calls == [("GET", "me", None)]
    assert not output.exists()
    assert client.get("/v1/actors").json() == before
    # Verify the backend independently rejects human registration for both tokens.
    response = client.post(
        "/v1/actors",
        json={"name": "Unauthorized", "kind": "human"},
        headers=auth(credentials["token"]),
    )
    assert response.status_code == 403


def test_registration_auto_handle_and_collision_cleanup(adapter, tmp_path):
    client, owner, _ = adapter
    administrator = write_credentials(tmp_path / "admin.json", owner["token"])
    first = tmp_path / "first.json"
    actor = onboarding.register_researcher("http://lab.invalid", administrator, "Alex Kim", first)
    assert actor["handle"] == "alex-kim"
    second = tmp_path / "second.json"
    with pytest.raises(onboarding.ApiError) as failure:
        onboarding.register_researcher(
            "http://lab.invalid", administrator, "Other", second, handle="alex-kim"
        )
    assert failure.value.status == 409
    assert not second.exists()
    assert len(client.get("/v1/actors").json()["items"]) == 2


def test_failed_credentials_do_not_leave_output(tmp_path, monkeypatch):
    credential_file = write_credentials(tmp_path / "invalid.json", "")
    called = []
    monkeypatch.setattr(onboarding, "Client", lambda *args: called.append(args))
    with pytest.raises(ValueError, match="nonempty token"):
        onboarding.register_researcher(
            "http://lab.invalid", credential_file, "Alex", tmp_path / "output.json"
        )
    assert called == []
    assert not (tmp_path / "output.json").exists()
