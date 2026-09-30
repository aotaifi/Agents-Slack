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
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
    args = parser.parse_args()
    bootstrap = json.loads(Path(args.admin_credentials).read_text())
    py = Client(args.url, bootstrap["token"])
    # Set up project and two distinct owned agents with Python's urllib client.
    project = py.request("POST", "projects", data={"name": "Two client demo"})
    agent_a = py.request("POST", "actors", data={"name": "urllib agent", "kind": "agent"})
    agent_b = py.request("POST", "actors", data={"name": "curl agent", "kind": "agent"})
    for agent in (agent_a, agent_b):
        py.request(
            "POST", f"projects/{project['id']}/members", data={"actor_id": agent["actor"]["id"]}
        )
    channel = py.request("POST", f"projects/{project['id']}/channels", data={"name": "demo"})
    thread = py.request(
        "POST", f"channels/{channel['id']}/threads", data={"title": "Protocol check"}
    )
    local = Path(__file__).resolve().parents[1] / ".local"
    write_secret(local / "demo-urllib-agent.json", agent_a)
    write_secret(local / "demo-curl-agent.json", agent_b)
    a = Client(args.url, agent_a["token"])
    b = Curl(args.url, agent_b["token"])
    key = "two-client-demo-first-message"
    first = a.post_message(thread["id"], "Hello from urllib", idempotency_key=key)
    retry = a.post_message(thread["id"], "Hello from urllib", idempotency_key=key)
    if first["id"] != retry["id"]:
        raise RuntimeError("idempotent retry created a different message")
    reply = b.request("POST", f"threads/{thread['id']}/messages", {"text": "Reply from curl"})
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
                "transcript": transcript,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
