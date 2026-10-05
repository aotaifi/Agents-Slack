"""Session-only notification, lease, cursor, and exact-retry adapter guarantees."""

import io
import json
import sys
import urllib.response
from email.message import Message
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
import claude_workspace as adapter  # noqa: E402
from agent_commons_client import ApiError  # noqa: E402

CONNECTION = {
    "id": "connection",
    "actor": {"id": "agent", "kind": "agent", "handle": "owner.agent"},
    "project": {"id": "project", "name": "Research"},
    "bound": False,
    "active": False,
    "revoked": False,
}


def mention(sequence):
    return {
        "id": sequence,
        "type": "message.created",
        "thread_id": "thread",
        "payload": {
            "id": f"message-{sequence}",
            "reply_to": "root",
            "author": {"id": "human"},
            "mentions": ["agent"],
            "text": "RAW_EVENT_NOT_FOR_CONTEXT",
            "metadata": {"huge": "private"},
        },
    }


class Fake:
    def __init__(self):
        self.calls, self.accepted = [], {}
        self.events = [mention(5)]
        self.denied = self.lose_post = self.lose_context = False
        self.pages = None
        self.me = {**CONNECTION["actor"]}
        self.rules_version = 2

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "me":
            return self.me
        if path == "agent-connections":
            return {"connection": CONNECTION, "token": "SCOPED_PRIVATE_TOKEN"}
        if path.endswith("/claim"):
            if self.denied:
                raise ApiError(409, "private server error")
            return {"connection": {**CONNECTION, "bound": True, "active": True}}
        if path.endswith("/release"):
            return {"connection": {**CONNECTION, "bound": True, "active": False}}
        if path.endswith("/inbox"):
            if self.pages:
                return self.pages.pop(0)
            after = kwargs["query"]["after"]
            pending = [e for e in self.events if e["id"] > after]
            return {
                "items": pending[:1],
                "next_cursor": pending[0]["id"] if len(pending) > 1 else None,
                "cursor": max([after, *[e["id"] for e in self.events]]),
            }
        if path.endswith("/context"):
            if self.lose_context:
                self.lose_context = False
                raise ApiError(0, "private network data")
            message_id = kwargs["query"]["trigger_message_id"]
            return {
                "rules": {"text": "External project rules", "version": self.rules_version},
                "messages": [{"id": message_id, "text": "Please review", "metadata": {"big": 1}}],
                "trigger_message": {"id": message_id, "text": "Please review", "reply_to": "root"},
                "parent_message": {"id": "root", "text": "Research question"},
                "has_older": True,
            }
        if path.endswith("/messages") and method == "POST":
            original = self.accepted.setdefault(kwargs["key"], kwargs)
            assert original == kwargs
            if self.lose_post:
                self.lose_post = False
                raise ApiError(0, "lost response")
            return {"id": "posted-reply"}
        raise AssertionError((method, path))


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "chosen-project"
    root.mkdir(mode=0o700)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    config = {
        "connection_id": "connection",
        "project_id": "project",
        "actor_id": "agent",
        "project_name": "Research",
        "handle": "owner.agent",
        "cwd": str(root.resolve()),
        "credentials": str(private / "credentials.json"),
        "checkpoint": str(private / "state.json"),
        "url": "http://127.0.0.1:8002",
        "throttle": 30,
        "replay_backlog": True,
        "command": "python /private/adapter.py --config /private/config.json",
    }
    adapter.write_private(
        private / "credentials.json",
        {
            "token": "PRIVATE_TOKEN",
            "connection": CONNECTION,
        },
        exclusive=True,
    )
    adapter.write_private(private / "config.json", config, exclusive=True)
    adapter.write_private(private / "state.json", adapter.initial_state(config), exclusive=True)
    return root, private / "config.json"


def payload(root, name="SessionStart", **values):
    return {"session_id": "session-one", "cwd": str(root), "hook_event_name": name, **values}


def state(config_path):
    return adapter.read_private(adapter.read_private(config_path)["checkpoint"])


