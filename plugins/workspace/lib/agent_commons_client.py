"""Small transport-only client for the Agent Commons HTTP API."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, message: str, body: Any = None, headers: Any = None):
        self.status, self.message, self.body, self.headers = status, message, body, headers
        super().__init__(f"HTTP {status}: {message}")


class Client:
    def __init__(self, base_url: str, token: str, timeout: float = 20):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        url = self.base_url + "/v1/" + urllib.parse.quote(path.lstrip("/"), safe="/")
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else None
                detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed, detail = raw.decode(errors="replace"), raw.decode(errors="replace")
            raise ApiError(exc.code, str(detail or exc.reason), parsed, exc.headers) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, str(exc.reason)) from None

    def projects(self):
        return self.request("GET", "projects")["items"]

    def actors(self):
        """List visible actors, including stable handles and accountable owners."""
        return self.request("GET", "actors")["items"]

    def members(self, project_id):
        return self.request("GET", f"projects/{project_id}/members")["items"]

    def channels(self, project_id):
        return self.request("GET", f"projects/{project_id}/channels")["items"]

    def threads(self, channel_id):
        return self.request("GET", f"channels/{channel_id}/threads")["items"]

    def messages(self, thread_id, after=0, limit=100):
        return self.request(
            "GET", f"threads/{thread_id}/messages", query={"after": after, "limit": limit}
        )

    def post_message(
        self, thread_id, text, *, mentions=None, metadata=None, reply_to=None, idempotency_key=None
    ):
        payload = {"text": text, "mentions": mentions or [], "metadata": metadata or {}}
        if reply_to is not None:
            payload["reply_to"] = reply_to
        return self.request(
            "POST",
            f"threads/{thread_id}/messages",
            data=payload,
            idempotency_key=idempotency_key,
        )

    def events(self, project_id, after=0, limit=100):
        return self.request(
            "GET", f"projects/{project_id}/events", query={"after": after, "limit": limit}
        )

    def inbox(self, project_id, after=0, limit=50, *, followed_thread_ids=()):
        return self.request(
            "GET",
            f"projects/{project_id}/inbox",
            query={"after": after, "limit": limit, "followed_thread_ids": followed_thread_ids},
        )

    def context(self, thread_id, *, trigger_message_id=None, limit=20, max_chars=12000):
        query = {"limit": limit, "max_chars": max_chars}
        if trigger_message_id is not None:
            query["trigger_message_id"] = trigger_message_id
        return self.request("GET", f"threads/{thread_id}/context", query=query)

    def search(self, project_id, q, limit=20, before=None):
        query = {"q": q, "limit": limit}
        if before is not None:
            query["before"] = before
        return self.request("GET", f"projects/{project_id}/search", query=query)

    def add_reaction(self, message_id, emoji):
        return self.request("PUT", f"messages/{message_id}/reactions", data={"emoji": emoji})

    def remove_reaction(self, message_id, emoji):
        return self.request("DELETE", f"messages/{message_id}/reactions", data={"emoji": emoji})


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal Agent Commons HTTP client")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", help="bearer token (prefer --token-file or AGENT_COMMONS_TOKEN)")
    parser.add_argument("--token-file", help="credentials JSON file containing a token field")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("projects")
    sub.add_parser("actors")
    for command in ("channels", "events", "inbox"):
        p = sub.add_parser(command)
        p.add_argument("project_id")
        if command in ("events", "inbox"):
            p.add_argument("--after", type=int, default=0)
        if command == "inbox":
            p.add_argument("--follow-thread", action="append", default=[])
    p = sub.add_parser("messages")
    p.add_argument("thread_id")
    p.add_argument("--after", type=int, default=0)
    p = sub.add_parser("post")
    p.add_argument("thread_id")
    p.add_argument("text")
    p.add_argument("--key")
    p.add_argument("--reply-to")
    p.add_argument("--mention", action="append", default=[])
    p = sub.add_parser("context")
    p.add_argument("thread_id")
    p.add_argument("--trigger-message-id")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--max-chars", type=int, default=12000)
    p = sub.add_parser("search")
    p.add_argument("project_id")
    p.add_argument("q")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--before", type=int)
    for command in ("react", "unreact"):
        p = sub.add_parser(command)
        p.add_argument("message_id")
        p.add_argument("emoji", choices=["👍", "❓"])
    args = parser.parse_args()
    token = args.token
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as credentials_file:
            token = json.load(credentials_file)["token"]
    token = token or os.environ.get("AGENT_COMMONS_TOKEN")
    if not token:
        parser.error("provide --token-file, --token, or AGENT_COMMONS_TOKEN")
    client = Client(args.url, token)
    if args.command == "projects":
        result = client.projects()
    elif args.command == "actors":
        result = client.actors()
    elif args.command == "channels":
        result = client.channels(args.project_id)
    elif args.command == "events":
        result = client.events(args.project_id, args.after)
    elif args.command == "inbox":
        result = client.inbox(
            args.project_id, args.after, followed_thread_ids=args.follow_thread
        )
    elif args.command == "messages":
        result = client.messages(args.thread_id, args.after)
    elif args.command == "context":
        result = client.context(
            args.thread_id,
            trigger_message_id=args.trigger_message_id,
            limit=args.limit,
            max_chars=args.max_chars,
        )
    elif args.command == "search":
        result = client.search(args.project_id, args.q, args.limit, args.before)
    elif args.command in ("react", "unreact"):
        method = client.add_reaction if args.command == "react" else client.remove_reaction
        result = method(args.message_id, args.emoji)
    else:
        result = client.post_message(
            args.thread_id,
            args.text,
            idempotency_key=args.key,
            reply_to=args.reply_to,
            mentions=args.mention,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
