import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
from agent_commons_client import ApiError, Client  # noqa: E402


@pytest.fixture
def server():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"items":[],"next_cursor":12,"cursor":18}')

        def do_POST(self):
            seen["key"] = self.headers.get("Idempotency-Key")
            seen["payload"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"key conflicts with existing body"}')

        def log_message(self, *_):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield Client(f"http://127.0.0.1:{httpd.server_port}", "example-token"), seen
    httpd.shutdown()
    thread.join()


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
