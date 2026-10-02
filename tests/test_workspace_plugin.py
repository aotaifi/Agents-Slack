"""Plugin layer: session isolation, connection setup, tunnel ownership, adapter parity."""

import argparse
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "workspace"
sys.path.insert(0, str(PLUGIN / "lib"))
import claude_workspace as adapter  # noqa: E402
import ws_tunnel  # noqa: E402
import wsplugin  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
TOKEN = "SCOPED_PRIVATE_TOKEN_ABC123"
CONNECTION = {
    "id": "connection",
    "actor": {"id": "agent", "kind": "agent", "handle": "owner.agent"},
    "project": {"id": "project", "name": "Research"},
    "label": "laptop",
    "bound": False,
    "active": False,
    "revoked": False,
}


class Stub(BaseHTTPRequestHandler):
    log = []
    kind = "agent"

    def _send(self, body, code=200):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        Stub.log.append(("GET", self.path))
        if "/inbox" in self.path:
            return self._send({"items": [], "next_cursor": None, "cursor": 7})
        if self.path == "/v1/me":
            return self._send({**CONNECTION["actor"], "kind": Stub.kind, "connection": CONNECTION})
        self._send({}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        Stub.log.append(("POST", self.path, body))
        if self.path.endswith("/claim"):
            return self._send({"connection": {**CONNECTION, "bound": True, "active": True}})
        if self.path.endswith("/release"):
            return self._send({"connection": {**CONNECTION, "bound": True, "active": False}})
        self._send({}, 404)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    Stub.log, Stub.kind = [], "agent"
    httpd = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = tmp_path / "home"
    monkeypatch.setenv("WORKSPACE_PLUGIN_HOME", str(base))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    work = tmp_path / "work"
    work.mkdir()
    cred = tmp_path / "connection.json"
    cred.write_text(json.dumps({"token": TOKEN, "connection": CONNECTION}))
    cred.chmod(0o644)  # browser downloads are not 0600
    return base, work, cred


def args(**kw):
    ns = dict(
        credentials=None, import_config=None, url=None, project=None, label=None,
        replay_backlog=False, tunnel=False, ssh=None, ssh_user=None, ssh_host=None,
        ssh_port=None, local_port=None, remote_host=None, remote_port=None, ssh_option=None,
    )  # fmt: skip
    ns.update(kw)
    return argparse.Namespace(**ns)


def hook_payload(work, name="UserPromptSubmit", **kw):
    return {"session_id": SID, "cwd": str(work), "hook_event_name": name, **kw}


class Boom:
    def request(self, *a, **k):
        raise AssertionError("network used")


# ---- session isolation ----


def test_session_id_comes_only_from_claude(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with pytest.raises(adapter.AdapterError, match="did not supply"):
        wsplugin.require_session(None)
    with pytest.raises(adapter.AdapterError, match="did not supply"):
        wsplugin.require_session("${CLAUDE_SESSION_ID}")  # unsubstituted placeholder
    assert wsplugin.require_session("abc") == "abc"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "one")
    with pytest.raises(adapter.AdapterError, match="Conflicting"):
        wsplugin.require_session("two")
    assert wsplugin.require_session("one") == "one"


def test_plugin_layer_never_loads_state_for_unconnected_sessions_or_subagents(
    env, server, monkeypatch
):
    base, work, cred = env
    loads = []
    real = wsplugin.load
    monkeypatch.setattr(wsplugin, "load", lambda *a, **k: loads.append(a) or real(*a, **k))
    assert wsplugin.run_hook(hook_payload(work), transport=Boom(), base=base) is None
    wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)
    loads.clear()
    for payload in (
        hook_payload(work, agent_id="child"),
        {**hook_payload(work), "session_id": "other-session"},
    ):
        assert wsplugin.run_hook(payload, transport=Boom(), base=base) is None
    assert loads == []  # no config/credential/checkpoint read at all


