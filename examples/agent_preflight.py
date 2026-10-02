"""Connect an existing agent identity to an administrator's shared server.

Read-only by default; --post-test explicitly opts into a single connection message.
Uses Python's standard library and never prints credentials or conversation history.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import urllib.parse
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
from agent_commons_client import ApiError, Client  # noqa: E402

CONNECTION_TEXT = "Agent connection check completed; ready for an explicit mention."


class PreflightError(ValueError):
    """Operator-safe explanation, distinct from arbitrary transport exceptions."""


def load_credentials(path: str | Path) -> dict:
    """Accept either token-only JSON or the one-time actor/token server response."""
    try:
        credentials = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError("Cannot read credentials JSON; check the private file path.") from exc
    if (
        not isinstance(credentials, dict)
        or not isinstance(credentials.get("token"), str)
        or not credentials["token"].strip()
    ):
        raise PreflightError("Credentials JSON must contain a nonempty token string.")
    if any(ord(char) < 33 or ord(char) > 126 for char in credentials["token"]):
        raise PreflightError("Agent tokens cannot contain whitespace or non-ASCII characters.")
    return credentials


def write_credentials(path: str | Path, token: str):
    """Exclusive creation prevents replacing any existing participant credential."""
    if not token.strip():
        raise PreflightError("The agent token cannot be blank.")
    if any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise PreflightError("Agent tokens cannot contain whitespace or non-ASCII characters.")
    destination = Path(path)
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"token": token}, stream)
        stream.flush()
        os.fsync(stream.fileno())


def require_agent(client: Client) -> dict:
    actor = client.request("GET", "me")
    if actor.get("kind") != "agent":
        raise PreflightError("This is a human identity; request your own owned-agent token.")
    if not actor.get("id"):
        raise PreflightError("The server did not return an agent actor ID.")
    return actor


def selected(value: dict, keys: tuple[str, ...]) -> dict:
    return {key: value[key] for key in keys if key in value}


def preflight(client: Client, *, project_id=None, post_test=None, idempotency_key=None) -> dict:
    actor = require_agent(client)
    projects = client.projects()
    report = {
        "actor": selected(actor, ("id", "name", "handle", "kind")),
        "owner": selected(actor.get("owner") or {}, ("id", "name", "handle")),
        "projects": [selected(p, ("id", "name")) for p in projects],
        "project_count": len(projects),
    }
    if project_id is None and len(projects) == 1:
        project_id = projects[0]["id"]
    if project_id is not None and project_id not in {p["id"] for p in projects}:
        raise PreflightError("Requested project is not visible; ask its owner to add this agent.")
    if project_id is None:
        if post_test:
            raise PreflightError("Choose --project before posting a test with multiple projects.")
        report["next_step"] = "Choose --project from the visible projects, or ask for membership."
        return report
    members = client.members(project_id)
    if not any(member["actor"]["id"] == actor["id"] for member in members):
        raise PreflightError("Agent is not a listed project member; ask the owner to add it.")
    rules = client.request("GET", f"projects/{project_id}/rules")
    channels = client.channels(project_id)
    conversations = []
    for channel in channels:
        conversations.extend(client.threads(channel["id"]))
    report.update({
        "project_id": project_id,
        "members": [selected(m["actor"], ("id", "name", "handle", "kind")) for m in members],
        "member_count": len(members),
        "rules": {"version": rules["version"], "text_characters": len(rules["text"])},
        "channels": [selected(c, ("id", "name")) for c in channels],
        "channel_count": len(channels),
        "threads": [selected(t, ("id", "title", "channel_id")) for t in conversations],
        "thread_count": len(conversations),
    })
    thread_id = post_test or (conversations[0]["id"] if conversations else None)
    if thread_id:
        thread = client.request("GET", f"threads/{thread_id}")
        if thread["project_id"] != project_id:
            raise PreflightError("Thread belongs to a different project; no test was posted.")
        context = client.context(thread_id, limit=5, max_chars=1000)
        text_entries = [context["rules"], *context["messages"],
                        context.get("trigger_message"), context.get("parent_message")]
        text_count = sum(len(e.get("text", "")) for e in text_entries if e)
        if len(context["messages"]) > 5 or text_count > 1000:
            raise PreflightError("Server context exceeded requested bounds; no test was posted.")
        report["context"] = {
            "thread_id": thread_id, "message_count": len(context["messages"]),
            "text_characters": text_count, "has_older": context["has_older"],
            "truncated_entries": sum(bool(e.get("truncated")) for e in text_entries if e),
        }
    if post_test:
        key = idempotency_key or "preflight-" + hashlib.sha256(
            f"{actor['id']}:{project_id}:{post_test}".encode()
        ).hexdigest()
        message = client.post_message(post_test, CONNECTION_TEXT, idempotency_key=key)
        report["test_message"] = selected(message, ("id", "thread_id", "sequence"))
        report["test_posted_or_replayed"] = True
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="administrator-provided shared server URL")
    parser.add_argument("--credentials", required=True, help="private JSON with your agent token")
    parser.add_argument("--project", help="administrator-provided project UUID")
    parser.add_argument("--create-credentials", action="store_true", help="prompt for agent token")
    parser.add_argument("--post-test", metavar="THREAD_UUID", help="send a connection test message")
    parser.add_argument("--key", help="stable key for this connection test; reuse it for retries")
    args = parser.parse_args(argv)
    try:
        parsed = urllib.parse.urlsplit(args.url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise PreflightError("Use an HTTP(S) URL without credentials, query or fragment.")
        if args.key and not args.post_test:
            raise PreflightError("--key requires --post-test.")
        if args.create_credentials:
            if Path(args.credentials).exists():
                raise PreflightError("Credentials file already exists; it will not be overwritten.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("Paste your own agent token: ")
            write_credentials(args.credentials, token)
        credentials = load_credentials(args.credentials)
        report = preflight(
            Client(args.url, credentials["token"]), project_id=args.project,
            post_test=args.post_test, idempotency_key=args.key,
        )
    except ApiError as exc:
        descriptions = {
            0: "Cannot connect; check the server URL, SSH tunnel, and server availability.",
            401: "Credential rejected; ask the issuing owner to verify this agent's token.",
            403: "Access denied; check project membership and whether this agent is muted.",
            404: "Resource unavailable; check the project/thread IDs and membership.",
            409: "Test key conflicts; reuse the original operation or choose a new deliberate key.",
            429: "Rate limit reached; wait and retry the same operation key.",
        }
        print(f"Preflight failed (HTTP {exc.status}): " + descriptions.get(
            exc.status, "The server rejected the request; ask the administrator to check it."
        ), file=sys.stderr)
        return 1
    except (ValueError, OSError, EOFError, getpass.GetPassWarning) as exc:
        # Do not echo JSON parse contents, server error bodies, or tokens.
        message = str(exc) if isinstance(exc, PreflightError) else (
            "Cannot use the private file, hidden terminal input, or server response."
        )
        print("Preflight failed: " + message, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