def test_other_session_outside_cwd_symlink_escape_and_any_subagent_are_noops(bundle, tmp_path):
    root, config = bundle
    fake = Fake()
    workspace = adapter.Workspace(config, transport=fake)
    assert workspace.hook(payload(root, "PostToolUse")) is None  # only SessionStart can bind
    assert fake.calls == []
    workspace.hook(payload(root))
    count = len(fake.calls)
    assert workspace.hook(payload(root, session_id="different-session")) is None
    assert workspace.hook(payload(tmp_path)) is None
    for agent_id in ("child", "", None):
        assert workspace.hook(payload(root, agent_id=agent_id)) is None
    (root / "escape").symlink_to(tmp_path)
    assert workspace.hook(payload(root / "escape")) is None
    assert len(fake.calls) == count
    with pytest.raises(adapter.AdapterError, match="bound Claude session"):
        workspace.command("ack", session="different-session", cwd=root)
    assert len(fake.calls) == count


def test_hook_json_throttle_pending_once_resume_and_subdirectories(bundle):
    root, config = bundle
    fake = Fake()
    clock = [100]
    workspace = adapter.Workspace(config, transport=fake, now=lambda: clock[0])
    output = workspace.hook(payload(root))
    hook = output["hookSpecificOutput"]
    assert (
        hook["hookEventName"] == "SessionStart"
        and "New workspace mention" in hook["additionalContext"]
    )
    assert "external conversation data" in hook["additionalContext"]
    assert "react, search, mute_thread" in hook["additionalContext"]
    assert "RAW_EVENT_NOT_FOR_CONTEXT" not in json.dumps(output)
    assert "metadata" not in json.dumps(output)
    assert state(config)["cursor"] == 0  # delivery does not acknowledge
    count = len(fake.calls)
    assert workspace.hook(payload(root, "PostToolUse")) is None
    assert len(fake.calls) == count
    clock[0] += 31
    (root / "src").mkdir()
    assert workspace.hook(payload(root / "src", "UserPromptSubmit")) is None
    assert len(fake.calls) == count + 1  # lease renewal, no duplicate context/notice
    resumed = workspace.hook(payload(root, source="resume"))
    assert resumed["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert not fake.accepted
    assert (
        workspace.command("pending", session="session-one", cwd=root)["message_id"] == "message-5"
    )


def test_empty_pages_are_scanned_without_skipping_pending_and_ack_is_explicit(bundle):
    root, config = bundle
    fake = Fake()
    fake.pages = [
        {"items": [], "next_cursor": 2, "cursor": 10},
        {"items": [], "next_cursor": 4, "cursor": 10},
        {"items": [mention(5)], "next_cursor": 5, "cursor": 10},
    ]
    workspace = adapter.Workspace(config, transport=fake)
    assert workspace.hook(payload(root)) is not None
    assert [kw["query"]["after"] for _, p, kw in fake.calls if p.endswith("inbox")] == [0, 2, 4]
    assert state(config)["cursor"] == 4 and state(config)["pending"]["event_id"] == 5
    result = workspace.command("ack", session="session-one", cwd=root)
    assert result == {"acknowledged_event": 5}
    assert state(config)["cursor"] == 5 and state(config)["pending"] is None


def test_new_checkpoint_tails_backlog_and_network_loss_retains_new_notice(bundle):
    root, config = bundle
    current = state(config)
    adapter.write_private(
        adapter.read_private(config)["checkpoint"], {**current, "initialized": False}
    )
    fake = Fake()
    workspace = adapter.Workspace(config, transport=fake, now=lambda: 100)
    assert workspace.hook(payload(root)) is None
    assert state(config)["cursor"] == 5
    fake.events.append(mention(6))
    fake.lose_context = True
    workspace = adapter.Workspace(config, transport=fake, now=lambda: 140)
    assert workspace.hook(payload(root, "PostToolUse")) is None
    assert state(config)["pending"]["message_id"] == "message-6"
    assert state(config)["pending"]["context"] is None and state(config)["cursor"] == 5
    restarted = adapter.Workspace(config, transport=fake, now=lambda: 180)
    assert restarted.hook(payload(root, "PostToolUse")) is not None
    assert state(config)["cursor"] == 5


def test_reply_persists_body_key_header_and_retries_after_lost_response(bundle, monkeypatch):
    root, config = bundle
    fake = Fake()
    workspace = adapter.Workspace(config, transport=fake)
    workspace.hook(payload(root))
    fake.rules_version = 3
    with pytest.raises(adapter.AdapterError, match="rules changed"):
        workspace.command("reply", session="session-one", cwd=root, text="Before review")
    assert state(config)["reply"] is None and fake.accepted == {}
    assert state(config)["pending"]["context"]["rules"]["version"] == 2
    with pytest.raises(adapter.AdapterError, match="rules changed"):
        workspace.command("reply", session="session-one", cwd=root, text="Before review")
    assert state(config)["reply"] is None and fake.accepted == {}
    assert (
        workspace.command("pending", session="session-one", cwd=root)["context"]["rules"]["version"]
        == 3
    )
    fake.lose_post = True
    with pytest.raises(ApiError):
        workspace.command(
            "reply", session="session-one", cwd=root, text="Concise reply", mentions=["human"]
        )
    operation = state(config)["reply"]
    assert operation["body"] == {"text": "Concise reply", "reply_to": "root", "mentions": ["human"]}
    assert state(config)["pending"] is not None and state(config)["cursor"] == 0
    restarted = adapter.Workspace(config, transport=fake)
    assert restarted.command("reply", session="session-one", cwd=root, text="changed input") == {
        "acknowledged_event": 5,
        "reply_id": "posted-reply",
    }
    posts = [kw for m, p, kw in fake.calls if m == "POST" and p.endswith("/messages")]
    assert posts[0] == posts[1]
    assert posts[0]["session"] == "session-one" and posts[0]["key"] == operation["key"]
    assert len(fake.accepted) == 1 and state(config)["pending"] is None
    assert len([p for _, p, _ in fake.calls if p.endswith("/context")]) == 5
    # Exercise the real urllib redirect machinery without opening network sockets.
    requests = []
    original_opener = adapter.urllib.request.build_opener

    class RedirectResponse(adapter.urllib.request.HTTPHandler, adapter.urllib.request.HTTPSHandler):
        def http_open(self, request):
            requests.append(request)
            headers = Message()
            headers["Location"] = "http://untrusted.example/steal"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response

        https_open = http_open

    monkeypatch.setattr(
        adapter.urllib.request,
        "build_opener",
        lambda *handlers: original_opener(*handlers, RedirectResponse()),
    )
    for origin in ("http://127.0.0.1:8002", "https://workspace.example"):
        with pytest.raises(ApiError) as caught:
            adapter.Transport(origin, "PRIVATE_TOKEN").request(
                "POST",
                "threads/thread/messages",
                data={"text": "reply"},
                session="session-one",
                key="retry-key",
            )
        assert caught.value.status == 302
    assert len(requests) == 2  # neither cross-origin nor HTTPS downgrade is followed
    assert all(req.get_header("Authorization") == "Bearer PRIVATE_TOKEN" for req in requests)
    assert all(req.get_header("X-workspace-session") == "session-one" for req in requests)
    assert all(req.get_header("Idempotency-key") == "retry-key" for req in requests)
    with pytest.raises(adapter.AdapterError, match="HTTPS"):
        adapter.Transport("http://non-loopback.example", "PRIVATE_TOKEN")


def test_server_lease_conflict_is_silent_disconnected_and_release_keeps_binding(bundle):
    root, config = bundle
    fake = Fake()
    fake.denied = True
    workspace = adapter.Workspace(config, transport=fake)
    assert workspace.hook(payload(root)) is None
    report = workspace.command("status", cwd=root)
    assert report["connected_last_check"] is False and report["error"]
    assert not any(p.endswith("/inbox") for _, p, _ in fake.calls)
    fake.denied = False
    assert workspace.hook(payload(root)) is not None
    assert workspace.hook(payload(root, "SessionEnd")) is None
    assert state(config)["ended"] and not state(config)["connected"]
    assert state(config)["session_id"] == "session-one"
    count = len(fake.calls)
    assert workspace.hook(payload(root, session_id="new-session")) is None
    assert len(fake.calls) == count


def test_prepare_generates_private_session_settings_without_editing_any_settings(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    global_settings = tmp_path / "home" / ".claude" / "settings.json"
    global_settings.parent.mkdir(parents=True)
    global_settings.write_text('{"keep":"unchanged"}')
    project_settings = root / ".claude" / "settings.json"
    project_settings.parent.mkdir()
    project_settings.write_text('{"project":"unchanged"}')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    credentials = tmp_path / "agent.json"
    adapter.write_private(credentials, {"token": "UNSCOPED_PRIVATE_TOKEN"}, exclusive=True)
    fake = Fake()
    monkeypatch.setattr(adapter, "Transport", lambda *a, **k: fake)
    output = tmp_path / "bundle"
    report = adapter.prepare(
        credentials,
        output,
        url="http://127.0.0.1:8002",
        project_id="project",
        label="chosen session",
        cwd=root,
        script="examples/connect_claude.py",
        python=sys.executable,
    )
    settings = adapter.read_private(report["settings"])
    assert set(settings["hooks"]) == {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PostToolUse",
    }
    assert "PRIVATE_TOKEN" not in json.dumps(settings)
    assert all(
        "--config" in rules[0]["hooks"][0]["command"] for rules in settings["hooks"].values()
    )
    assert global_settings.read_text() == '{"keep":"unchanged"}'
    assert project_settings.read_text() == '{"project":"unchanged"}'
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in output.iterdir())
    count = len(fake.calls)
    with pytest.raises(FileExistsError):
        adapter.prepare(
            credentials,
            output,
            url="http://127.0.0.1:8002",
            project_id="project",
            label="chosen session",
            cwd=root,
            script=__file__,
            python=sys.executable,
        )
    assert len(fake.calls) == count
    fake.me = {**CONNECTION["actor"], "connection": CONNECTION}
    adapter.prepare(
        output / "credentials.json",
        tmp_path / "scoped",
        url=None,
        project_id=None,
        label=None,
        cwd=root,
        script=__file__,
        python=sys.executable,
    )
    assert len([p for _, p, _ in fake.calls if p == "agent-connections"]) == 1


def test_file_lock_prevents_parallel_hook_and_human_credential_cannot_prepare(
    bundle, tmp_path, monkeypatch
):
    root, config = bundle
    fake = Fake()
    first = adapter.Workspace(config, transport=fake)
    second = adapter.Workspace(config, transport=fake)
    with first.locked():
        assert second.hook(payload(root)) is None
    assert fake.calls == []
    fake.me = {"id": "human", "kind": "human"}
    monkeypatch.setattr(adapter, "Transport", lambda *a, **k: fake)
    with pytest.raises(adapter.AdapterError, match="human/admin"):
        adapter.prepare(
            adapter.read_private(config)["credentials"],
            tmp_path / "human",
            url="http://localhost",
            project_id="project",
            label="test",
            cwd=root,
            script=__file__,
            python=sys.executable,
        )
    assert not any(p == "agent-connections" for _, p, _ in fake.calls)


MUTED = "aaaaaaaa-0000-4000-8000-000000000001"


def test_muted_thread_is_skipped_without_context_and_old_state_loads(bundle):
    root, config = bundle
    fake = Fake()
    event = {**mention(5), "thread_id": MUTED}
    fake.events = [event, mention(6)]
    adapter.write_private(
        adapter.read_private(config)["checkpoint"],
        {k: v for k, v in adapter.initial_state(adapter.read_private(config)).items()
         if k != "muted_threads"},
    )  # fmt: skip
    clock = [1000]
    workspace = adapter.Workspace(config, transport=fake, now=lambda: clock[0])
    assert workspace.command("status", cwd=root)["muted_threads"] == []
    workspace.hook(payload(root))  # first hook: session binds, mention 5 pending
    assert state(config)["pending"]["event_id"] == 5
    # Muting the pending mention's thread clears it and advances the cursor.
    assert workspace.command("mute", session="session-one", cwd=root, thread_id=MUTED) == {
        "muted_threads": [MUTED]
    }
    assert state(config)["pending"] is None and state(config)["cursor"] == 5
    fake.calls.clear()
    fake.events = [event, mention(6), {**mention(7), "thread_id": MUTED}]
    clock[0] = 2000
    notice = workspace.hook(payload(root, "UserPromptSubmit"))
    assert state(config)["pending"]["event_id"] == 6 and notice
    workspace.command("ack", session="session-one", cwd=root)
    fake.calls.clear()
    clock[0] = 3000
    assert workspace.hook(payload(root, "UserPromptSubmit")) is None
    assert not [p for _, p, _ in fake.calls if p.endswith("/context")]
    assert state(config)["cursor"] == 7 and state(config)["pending"] is None
    assert workspace.command("unmute", session="session-one", cwd=root, thread_id=MUTED) == {
        "muted_threads": []
    }
    with pytest.raises(adapter.AdapterError, match="UUID"):
        workspace.command("mute", session="session-one", cwd=root, thread_id="nope")