def test_unconnected_session_and_subagents_make_no_requests(env, server):
    base, work, cred = env
    # Nothing connected: no request, no files created beyond the home dir.
    assert wsplugin.run_hook(hook_payload(work), transport=Boom(), base=base) is None
    wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)
    n = len(Stub.log)
    for payload in (
        hook_payload(work, agent_id="child"),
        {**hook_payload(work), "session_id": "other-session"},  # a different conversation
        {**hook_payload(work), "hook_event_name": "Stop"},
        {"hook_event_name": "PostToolUse"},
    ):
        assert wsplugin.run_hook(payload, transport=Boom(), base=base) is None
    assert len(Stub.log) == n


def test_sibling_conversation_in_same_folder_is_not_connected(env, server):
    base, work, cred = env
    wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)
    assert wsplugin.status("another-session", base=base) == {"connected": False}
    with pytest.raises(adapter.AdapterError, match="not connected"):
        wsplugin.act("ack", "another-session", work, base=base)


# ---- connection setup ----


def test_connect_binds_actual_session_privately_without_leaking_token(env, server, capsys):
    base, work, cred = env
    out = wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)
    assert out["connected"] and TOKEN not in json.dumps(out)
    d = wsplugin.session_dir(SID, base)
    assert (d.stat().st_mode & 0o777) == 0o700
    for f in d.iterdir():
        assert (f.stat().st_mode & 0o777) == 0o600, f
    assert wsplugin.session_hash(SID) in str(d) and SID not in str(d)
    state = adapter.read_private(d / "checkpoint.json")
    assert state["session_id"] == SID and state["connected"]
    assert state["initialized"] is True and state["cursor"] == 7  # inbox starts at connect
    claims = [e for e in Stub.log if e[0] == "POST" and e[1].endswith("/claim")]
    assert claims[0][2] == {"session_id": SID}
    assert not (d / "settings.json").exists()
    assert not list(base.glob("stage-*"))  # staging copy removed
    with pytest.raises(adapter.AdapterError, match="already connected"):
        wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)


def test_human_or_admin_credentials_and_bad_files_are_rejected_and_clean_up(env, server):
    base, work, cred = env
    Stub.kind = "human"
    with pytest.raises(adapter.AdapterError, match="human/admin"):
        wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)
    assert not wsplugin.session_dir(SID, base).exists()
    bad = cred.parent / "bad.json"
    bad.write_text(json.dumps({"no": "token"}))
    with pytest.raises(adapter.AdapterError, match="not a scoped"):
        wsplugin.read_source(bad)
    link = cred.parent / "link.json"
    link.symlink_to(cred)
    with pytest.raises(adapter.AdapterError, match="regular"):
        wsplugin.read_source(link)


def test_failed_claim_leaves_nothing_behind(env, server, monkeypatch):
    base, work, cred = env
    monkeypatch.setattr(Stub, "do_POST", lambda self: self._send({}, 409))
    with pytest.raises(Exception):
        wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)
    assert not wsplugin.session_dir(SID, base).exists()


def test_discover_lists_metadata_never_tokens(env):
    base, work, cred = env
    found = wsplugin.discover(cred.parent)
    assert found[0]["project"] == "Research" and found[0]["agent"] == "owner.agent"
    assert TOKEN not in json.dumps(found)


def test_import_only_for_the_exact_bound_session(env, server, tmp_path):
    base, work, cred = env
    private = tmp_path / "old"
    adapter.prepare(
        cred.parent / "connection.json" if False else _private_copy(cred, tmp_path), private,
        url=server, project_id=None, label=None, cwd=work, script="x.py", python="python3",
    )  # fmt: skip
    config = private / "config.json"
    ws = adapter.Workspace(config)
    with ws.locked():
        ws.save(session_id=SID)
    with pytest.raises(adapter.AdapterError, match="different"):
        wsplugin.import_config(config, "someone-else", str(work), tmp_path / "x")
    d = wsplugin.session_dir(SID, base)
    out = wsplugin.import_config(config, SID, str(work), d)
    assert out["imported"] and wsplugin.resolve_config(d) == config.resolve()
    # unclaimed adapters never import
    private2 = tmp_path / "old2"
    adapter.prepare(
        _private_copy(cred, tmp_path, "c2.json"), private2, url=server, project_id=None,
        label=None, cwd=work, script="x.py", python="python3",
    )  # fmt: skip
    with pytest.raises(adapter.AdapterError, match="different"):
        wsplugin.import_config(private2 / "config.json", SID, str(work), tmp_path / "y")
    # disconnect removes only the link, never the original bundle
    wsplugin.disconnect(SID, base=base)
    assert config.exists() and not d.exists()


