"""One loopback HTTP stub server for every test that needs a fake workspace API."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer


class JsonHandler(BaseHTTPRequestHandler):
    """Base handler: JSON replies, JSON request bodies, and no access log noise."""

    def send_json(self, body, code=200, content_type=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def log_message(self, *_):
        pass


@contextmanager
def serve(handler):
    """Serve `handler` on an ephemeral loopback port; yield its base URL."""
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    # A short poll interval makes shutdown() return at once instead of after 0.5 s.
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
