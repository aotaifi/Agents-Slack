#!/usr/bin/env python3
"""Throwaway loopback stand-in for the workspace API (me/claim/release/inbox) for smoke tests."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

CONN = {
    "id": "smoke-connection",
    "actor": {"id": "smoke-agent", "kind": "agent", "handle": "smoke.agent"},
    "project": {"id": "smoke-project", "name": "Smoke project"},
    "label": "smoke", "bound": False, "active": False, "revoked": False,
}  # fmt: skip


class Handler(BaseHTTPRequestHandler):
    def reply(self, body, code=200):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        print(self.command, self.path, file=sys.stderr, flush=True)

    def do_GET(self):
        if self.path == "/v1/me":
            return self.reply({**CONN["actor"], "connection": CONN})
        if "/inbox" in self.path:
            return self.reply({"items": [], "next_cursor": None, "cursor": 0})
        self.reply({}, 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        bound = {**CONN, "bound": True, "active": self.path.endswith("/claim")}
        self.reply({"connection": bound})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