def _private_copy(src, tmp_path, name="c.json"):
    dst = tmp_path / name
    dst.write_text(src.read_text())
    dst.chmod(0o600)
    return dst


# ---- adapter compatibility through the plugin ----


def test_hook_notifies_inbox_reply_roundtrip_via_plugin(env, server):
    base, work, cred = env
    sys.path.insert(0, str(Path(__file__).parent))
    from test_claude_workspace import Fake

    fake = Fake()
    wsplugin.connect(
        args(credentials=str(cred), url=server, replay_backlog=True), SID, str(work), base=base
    )
    out = wsplugin.run_hook(hook_payload(work), transport=fake, base=base)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "New workspace mention" in text and "ws.py" in text and "--config" not in text
    box = wsplugin.inbox(SID, work, transport=fake, base=base)
    assert box["pending"]["message_id"] == "message-5"
    res = wsplugin.act("reply", SID, work, text="hello", transport=fake, base=base)
    assert res["reply_id"] == "posted-reply" and fake.accepted
    assert wsplugin.inbox(SID, work, transport=fake, base=base) == {"pending": None}


def test_session_end_hook_releases_lease(env, server):
    base, work, cred = env
    wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)
    from test_claude_workspace import Fake

    fake = Fake()
    wsplugin.run_hook(hook_payload(work, "SessionEnd"), transport=fake, base=base)
    assert any(p.endswith("/release") for _, p, _ in fake.calls)


def test_vendored_adapter_matches_repository_copy():
    for name in ("claude_workspace.py", "agent_commons_client.py"):
        root = Path(__file__).resolve().parents[1]
        assert (PLUGIN / "lib" / name).read_bytes() == (root / "clients/python" / name).read_bytes()


# ---- tunnels ----


class FakeProc:
    def __init__(self, pid, returncode=None):
        self.pid, self.returncode = pid, returncode

    def poll(self):
        return self.returncode


