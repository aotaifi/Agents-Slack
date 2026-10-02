"""Readonly agent discovery, own credentials, and deliberate connection checks."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import agent_preflight  # noqa: E402
import poll_agent  # noqa: E402
from agent_commons_client import ApiError  # noqa: E402


class FakeClient:
    def __init__(self, kind="agent", thread_project="project"):
        self.kind, self.thread_project = kind, thread_project
        self.calls, self.posts = [], []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        assert method == "GET"
        if path == "me":
            return {
                "id": "agent", "name": "Research agent", "handle": "researcher.agent",
                "kind": self.kind, "owner": {"id": "human", "name": "Researcher"},
            }
        if path == "projects/project/rules":
            return {"text": "PRIVATE_RULES", "version": 2}
        if path == "threads/thread":
            return {"id": "thread", "project_id": self.thread_project, "title": "Research"}
        raise AssertionError(path)

    def projects(self):
        self.calls.append(("GET", "projects", {}))
        return [{"id": "project", "name": "Shared pilot"}]

    def members(self, project_id):
        self.calls.append(("GET", f"projects/{project_id}/members", {}))
        return [{"actor": {"id": "agent", "name": "Research agent", "kind": "agent"}}]

    def channels(self, project_id):
        self.calls.append(("GET", f"projects/{project_id}/channels", {}))
        return [{"id": "channel", "name": "General"}]

    def threads(self, channel_id):
        self.calls.append(("GET", f"channels/{channel_id}/threads", {}))
        return [{"id": "thread", "title": "Research", "channel_id": channel_id}]

    def context(self, thread_id, **kwargs):
        self.calls.append(("GET", f"threads/{thread_id}/context", kwargs))
        return {
            "rules": {"text": "PRIVATE_RULES", "version": 2},
            "messages": [{"text": "PRIVATE_HISTORY", "truncated": True}],
            "trigger_message": None, "parent_message": None, "has_older": True,
        }

    def post_message(self, thread_id, text, **kwargs):
        self.posts.append((thread_id, text, kwargs))
        return {"id": "test-message", "thread_id": thread_id, "sequence": 10,
                "text": "PRIVATE_POST"}

    def inbox(self, project_id, **kwargs):
        self.calls.append(("GET", f"projects/{project_id}/inbox", kwargs))
        return {"items": [], "next_cursor": None, "cursor": 10}


def test_preflight_is_readonly_and_reports_no_rules_or_message_history():
    client = FakeClient()
    report = agent_preflight.preflight(client, project_id="project")
    assert client.posts == []
    assert all(method == "GET" for method, _, _ in client.calls)
    assert report["actor"]["kind"] == "agent"
    assert report["rules"] == {"version": 2, "text_characters": 13}
    assert report["context"] == {
        "thread_id": "thread", "message_count": 1, "text_characters": 28,
        "has_older": True, "truncated_entries": 1,
    }
    serialized = json.dumps(report)
    assert "PRIVATE_RULES" not in serialized and "PRIVATE_HISTORY" not in serialized
    assert client.calls[-1][2] == {"limit": 5, "max_chars": 1000}


def test_human_credential_is_rejected_before_any_project_request():
    client = FakeClient(kind="human")
    with pytest.raises(ValueError, match="human identity"):
        agent_preflight.preflight(client)
    assert [path for _, path, _ in client.calls] == ["me"]
    assert client.posts == []


def test_wrong_project_and_foreign_thread_never_post():
    client = FakeClient()
    with pytest.raises(ValueError, match="not visible"):
        agent_preflight.preflight(client, project_id="missing", post_test="thread")
    assert client.posts == []
    foreign = FakeClient(thread_project="other")
    with pytest.raises(ValueError, match="different project"):
        agent_preflight.preflight(foreign, project_id="project", post_test="thread")
    assert foreign.posts == []


def test_post_test_is_explicit_and_uses_stable_default_or_supplied_key():
    client = FakeClient()
    first = agent_preflight.preflight(client, post_test="thread")
    agent_preflight.preflight(client, post_test="thread")
    assert client.posts[0] == client.posts[1]
    assert client.posts[0][1] == agent_preflight.CONNECTION_TEXT
    assert first["test_message"]["id"] == "test-message"
    assert "PRIVATE_POST" not in json.dumps(first)
    agent_preflight.preflight(client, post_test="thread", idempotency_key="retry-me")
    assert client.posts[-1][2]["idempotency_key"] == "retry-me"


def test_multiple_projects_require_explicit_selection_before_post():
    client = FakeClient()
    client.projects = lambda: [{"id": "project", "name": "One"}, {"id": "two", "name": "Two"}]
    report = agent_preflight.preflight(client)
    assert report["project_count"] == 2 and "next_step" in report
    with pytest.raises(ValueError, match="Choose --project"):
        agent_preflight.preflight(client, post_test="thread")
    assert client.posts == []


def test_credentials_accept_token_only_and_server_actor_response_without_overwrite(tmp_path):
    path = tmp_path / "private" / "agent.json"
    agent_preflight.write_credentials(path, "PRIVATE_TOKEN")
    assert agent_preflight.load_credentials(path) == {"token": "PRIVATE_TOKEN"}
    with pytest.raises(FileExistsError):
        agent_preflight.write_credentials(path, "REPLACEMENT")
    assert agent_preflight.load_credentials(path)["token"] == "PRIVATE_TOKEN"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    server_response = tmp_path / "server-response.json"
    server_response.write_text(json.dumps({"token": "PRIVATE_TOKEN", "actor": {"id": "agent"}}))
    assert agent_preflight.load_credentials(server_response)["actor"]["id"] == "agent"


@pytest.mark.parametrize("payload", [[], {}, {"token": ""}, {"token": 42}])
def test_invalid_credentials_fail_without_echoing_contents(tmp_path, payload):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="nonempty token string"):
        agent_preflight.load_credentials(path)


def test_cli_getpass_creates_private_file_and_never_prints_token(tmp_path, monkeypatch, capsys):
    path = tmp_path / "private" / "agent.json"
    monkeypatch.setattr(agent_preflight.getpass, "getpass", lambda _: "PRIVATE_TOKEN")
    monkeypatch.setattr(agent_preflight, "Client", lambda url, token: FakeClient())
    args = ["--url", "http://127.0.0.1:8002", "--credentials", str(path), "--create-credentials"]
    assert agent_preflight.main(args) == 0
    captured = capsys.readouterr()
    assert "PRIVATE_TOKEN" not in captured.out + captured.err
    assert "Research agent" in captured.out
    assert agent_preflight.main(args) == 1
    assert "will not be overwritten" in capsys.readouterr().err
    assert agent_preflight.load_credentials(path)["token"] == "PRIVATE_TOKEN"


def test_cli_api_failure_does_not_print_server_body_or_credential(tmp_path, monkeypatch, capsys):
    path = tmp_path / "agent.json"
    agent_preflight.write_credentials(path, "PRIVATE_TOKEN")

    class Denied(FakeClient):
        def request(self, *_args, **_kwargs):
            raise ApiError(401, "PRIVATE_TOKEN", body={"token": "PRIVATE_TOKEN"})

    monkeypatch.setattr(agent_preflight, "Client", lambda url, token: Denied())
    assert agent_preflight.main([
        "--url", "http://127.0.0.1:8002", "--credentials", str(path),
    ]) == 1
    captured = capsys.readouterr()
    assert "PRIVATE_TOKEN" not in captured.out + captured.err
    assert "HTTP 401" in captured.err


def test_poll_connectivity_accepts_token_only_and_resolves_current_identity(tmp_path,
                                                                          monkeypatch, capsys):
    path = tmp_path / "agent.json"
    agent_preflight.write_credentials(path, "PRIVATE_TOKEN")
    client = FakeClient()
    monkeypatch.setattr(poll_agent, "Client", lambda url, token: client)
    checkpoint = tmp_path / "checkpoint.json"
    poll_agent.main([
        "--credentials", str(path), "--project", "project", "--checkpoint", str(checkpoint),
        "--reply-text", "Connectivity check only",
    ])
    assert json.loads(checkpoint.read_text())["identity"]["actor_id"] == "agent"
    assert client.calls[0][1] == "me"
    assert client.posts == []
    assert "PRIVATE_TOKEN" not in capsys.readouterr().out


def test_poll_connectivity_rejects_human_before_checkpoint_creation(tmp_path, monkeypatch):
    path = tmp_path / "human.json"
    agent_preflight.write_credentials(path, "PRIVATE_TOKEN")
    monkeypatch.setattr(poll_agent, "Client", lambda url, token: FakeClient(kind="human"))
    checkpoint = tmp_path / "checkpoint.json"
    with pytest.raises(ValueError, match="human identity"):
        poll_agent.main([
            "--credentials", str(path), "--project", "project", "--checkpoint", str(checkpoint),
            "--reply-text", "Connectivity check only",
        ])
    assert not checkpoint.exists()


def test_transport_value_error_never_echoes_token(tmp_path, monkeypatch, capsys):
    path = tmp_path / "agent.json"
    agent_preflight.write_credentials(path, "PRIVATE_TOKEN")

    class Malformed(FakeClient):
        def request(self, *_args, **_kwargs):
            raise ValueError("invalid header PRIVATE_TOKEN")

    monkeypatch.setattr(agent_preflight, "Client", lambda url, token: Malformed())
    assert agent_preflight.main([
        "--url", "http://127.0.0.1:8002", "--credentials", str(path),
    ]) == 1
    assert "PRIVATE_TOKEN" not in capsys.readouterr().err


def test_hidden_prompt_unavailable_never_falls_back_to_echoed_input(tmp_path, monkeypatch, capsys):
    path = tmp_path / "agent.json"

    def unavailable(_):
        import warnings
        warnings.warn("Cannot hide input", agent_preflight.getpass.GetPassWarning)
        raise AssertionError("must stop before fallback input")

    monkeypatch.setattr(agent_preflight.getpass, "getpass", unavailable)
    assert agent_preflight.main([
        "--url", "http://127.0.0.1:8002", "--credentials", str(path), "--create-credentials",
    ]) == 1
    assert not path.exists()
    assert "hidden terminal input" in capsys.readouterr().err
