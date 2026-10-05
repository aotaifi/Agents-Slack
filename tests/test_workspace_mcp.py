"""The stdio MCP server, run as a real subprocess against a loopback stub."""

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "workspace"
SERVER = PLUGIN / "scripts" / "mcp_server.py"
sys.path.insert(0, str(PLUGIN / "lib"))
sys.path.insert(0, str(Path(__file__).parent))
import wsplugin  # noqa: E402
from test_workspace_plugin import CONNECTION, SID, TOKEN, args  # noqa: E402

THREAD = "aaaaaaaa-0000-4000-8000-000000000001"
TRIGGER = "bbbbbbbb-0000-4000-8000-000000000002"
ROOT = "cccccccc-0000-4000-8000-000000000003"
OTHER = "dddddddd-0000-4000-8000-000000000004"
LONG = "word " * 200  # 1000 characters


class Stub(BaseHTTPRequestHandler):
    log = []
    foreign_thread = False

    def _send(self, body, code=200):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        Stub.log.append(("GET", self.path, dict(self.headers)))
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path.endswith("/inbox"):
            if query["after"] == ["0"]:
                return self._send({"items": [], "next_cursor": None, "cursor": 7})
            item = {
                "id": 8,
                "type": "message.created",
                "thread_id": THREAD,
                "payload": {
                    "id": TRIGGER, "reply_to": ROOT, "author": {"id": "human"},
                    "mentions": ["agent"], "text": "ignored",
                },
            }  # fmt: skip
            return self._send({"items": [item], "next_cursor": None, "cursor": 8})
        if url.path.endswith("/context"):
            project = "elsewhere" if Stub.foreign_thread else "project"
            trigger = query.get("trigger_message_id", [TRIGGER])[0]
            author = {"id": "human", "handle": "ana"}
            cap = int(query["max_chars"][0])  # like the real server, clip to the budget
            cap = 300 if cap <= 2000 else cap  # small share for the adapter-sized request
            full = "Please review " + "x" * 7000
            return self._send(
                {
                    "thread": {"id": THREAD, "project_id": project, "title": "Question"},
                    "rules": {"text": "Be brief.", "version": 1},
                    "messages": [
                        {"id": trigger, "text": full[:cap], "truncated": len(full) > cap,
                         "author": author, "reply_to": ROOT,
                         "created_at": "2026-01-01T00:00:00+00:00",
                         "metadata": {"private": 1}},
                    ],  # fmt: skip
                    "trigger_message": {"id": trigger, "text": "Please review", "reply_to": ROOT},
                    "parent_message": None,
                    "has_older": False,
                }
            )
        if url.path == "/v1/me":
            return self._send({**CONNECTION["actor"], "connection": CONNECTION})
        self._send({}, 404)

    def _write(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        Stub.log.append((method, self.path, body, dict(self.headers)))
        if self.path.endswith("/claim"):
            return self._send({"connection": {**CONNECTION, "bound": True, "active": True}})
        if self.path.endswith("/messages"):
            return self._send({"id": "posted"}, 201)
        if self.path.endswith("/reactions"):
            return self._send({"id": OTHER, "project_id": "project"})
        self._send({}, 404)

    do_POST = lambda self: self._write("POST")  # noqa: E731
    do_PUT = lambda self: self._write("PUT")  # noqa: E731

    def log_message(self, *a):
        pass


@pytest.fixture
def stub():
    Stub.log, Stub.foreign_thread = [], False
    httpd = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
def home(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return tmp_path / "home", work


class Client:
    def __init__(self, base, work, session=SID):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env.update(WORKSPACE_PLUGIN_HOME=str(base), CLAUDE_PROJECT_DIR=str(work))
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True, cwd=work,
        )  # fmt: skip
        self.seen, self.n = [], 0

    def send_raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params=None):
        self.n += 1
        self.send_raw(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                  "params": params or {}}))  # fmt: skip
        line = self.proc.stdout.readline()
        self.seen.append(line)
        return json.loads(line)

    def tool(self, name, **arguments):
        reply = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return reply["result"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.seen.append(self.proc.stdout.read())
        return self.proc.stderr.read()


@pytest.fixture
def make(home):
    clients = []

    def build(session=SID):
        c = Client(*home, session=session)
        clients.append(c)
        return c

    yield build
    for c in clients:
        if c.proc.poll() is None:
            c.proc.kill()


@pytest.fixture
def connected(home, stub, tmp_path):
    base, work = home
    cred = tmp_path / "connection.json"
    cred.write_text(json.dumps({"token": TOKEN, "connection": CONNECTION}))
    cred.chmod(0o600)
    os.environ["WORKSPACE_PLUGIN_HOME"] = str(base)
    try:
        wsplugin.connect(args(credentials=str(cred), url=stub), SID, str(work), base=base)
    finally:
        del os.environ["WORKSPACE_PLUGIN_HOME"]
    Stub.log.clear()
    return base, work


def body(result):
    return json.loads(result["content"][0]["text"])


def posts(path_end):
    return [e for e in Stub.log if e[0] == "POST" and e[1].endswith(path_end)]


# ---- protocol ----


def test_initialize_negotiation_and_instructions(make):
    c = make()
    for asked, expected in (("2025-03-26", "2025-03-26"), ("2099-01-01", "2024-11-05")):
        r = c.rpc("initialize", {"protocolVersion": asked})["result"]
        assert r["protocolVersion"] == expected and r["capabilities"] == {"tools": {}}
        assert r["serverInfo"]["name"] == "workspace"
    text = (PLUGIN / "skills" / "guide" / "SKILL.md").read_text()
    assert r["instructions"] == re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()
    c.send_raw(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert c.rpc("ping")["result"] == {}
    c.close()


def test_tools_list_unknown_method_and_garbage_line(make):
    c = make()
    tools = c.rpc("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "status", "check_mentions", "read_thread", "reply", "dismiss", "react",
    ]  # fmt: skip
    for t in tools:
        assert t["inputSchema"]["additionalProperties"] is False and t["description"]
    by = {t["name"]: t["inputSchema"] for t in tools}
    assert by["reply"]["required"] == ["text"] and by["read_thread"]["properties"]["limit"][
        "maximum"
    ] == 20
    assert c.rpc("nope/nothing")["error"]["code"] == -32601
    c.send_raw("this is not json")
    assert c.rpc("ping")["result"] == {}  # still alive
    c.close()
    assert all(json.loads(x) for x in c.seen if x.strip())  # stdout is only JSON-RPC


def test_missing_session_id_is_a_tool_error_for_every_tool(make, stub):
    c = make(session=None)
    for name, a in (
        ("status", {}), ("check_mentions", {}), ("read_thread", {"thread_id": THREAD}),
        ("reply", {"text": "hi"}), ("dismiss", {}),
        ("react", {"message_id": OTHER, "emoji": "👍"}),
    ):  # fmt: skip
        r = c.tool(name, **a)
        assert r["isError"] and "/workspace:" in r["content"][0]["text"]
    assert Stub.log == []


def test_unconnected_session_makes_no_requests(make, stub):
    c = make()
    assert body(c.tool("status")) == {"connected": False}
    for name, a in (
        ("check_mentions", {}), ("read_thread", {"thread_id": THREAD}),
        ("reply", {"text": "hi"}), ("dismiss", {}),
        ("react", {"message_id": OTHER, "emoji": "👍"}),
    ):  # fmt: skip
        r = c.tool(name, **a)
        assert r["isError"] and "/workspace:connect" in r["content"][0]["text"]
    assert Stub.log == []


# ---- connected session ----


def test_check_mentions_returns_pending_with_context(make, connected):
    c = make()
    out = body(c.tool("check_mentions"))["pending"]
    assert out["message_id"] == TRIGGER
    assert out["context"]["rules"]["text"] == "Be brief."
    c.close()
    assert all(TOKEN not in s for s in c.seen)


def test_long_reply_refused_unless_detailed(make, connected):
    c = make()
    c.tool("check_mentions")
    Stub.log.clear()
    r = c.tool("reply", text=LONG)
    assert r["isError"] and "Too long for a default reply (1000 chars, limit 600)" in (
        r["content"][0]["text"]
    )
    assert Stub.log == []
    r = c.tool("reply", text=LONG, detailed=True)
    assert "isError" not in r and body(r)["reply_id"] == "posted"
    assert len(posts("/messages")) == 1
    r = c.tool("reply", text="x" * 20001, detailed=True)
    assert r["isError"]


def test_short_reply_posts_with_key_and_reply_to_then_dismiss_acks(make, connected):
    c = make()
    c.tool("check_mentions")
    r = c.tool("reply", text="Looks good.")
    assert body(r)["reply_id"] == "posted"
    (post,) = posts("/messages")
    assert post[2]["reply_to"] == ROOT and post[2]["text"] == "Looks good."
    assert {k.lower() for k in post[3]} >= {"idempotency-key", "x-workspace-session"}
    assert post[3]["X-Workspace-Session"] == SID
    c.close()
    assert all(TOKEN not in s for s in c.seen)


def test_dismiss_acks_without_posting(make, connected):
    c = make()
    c.tool("check_mentions")
    Stub.log.clear()
    assert body(c.tool("dismiss"))["acknowledged_event"] == 8
    assert posts("/messages") == [] and posts("/claim")
    assert c.tool("dismiss")["isError"]  # nothing pending any more


def test_react_claims_lease_and_sends_session_header(make, connected):
    c = make()
    r = c.tool("react", message_id=OTHER, emoji="👍")
    assert body(r)["reacted"] == "👍"
    kinds = [e[0] + " " + e[1] for e in Stub.log]
    assert kinds[0].endswith("/claim") and kinds[1] == f"PUT /v1/messages/{OTHER}/reactions"
    put = Stub.log[1]
    assert put[2] == {"emoji": "👍"} and put[3]["X-Workspace-Session"] == SID
    assert c.tool("react", message_id=OTHER, emoji="💥")["isError"]
    assert c.tool("react", message_id="not-a-uuid", emoji="👍")["isError"]
    c.close()
    assert all(TOKEN not in s for s in c.seen)


def test_read_thread_is_thin_bounded_and_project_scoped(make, connected):
    c = make()
    out = body(c.tool("read_thread", thread_id=THREAD, limit=5))
    (message,) = out["messages"]
    assert set(message) == {"id", "author", "text", "reply_to", "created_at"}
    assert message["author"] == "ana" and "private" not in json.dumps(out)
    assert "note" in out
    get = [e for e in Stub.log if "/context" in e[1]][-1]
    assert "limit=5" in get[1] and "max_chars=6000" in get[1]
    assert c.tool("read_thread", thread_id=THREAD, limit=21)["isError"]
    Stub.foreign_thread = True
    assert "not in the connected project" in c.tool("read_thread", thread_id=THREAD)[
        "content"
    ][0]["text"]


def test_unknown_argument_is_rejected(make, connected):
    c = make()
    r = c.tool("dismiss", surprise=1)
    assert r["isError"]
