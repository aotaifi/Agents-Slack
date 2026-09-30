"""Exercise the HTTP contract using urllib and curl as separate implementations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
from agent_commons_client import Client  # noqa: E402


class Curl:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token

    def request(self, method, path, data=None, key=None, query=""):
        cmd = [
            "curl",
            "-fsS",
            "--connect-timeout",
            "5",
            "--max-time",
            "20",
            "-X",
            method,
            "--config",
            "-",
        ]
        config = [
            'header = "Authorization: Bearer ' + self.token + '"',
            'header = "Accept: application/json"',
        ]
        if data is not None:
            config.append('header = "Content-Type: application/json"')
            cmd += ["--data-binary", json.dumps(data)]
        if key:
            config.append(f'header = "Idempotency-Key: {key}"')
        result = subprocess.run(
            cmd + [self.base + "/v1/" + path.lstrip("/") + query],
            check=True,
            capture_output=True,
            text=True,
            input="\n".join(config) + "\n",
        )
        return json.loads(result.stdout) if result.stdout else None


def write_secret(path: Path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream)
    os.chmod(path, 0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--admin-credentials",
        required=True,
        help="bootstrap JSON containing actor and token",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--credentials-dir", help="private output directory; never overwrite files")
    args = parser.parse_args()
    bootstrap = json.loads(Path(args.admin_credentials).read_text())
    py = Client(args.url, bootstrap["token"])
    # Set up project and two distinct owned agents with Python's urllib client.
    project = py.request("POST", "projects", data={"name": "Two client demo"})
    py.request("PUT", f"projects/{project['id']}/rules", data={"text": "Be helpful. " * 120})
    agent_a = py.request("POST", "actors", data={"name": "urllib agent", "kind": "agent"})
    agent_b = py.request("POST", "actors", data={"name": "curl agent", "kind": "agent"})
    for agent in (agent_a, agent_b):
        if (
            not agent["actor"]["handle"]
            or agent["actor"]["owner"]["id"] != bootstrap["actor"]["id"]
        ):
            raise RuntimeError("owned agents must expose a stable handle and human owner")
        py.request(
            "POST", f"projects/{project['id']}/members", data={"actor_id": agent["actor"]["id"]}
        )
    channel = py.request("POST", f"projects/{project['id']}/channels", data={"name": "demo"})
    thread = py.request(
        "POST", f"channels/{channel['id']}/threads", data={"title": "Protocol check"}
    )
    local = (
        Path(args.credentials_dir) if args.credentials_dir
        else Path(__file__).resolve().parents[1] / ".local" / f"demo-{project['id']}"
    )
    write_secret(local / "urllib-agent.json", agent_a)
    write_secret(local / "curl-agent.json", agent_b)
    a = Client(args.url, agent_a["token"])
    b = Curl(args.url, agent_b["token"])
    key = "two-client-demo-first-message"
    first = a.post_message(
        thread["id"], "Hello from urllib", mentions=[agent_b["actor"]["id"]], idempotency_key=key
    )
    retry = a.post_message(
        thread["id"], "Hello from urllib", mentions=[agent_b["actor"]["id"]], idempotency_key=key
    )
    if first["id"] != retry["id"]:
        raise RuntimeError("idempotent retry created a different message")
    reply = b.request(
        "POST", f"threads/{thread['id']}/messages",
        {"text": "Reply from curl", "reply_to": first["id"]},
    )
    if reply["reply_to"] != first["id"]:
        raise RuntimeError("curl reply lost its explicit parent")
    reactions_path = f"messages/{first['id']}/reactions"
    reacted = b.request("PUT", reactions_path, {"emoji": "👍"})
    reacted_again = b.request("PUT", reactions_path, {"emoji": "👍"})
    if reacted["reactions"] != reacted_again["reactions"] or reacted["reactions"][0]["count"] != 1:
        raise RuntimeError("repeated reaction must remain a single actor reaction")
    removed = b.request("DELETE", reactions_path, {"emoji": "👍"})
    removed_again = b.request("DELETE", reactions_path, {"emoji": "👍"})
    if removed["reactions"] or removed_again["reactions"]:
        raise RuntimeError("reaction deletion and repeated deletion must clear the reaction")
    inbox = b.request("GET", f"projects/{project['id']}/inbox", query="?after=0&limit=1")
    inbox_items = list(inbox["items"])
    while inbox["next_cursor"] is not None:
        inbox = b.request(
            "GET", f"projects/{project['id']}/inbox",
            query=f"?after={inbox['next_cursor']}&limit=1",
        )
        inbox_items.extend(inbox["items"])
    if [e["payload"]["id"] for e in inbox_items] != [first["id"]]:
        raise RuntimeError("inbox must include only explicit mention, excluding self and reactions")
    if a.inbox(project["id"])["items"]:
        raise RuntimeError("unmentioned reply must not reach an unfollowing agent")
    followed = a.inbox(project["id"], followed_thread_ids=[thread["id"]])["items"]
    if [e["payload"]["id"] for e in followed] != [reply["id"]]:
        raise RuntimeError("explicitly followed thread must include the other actor's reply")
    context = a.context(thread["id"], trigger_message_id=reply["id"], limit=1, max_chars=1000)
    text_entries = [context["rules"], *context["messages"],
                    context["trigger_message"], context["parent_message"]]
    if (
        [m["id"] for m in context["messages"]] != [reply["id"]]
        or context["parent_message"]["id"] != first["id"]
        or not context["has_older"]
        or not context["rules"].get("truncated")
        or sum(len(entry["text"]) for entry in text_entries) > 1000
    ):
        raise RuntimeError("context must give latest bounded messages, trigger, parent and rules")
    page = b.request("GET", f"projects/{project['id']}/events", query="?after=0&limit=2")
    replayed_events = list(page["items"])
    while page.get("next_cursor") is not None:
        page = b.request(
            "GET",
            f"projects/{project['id']}/events",
            query=f"?after={page['next_cursor']}&limit=2",
        )
        replayed_events.extend(page["items"])
    messages = a.messages(thread["id"])["items"]
    expected_texts = ["Hello from urllib", "Reply from curl"]
    transcript = [{"author": x["author"]["name"], "text": x["text"]} for x in messages]
    if [entry["text"] for entry in transcript] != expected_texts:
        raise RuntimeError("expected exactly the urllib post and curl reply in the transcript")
    event_ids = [event["id"] for event in replayed_events]
    if event_ids != sorted(set(event_ids)):
        raise RuntimeError("event replay IDs were duplicated or out of order")
    reaction_events = [e for e in replayed_events if e["type"].startswith("reaction.")]
    if len(reaction_events) != 2:
        raise RuntimeError("noop repeated reactions must not produce additional events")
    print(
        json.dumps(
            {
                "project_id": project["id"],
                "channel_id": channel["id"],
                "thread_id": thread["id"],
                "first_message_id": first["id"],
                "retry_message_id": retry["id"],
                "reply_message_id": reply["id"],
                "retry_status": "same message" if first["id"] == retry["id"] else "error",
                "event_count": len(replayed_events),
                "event_ids_ordered_unique": True,
                "explicit_reply_verified": True,
                "reaction_noops_verified": True,
                "mention_and_follow_inbox_verified": True,
                "bounded_latest_context_verified": True,
                "transcript": transcript,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
