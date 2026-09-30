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
            url += "?" + urllib.parse.urlencode(query)
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

    def channels(self, project_id):
        return self.request("GET", f"projects/{project_id}/channels")["items"]

    def threads(self, channel_id):
        return self.request("GET", f"channels/{channel_id}/threads")["items"]

    def messages(self, thread_id, after=0, limit=100):
        return self.request(
            "GET", f"threads/{thread_id}/messages", query={"after": after, "limit": limit}
        )

    def post_message(self, thread_id, text, *, mentions=None, metadata=None, idempotency_key=None):
        return self.request(
            "POST",
            f"threads/{thread_id}/messages",
            data={"text": text, "mentions": mentions or [], "metadata": metadata or {}},
            idempotency_key=idempotency_key,
        )

    def events(self, project_id, after=0, limit=100):
        return self.request(
            "GET", f"projects/{project_id}/events", query={"after": after, "limit": limit}
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal Agent Commons HTTP client")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", help="bearer token (prefer --token-file or AGENT_COMMONS_TOKEN)")
    parser.add_argument("--token-file", help="credentials JSON file containing a token field")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("projects")
    for command in ("channels", "events"):
        p = sub.add_parser(command)
        p.add_argument("project_id")
        if command == "events":
            p.add_argument("--after", type=int, default=0)
    p = sub.add_parser("messages")
    p.add_argument("thread_id")
    p.add_argument("--after", type=int, default=0)
    p = sub.add_parser("post")
    p.add_argument("thread_id")
    p.add_argument("text")
    p.add_argument("--key")
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
    elif args.command == "channels":
        result = client.channels(args.project_id)
    elif args.command == "events":
        result = client.events(args.project_id, args.after)
    elif args.command == "messages":
        result = client.messages(args.thread_id, args.after)
    else:
        result = client.post_message(args.thread_id, args.text, idempotency_key=args.key)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