@pytest.fixture
def tun(tmp_path, monkeypatch):
    live = {}
    killed = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    monkeypatch.setattr(ws_tunnel, "proc_argv", lambda pid: live.get(pid))
    monkeypatch.setattr(ws_tunnel.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(ws_tunnel, "port_is_free", lambda p: True)

    def spawn(argv, **kw):
        live[4242] = argv
        return FakeProc(4242)

    t = ws_tunnel.Tunnels(tmp_path / "h", spawn=spawn, ready_timeout=2)
    yield t, port, live, killed
    listener.close()


def test_argv_binds_loopback_keeps_host_key_checks_and_validates_input():
    argv = ws_tunnel.build_argv("alice@host", 8002, "127.0.0.1", 18000, ssh_port=22)
    assert "127.0.0.1:8002:127.0.0.1:18000" in argv[argv.index("-L") + 1]
    joined = " ".join(argv)
    assert "StrictHostKeyChecking" not in joined and "UserKnownHostsFile" not in joined
    assert "ExitOnForwardFailure=yes" in joined and argv[-2:] == ["--", "alice@host"]
    for bad in ("-oProxyCommand=x", "a b", "", "host;rm"):
        with pytest.raises(ws_tunnel.TunnelError):
            ws_tunnel.build_argv(bad, 8002, "127.0.0.1", 18000)
    with pytest.raises(ws_tunnel.TunnelError):
        ws_tunnel.build_argv("h", 99999, "127.0.0.1", 18000)
    with pytest.raises(ws_tunnel.TunnelError):
        ws_tunnel.build_argv("h", 8002, "127.0.0.1", 18000, options=["bad option"])


def test_tunnel_shared_by_two_sessions_stops_only_with_last_owner(tun):
    t, port, live, killed = tun
    assert t.up("A", "alice@h", "127.0.0.1", 18000, local_port=port) == port
    assert t.up("B", "alice@h", "127.0.0.1", 18000, local_port=port) == port
    assert t.release("A", port) == "kept-shared" and killed == []
    assert t.release("B", port) == "stopped" and killed == [4242]
    assert t.read(port) is None


def test_release_never_signals_a_process_that_is_not_our_ssh(tun):
    t, port, live, killed = tun
    t.up("A", "alice@h", "127.0.0.1", 18000, local_port=port)
    live[4242] = ["ssh", "-L", "9999:other:1", "unrelated"]  # pid recycled / foreign
    assert t.release("A", port) == "already-gone" and killed == []


def test_different_target_on_our_port_and_foreign_program_are_clear_errors(tun, monkeypatch):
    t, port, live, killed = tun
    t.up("A", "alice@h", "127.0.0.1", 18000, local_port=port)
    with pytest.raises(ws_tunnel.TunnelError, match="another integration tunnel"):
        t.up("B", "bob@h", "127.0.0.1", 18000, local_port=port)
    t2 = ws_tunnel.Tunnels(t.dir.parent / "h2")
    monkeypatch.setattr(ws_tunnel, "port_is_free", lambda p: False)
    with pytest.raises(ws_tunnel.TunnelError, match="already in use by another program"):
        t2.up("A", "alice@h", "127.0.0.1", 18000, local_port=port)
    assert t2.read(port) is None


@pytest.mark.parametrize(
    "stderr,needle",
    [
        ("Host key verification failed.", "never disabled"),
        ("alice@h: Permission denied (publickey).", "BatchMode"),
        ("bind: Address already in use", "another --local-port"),
    ],
)
def test_ssh_failures_are_explained(tmp_path, monkeypatch, stderr, needle):
    monkeypatch.setattr(ws_tunnel, "port_is_free", lambda p: True)
    monkeypatch.setattr(ws_tunnel, "port_accepts", lambda p, timeout=0.5: False)

    def spawn(argv, **kw):
        os.write(kw["stderr"], stderr.encode())
        return FakeProc(1, returncode=255)

    t = ws_tunnel.Tunnels(tmp_path / "h", spawn=spawn, ready_timeout=2)
    with pytest.raises(ws_tunnel.TunnelError, match=needle):
        t.up("A", "alice@h", "127.0.0.1", 18000, local_port=8123)
    assert t.read(8123) is None


def test_connect_with_tunnel_uses_tunnel_url_and_disconnect_stops_only_ours(
    env, server, monkeypatch, tun
):
    base, work, cred = env
    t, port, live, killed = tun
    host, srv_port = server.rsplit(":", 1)
    # Fake ssh "forwards" to the stub server by making the tunnel port the stub's own port.
    t.dir = Path(base) / "tunnels"
    t.dir.mkdir(parents=True, exist_ok=True)
    out = wsplugin.connect(
        args(credentials=str(cred), ssh="alice@h", local_port=int(srv_port)),
        SID, str(work), tunnels=t, base=base,
    )  # fmt: skip
    assert out["tunnel_port"] == int(srv_port) and out["url"] == server
    assert wsplugin.status(SID, tunnels=t, base=base)["tunnel"]["alive"]
    unrelated = 31337
    live[unrelated] = ["ssh", "-N", "someone-else"]
    res = wsplugin.disconnect(SID, tunnels=t, base=base)
    assert res["tunnel"] == "stopped" and killed == [4242] and unrelated not in killed
    assert unrelated in live
    assert any(e[0] == "POST" and e[1].endswith("/release") for e in Stub.log)


def test_tunnel_with_conflicting_url_is_refused_and_released(env, server, tun):
    base, work, cred = env
    t, port, live, killed = tun
    with pytest.raises(adapter.AdapterError, match="URL must be"):
        wsplugin.connect(
            args(credentials=str(cred), ssh="alice@h", local_port=port, url="http://127.0.0.1:1"),
            SID, str(work), tunnels=t, base=base,
        )  # fmt: skip
    assert t.read(port) is None and killed == [4242]


def test_ssh_options_cannot_weaken_host_keys_or_run_commands():
    for bad in (
        "StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null", "ProxyCommand=sh${IFS}-c",
        "LocalCommand=id", "PermitLocalCommand=yes", "RemoteForward=9000:127.0.0.1:22",
    ):  # fmt: skip
        with pytest.raises(ws_tunnel.TunnelError):
            ws_tunnel.build_argv("h", 8002, "127.0.0.1", 18000, options=[bad])
    argv = ws_tunnel.build_argv("h", 8002, "127.0.0.1", 18000, options=["ConnectTimeout=5"])
    assert "ConnectTimeout=5" in argv


def test_token_never_sent_when_tunnel_is_dead(env, server, tun):
    base, work, cred = env
    t, port, live, killed = tun
    _, srv_port = server.rsplit(":", 1)
    t.dir = Path(base) / "tunnels"
    t.dir.mkdir(parents=True, exist_ok=True)
    wsplugin.connect(
        args(credentials=str(cred), ssh="alice@h", local_port=int(srv_port)),
        SID, str(work), tunnels=t, base=base,
    )  # fmt: skip
    live.clear()  # ssh died; the stub (a stand-in for a stranger) still answers on the port
    n = len(Stub.log)
    with pytest.raises(adapter.AdapterError, match="Tunnel is down"):
        wsplugin.load(SID, base=base)
    assert wsplugin.run_hook(hook_payload(work, "SessionStart"), base=base) is None
    assert len(Stub.log) == n


def test_reply_refuses_credential_text_and_inbox_surfaces_errors(env, server):
    base, work, cred = env
    from test_claude_workspace import Fake

    wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)
    fake = Fake()
    d = wsplugin.session_dir(SID, base)
    secret = adapter.read_private(d / "credentials.json")["token"]
    fake.events = []
    with pytest.raises(adapter.AdapterError, match="credential"):
        wsplugin.act("reply", SID, work, text=f"x {secret}", transport=fake, base=base)
    state = adapter.read_private(d / "checkpoint.json")
    adapter.write_private(d / "checkpoint.json", {**state, "last_poll": 0})
    fake.denied = True  # claim fails -> hook records the error
    out = wsplugin.inbox(SID, work, transport=fake, base=base)
    assert out["pending"] is None and out["error"]


