"""Claude Code plugin layer over the scoped session adapter (claude_workspace).

One private directory per connected Claude session, named by the hash of the session id
Claude itself supplies. A session without such a directory never touches the network.
Hooks only deliver notifications; nothing here writes replies, starts models or wakes
closed sessions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

from agent_commons_client import ApiError
from claude_workspace import (
    AdapterError,
    Workspace,
    prepare,
    read_private,
    write_private,
)
from ws_tunnel import TunnelError, Tunnels

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "ws.py"
EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "SessionEnd")


def home():
    # Not CLAUDE_PLUGIN_DATA: hooks receive it but the Bash tool may not, and both must agree.
    base = os.environ.get("WORKSPACE_PLUGIN_HOME")
    path = Path(base) if base else Path.home() / ".config" / "claude-workspace"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def defaults():
    return json.loads((PLUGIN_ROOT / "defaults.json").read_text())


def session_hash(session_id):
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def session_dir(session_id, base=None):
    return (base or home()) / "sessions" / session_hash(session_id)


def require_session(explicit=None, env=None):
    """The session id comes only from Claude; the folder is never consulted."""
    env = os.environ if env is None else env
    values = {
        v for v in (env.get("CLAUDE_CODE_SESSION_ID"), explicit) if v and not v.startswith("${")
    }
    if len(values) > 1:
        raise AdapterError("Conflicting session ids from Claude; refusing to guess.")
    if not values:
        raise AdapterError(
            "Claude did not supply a session id (CLAUDE_CODE_SESSION_ID). "
            "Run this from inside the Claude conversation to connect."
        )
    sid = values.pop()
    if not 1 <= len(sid) <= 200 or any(not 33 <= ord(c) <= 126 for c in sid):
        raise AdapterError("Claude supplied an invalid session id.")
    return sid


def resolve_config(directory):
    """Return the adapter config path for a connected session directory, or None."""
    own = directory / "config.json"
    if own.is_file():
        return own
    meta = directory / "session.json"
    if meta.is_file():
        link = json.loads(meta.read_text()).get("link")
        if link:
            return Path(link)
    return None


def load(session_id, *, transport=None, base=None):
    directory = session_dir(session_id, base)
    config = resolve_config(directory) if directory.is_dir() else None
    if config is None:
        raise AdapterError("This Claude session is not connected. Use /workspace:connect.")
    spec = meta_of(directory).get("tunnel")
    if spec and transport is None:
        found = Tunnels(base or home()).status(spec["local_port"])
        if not (found and found["alive"]):
            # Never send the token to whatever else may now listen on that port.
            raise AdapterError("Tunnel is down; run `ws.py tunnel up` (or reconnect).")
    workspace = Workspace(config, transport=transport)
    workspace.config["command"] = f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))}"
    return workspace, directory


def meta_of(directory):
    path = directory / "session.json"
    return json.loads(path.read_text()) if path.is_file() else {}


# ---- hooks -----------------------------------------------------------------------------


def run_hook(payload, *, transport=None, base=None):
    """Quiet no-op for subagents and for every session that was not explicitly connected."""
    try:
        if (
            not isinstance(payload, dict)
            or "agent_id" in payload
            or payload.get("hook_event_name") not in EVENTS
        ):
            return None
        sid = payload.get("session_id")
        if not isinstance(sid, str) or not sid:
            return None
        directory = session_dir(sid, base)
        if not directory.is_dir():  # unconnected: no further reads, no requests
            return None
        workspace, _ = load(sid, transport=transport, base=base)
        return workspace.hook(payload)
    except Exception:
        return None


# ---- connection setup ------------------------------------------------------------------


def read_source(path):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise AdapterError("Credential must be a regular JSON file under 64 KB.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise AdapterError("Credential file is not valid JSON.") from None
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        raise AdapterError("This is not a scoped connection credential file.")
    return data


def discover(directory=None, limit=10):
    """List candidate downloaded connection files; never includes a token."""
    root = Path(directory).expanduser() if directory else Path.home() / "Downloads"
    found = []
    if root.is_dir():
        for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[
            :100
        ]:
            try:
                data = read_source(path)
            except (AdapterError, OSError):
                continue
            connection = data.get("connection") or {}
            if not connection:
                continue
            found.append(
                {
                    "path": str(path),
                    "label": connection.get("label"),
                    "project": (connection.get("project") or {}).get("name"),
                    "agent": (connection.get("actor") or {}).get("handle"),
                    "bound": connection.get("bound"),
                    "revoked": connection.get("revoked"),
                    "url": data.get("url"),
                }
            )
    return found[:limit]


def tunnel_spec(args, cfg):
    explicit = args.ssh or args.ssh_user or args.tunnel
    if not explicit:
        return None
    target = args.ssh
    if not target:
        if not args.ssh_user:
            raise AdapterError(
                "A tunnel needs your SSH account: --ssh ALIAS_OR_USER@HOST or --ssh-user USER."
            )
        target = f"{args.ssh_user}@{args.ssh_host or cfg['ssh_host']}"
    return {
        "target": target,
        "remote_host": args.remote_host or cfg["remote_host"],
        "remote_port": args.remote_port or cfg["remote_port"],
        "local_port": args.local_port,
        "ssh_port": args.ssh_port,
        "options": list(args.ssh_option or []),
    }


def bring_up(tunnels, owner, spec):
    return tunnels.up(
        owner, spec["target"], spec["remote_host"], spec["remote_port"],
        local_port=spec.get("local_port"), ssh_port=spec.get("ssh_port"),
        options=spec.get("options", ()),
    )  # fmt: skip


def connect(args, session_id, cwd, *, tunnels=None, base=None):
    base = Path(base) if base else home()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = session_dir(session_id, base)
    if directory.exists():
        raise AdapterError("This session is already connected. Run /workspace:disconnect first.")
    if args.import_config:
        return import_config(args.import_config, session_id, cwd, directory)
    if not args.credentials:
        raise AdapterError(
            "Choose a connection credential: pass its path, or run `ws.py discover` to list "
            "the files in ~/Downloads."
        )
    source = read_source(args.credentials)
    spec = tunnel_spec(args, defaults())
    tunnels = tunnels or (Tunnels(base) if spec else None)
    url = args.url or source.get("url")
    owner = session_hash(session_id)
    port = None
    started = False
    if spec:
        port = bring_up(tunnels, owner, spec)
        started = True
        tunnel_url = f"http://127.0.0.1:{port}"
        if args.url and args.url.rstrip("/") != tunnel_url:
            tunnels.release(owner, port)
            raise AdapterError(f"With a tunnel the URL must be {tunnel_url}.")
        url = tunnel_url
    if not url:
        raise AdapterError("Supply --url, or use a connection file that contains the URL.")
    staging = None
    try:
        staging = Path(tempfile.mkdtemp(prefix="stage-", dir=base))
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged = staging / "credentials.json"
        write_private(staged, source, exclusive=True)
        result = prepare(
            staged, directory, url=url, project_id=args.project, label=args.label,
            cwd=cwd, script=SCRIPT, python=sys.executable,
            replay_backlog=args.replay_backlog,
        )  # fmt: skip
        (directory / "settings.json").unlink(missing_ok=True)  # unused by the plugin
        workspace, _ = load(session_id, base=base)
        with workspace.locked():
            workspace.save(session_id=session_id, last_poll=time.time())
            workspace.claim()
        meta = {"tunnel": {**spec, "local_port": port} if spec else None}
        write_private(directory / "session.json", meta)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        if started:
            tunnels.release(owner, port)
        raise
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "connected": True,
        "project": result["project_id"],
        "connection": result["connection_id"],
        "url": url,
        "tunnel_port": port,
    }


def import_config(config_path, session_id, cwd, directory):
    """Adopt an existing adapter bundle in place, only if bound to this exact session."""
    path = Path(config_path).expanduser().resolve(strict=True)
    config = read_private(path)
    state = read_private(config["checkpoint"])
    credentials = read_private(config["credentials"])
    if (
        state.get("identity") != [config["connection_id"], config["project_id"]]
        or credentials["connection"]["id"] != config["connection_id"]
    ):
        raise AdapterError("Adapter files do not match each other.")
    if state.get("session_id") != session_id:
        raise AdapterError(
            "That adapter belongs to a different (or not yet claimed) session; "
            "import only works for the session it is bound to."
        )
    if not Path(cwd).resolve().is_relative_to(config["cwd"]):
        raise AdapterError("This session's folder is outside the adapter's working folder.")
    directory.mkdir(mode=0o700, parents=True)
    try:
        write_private(directory / "session.json", {"link": str(path), "tunnel": None})
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return {"connected": True, "imported": True, "project": config["project_id"]}


# ---- everyday commands -----------------------------------------------------------------


def status(session_id, *, check=False, transport=None, tunnels=None, base=None):
    directory = session_dir(session_id, base)
    if not directory.is_dir():
        return {"connected": False}
    workspace, directory = load(session_id, transport=transport, base=base)
    with workspace.locked():
        state = workspace.state
        out = {
            "connected": True,
            "project": workspace.config["project_name"],
            "agent": workspace.config["handle"],
            "url": workspace.config["url"],
            "session_bound": state["session_id"] == session_id,
            "lease_ok_last_check": state["connected"],
            "ended": state["ended"],
            "error": state["error"],
            "pending": bool(state["pending"]),
        }
    spec = meta_of(directory).get("tunnel")
    if spec:
        found = (tunnels or Tunnels(base or home())).status(spec["local_port"])
        out["tunnel"] = {
            "local_port": spec["local_port"],
            "alive": bool(found and found["alive"]),
            "listening": bool(found and found["listening"]),
        }
    if check:
        try:
            workspace.api().request("GET", "me")
            out["server_reachable"] = True
        except (ApiError, AdapterError, TimeoutError):
            out["server_reachable"] = False
    return out


def inbox(session_id, cwd, *, transport=None, base=None):
    """Check now (ignoring the hook throttle), then show the pending mention if any."""
    workspace, _ = load(session_id, transport=transport, base=base)
    workspace.config["throttle"] = 0
    workspace.hook(
        {"session_id": session_id, "cwd": str(cwd), "hook_event_name": "UserPromptSubmit"}
    )
    try:
        return {"pending": workspace.command("pending", session=session_id, cwd=cwd)}
    except AdapterError as exc:
        if "no pending" in str(exc):
            with workspace.locked():
                error = workspace.state["error"]
            return {"pending": None, **({"error": error, "note": "check failed"} if error else {})}
        raise


def act(action, session_id, cwd, *, text=None, mentions=(), transport=None, base=None):
    workspace, _ = load(session_id, transport=transport, base=base)
    if text is not None:
        token = read_private(workspace.config["credentials"]).get("token", "")
        if token and token in text:
            raise AdapterError("Reply text contains the connection credential; refusing to post.")
    return workspace.command(action, session=session_id, cwd=cwd, text=text, mentions=mentions)


def disconnect(session_id, *, transport=None, tunnels=None, base=None):
    base = base or home()
    directory = session_dir(session_id, base)
    if not directory.is_dir():
        return {"connected": False}
    try:
        meta = meta_of(directory)
    except ValueError:
        meta = {}
    released = False
    try:
        workspace, _ = load(session_id, transport=transport, base=base)
        with workspace.locked():
            if workspace.state["session_id"] == session_id:
                try:
                    workspace.api().request(
                        "POST",
                        f"agent-connections/{workspace.config['connection_id']}/release",
                        data={"session_id": session_id},
                    )
                    released = True
                except (ApiError, AdapterError, TimeoutError):
                    pass  # lease expires on its own in five minutes
                workspace.save(connected=False, ended=True)
    except AdapterError:
        pass
    outcome = None
    spec = meta.get("tunnel")
    if spec:
        outcome = (tunnels or Tunnels(base)).release(session_hash(session_id), spec["local_port"])
    shutil.rmtree(directory)  # only this session's private copy; imported originals stay
    return {"connected": False, "lease_released": released, "tunnel": outcome}


def tunnel_command(action, session_id, *, tunnels=None, base=None):
    base = base or home()
    tunnels = tunnels or Tunnels(base)
    if action == "list":
        return {"tunnels": [{k: v for k, v in t.items() if k != "argv"} for t in tunnels.list()]}
    directory = session_dir(session_id, base)
    spec = meta_of(directory).get("tunnel") if directory.is_dir() else None
    if not spec:
        raise AdapterError("This session has no tunnel from this integration.")
    owner = session_hash(session_id)
    if action == "up":
        return {"tunnel_port": bring_up(tunnels, owner, spec)}
    return {"tunnel": tunnels.release(owner, spec["local_port"])}


# ---- command line ----------------------------------------------------------------------


def parser():
    p = argparse.ArgumentParser(prog="ws.py", description=__doc__)
    p.add_argument("--session", help="session id substituted by Claude (cross-checked)")
    p.add_argument("--config", help=argparse.SUPPRESS)  # accepted for adapter compatibility
    sub = p.add_subparsers(dest="action", required=True)
    c = sub.add_parser("connect")
    c.add_argument("credentials", nargs="?", help="downloaded scoped connection file")
    c.add_argument("--import", dest="import_config", metavar="CONFIG")
    c.add_argument("--url")
    c.add_argument("--project")
    c.add_argument("--label")
    c.add_argument("--replay-backlog", action="store_true")
    c.add_argument("--tunnel", action="store_true", help="open an SSH tunnel with defaults")
    c.add_argument("--ssh", metavar="ALIAS_OR_USER@HOST")
    c.add_argument("--ssh-user")
    c.add_argument("--ssh-host")
    c.add_argument("--ssh-port", type=int)
    c.add_argument("--local-port", type=int)
    c.add_argument("--remote-host")
    c.add_argument("--remote-port", type=int)
    c.add_argument("--ssh-option", action="append", metavar="KEY=VALUE")
    d = sub.add_parser("discover")
    d.add_argument("--dir")
    s = sub.add_parser("status")
    s.add_argument("--check", action="store_true", help="also ask the server (GET /v1/me)")
    for name in ("inbox", "pending", "ack", "reply", "disconnect"):
        x = sub.add_parser(name)
        x.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if name == "reply":
            x.add_argument("--text-file")
            x.add_argument("--mention", action="append", default=[])
    t = sub.add_parser("tunnel")
    t.add_argument("what", choices=("up", "stop", "list"))
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cwd = Path.cwd()
        if args.action == "discover":
            result = {"candidates": discover(args.dir)}
        elif args.action == "tunnel" and args.what == "list":
            result = tunnel_command("list", None)
        else:
            sid = require_session(args.session)
            if args.action == "connect":
                result = connect(args, sid, str(cwd))
            elif args.action == "status":
                result = status(sid, check=args.check)
            elif args.action == "inbox":
                result = inbox(sid, cwd)
            elif args.action == "disconnect":
                result = disconnect(sid)
            elif args.action == "tunnel":
                result = tunnel_command(args.what, sid)
            else:
                text = None
                if args.action == "reply":
                    if args.text_file:
                        reply_path = Path(args.text_file).expanduser().resolve()
                        if reply_path.is_relative_to(home().resolve()):
                            raise AdapterError(
                                "Reply text must not come from the plugin's private store."
                            )
                        with reply_path.open(encoding="utf-8") as stream:
                            text = stream.read(20001)
                    else:
                        text = sys.stdin.read(20001)
                result = act(
                    args.action, sid, cwd, text=text, mentions=getattr(args, "mention", ())
                )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        if isinstance(exc, (AdapterError, TunnelError)):
            explanation = str(exc)
        elif isinstance(exc, ApiError):
            explanation = (
                f"Workspace request failed (HTTP {exc.status}); check access/lease/network."
            )
        else:
            explanation = "Operation failed; check private files, network, and configuration."
        print(explanation, file=sys.stderr)
        return 1
