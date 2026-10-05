import json
import sys
from pathlib import Path

import pytest
from stubs import JsonHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
from agent_commons_client import ApiError, Client  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from two_clients import write_secret  # noqa: E402

JSON = "application/json"


@pytest.fixture
def server(stub_server):
    seen = {}

    class Handler(JsonHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            self.send_json({"items": [], "next_cursor": 12, "cursor": 18}, content_type=JSON)

        def do_POST(self):
            seen["key"] = self.headers.get("Idempotency-Key")
            seen["payload"] = self.read_json()
            self.send_json({"detail": "key conflicts with existing body"}, 409, JSON)

        def do_PUT(self):
            seen["method"] = self.command
            seen["path"] = self.path
            seen["payload"] = self.read_json()
            self.send_json({"reactions": []})

        do_DELETE = do_PUT

    return Client(stub_server(Handler), "example-token"), seen


def test_event_cursor_and_bearer_header(server):
    client, seen = server
    result = client.events("project id", after=7, limit=20)
    assert result["cursor"] == 18
    assert seen["path"] == "/v1/projects/project%20id/events?after=7&limit=20"
    assert seen["auth"] == "Bearer example-token"


def test_post_sends_idempotency_key_and_reports_http_conflict(server):
    client, seen = server
    with pytest.raises(ApiError) as caught:
        client.post_message("thread", "hello", idempotency_key="retry-1")
    assert caught.value.status == 409
    assert "conflicts" in caught.value.message
    assert seen["key"] == "retry-1"
    assert seen["payload"]["text"] == "hello"


def test_inbox_repeats_followed_threads_and_context_encodes_trigger(server):
    client, seen = server
    client.inbox("project", after=12, followed_thread_ids=["one", "two"])
    assert seen["path"] == (
        "/v1/projects/project/inbox?after=12&limit=50"
        "&followed_thread_ids=one&followed_thread_ids=two"
    )
    client.context("thread", trigger_message_id="trigger", limit=3, max_chars=1000)
    assert seen["path"] == (
        "/v1/threads/thread/context?limit=3&max_chars=1000&trigger_message_id=trigger"
    )


def test_reply_and_mentions_are_explicit_and_reaction_delete_keeps_json_body(server):
    client, seen = server
    with pytest.raises(ApiError):
        client.post_message("thread", "reply", reply_to="parent", mentions=["agent"])
    assert seen["payload"]["reply_to"] == "parent"
    assert seen["payload"]["mentions"] == ["agent"]
    client.add_reaction("message", "👍")
    assert seen["method"] == "PUT"
    assert seen["path"] == "/v1/messages/message/reactions"
    assert seen["payload"] == {"emoji": "👍"}
    client.remove_reaction("message", "👍")
    assert seen["method"] == "DELETE"
    assert seen["payload"] == {"emoji": "👍"}


def test_post_does_not_add_null_reply_parent_to_legacy_metadata(server):
    client, seen = server
    with pytest.raises(ApiError):
        client.post_message("thread", "legacy", metadata={"reply_to": "parent"})
    assert "reply_to" not in seen["payload"]
    assert seen["payload"]["metadata"] == {"reply_to": "parent"}


def test_demo_credentials_are_private_and_never_overwrite_an_existing_token(tmp_path):
    credentials = tmp_path / "per-run" / "agent.json"
    original = {"token": "first-token", "actor": {"id": "actor"}}
    write_secret(credentials, original)
    with pytest.raises(FileExistsError):
        write_secret(credentials, {"token": "different-token"})
    assert json.loads(credentials.read_text()) == original
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert credentials.parent.stat().st_mode & 0o777 == 0o700