def test_ssh_account_is_remembered_after_first_tunnel_connect(env, server, tun):
    base, work, cred = env
    t, port, live, killed = tun
    _, srv = server.rsplit(":", 1)
    t.dir = Path(base) / "tunnels"
    t.dir.mkdir(parents=True, exist_ok=True)
    assert wsplugin.load_profile(base) is None
    wsplugin.connect(
        args(credentials=str(cred), ssh="alice@h", local_port=int(srv)),
        SID, str(work), tunnels=t, base=base,
    )  # fmt: skip
    assert wsplugin.load_profile(base)["target"] == "alice@h"
    assert "token" not in json.dumps(wsplugin.load_profile(base)).lower()
    wsplugin.disconnect(SID, tunnels=t, base=base)
    # Second connect: no ssh flags at all, profile supplies the account.
    spec = wsplugin.tunnel_spec(args(local_port=int(srv)), wsplugin.defaults(), base)
    assert spec["target"] == "alice@h" and spec["from_profile"]
    assert wsplugin.tunnel_spec(args(no_tunnel=True), wsplugin.defaults(), base) is None
    assert wsplugin.tunnel_spec(args(url="http://127.0.0.1:1"), wsplugin.defaults(), base) is None
    out = wsplugin.connect(
        args(credentials=str(cred), local_port=int(srv)), SID, str(work), tunnels=t, base=base
    )
    assert out["tunnel_port"] == int(srv)


def test_mention_posted_right_after_connect_is_delivered_on_next_prompt(env, server):
    """Regression: the first poll used to only set the start point and skip this mention."""
    base, work, cred = env
    from test_claude_workspace import Fake, mention

    wsplugin.connect(args(credentials=str(cred), url=server), SID, str(work), base=base)
    fake = Fake()
    fake.events = [mention(8)]  # posted after connect (server cursor was 7)
    out = wsplugin.run_hook(hook_payload(work), transport=fake, base=base)
    assert out and "New workspace mention" in out["hookSpecificOutput"]["additionalContext"]
