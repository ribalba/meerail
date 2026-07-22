"""A stand-in Meerato, just enough to photograph the Add Task dialog.

The dialog's buckets and statuses come from Meerato itself: the server proxies
`GET /api/create?token=…` and the browser renders whatever comes back (see
app/meerato.py and app/static/js/app.tasks.js). So the screenshot needs a
Meerato to answer — but not a real one, and certainly not the user's.

Reachability is the fiddly part. The meerail server runs in a container, so
`localhost` there is the container, not this process. Binding to 0.0.0.0 and
addressing the compose network's gateway is what bridges the two; `gateway()`
asks Docker for it rather than assuming the usual 172.17.0.1.

Nothing here is used outside the screenshot harness.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# What the dialog renders. Invented, but shaped like Meerato's real payload —
# `{statuses, buckets, default_bucket_id}` — and worded so the screenshot reads
# as somebody's actual task board.
OPTIONS = {
    "default_bucket_id": "b-admin",
    "buckets": [
        {"id": "b-admin", "name": "Admin & paperwork"},
        {"id": "b-house", "name": "House"},
        {"id": "b-work", "name": "Northwind"},
        {"id": "b-someday", "name": "Someday"},
    ],
    "statuses": [
        {"value": "on_list", "label": "On the list"},
        {"value": "backlog", "label": "Backlog"},
        {"value": "waiting", "label": "Waiting on someone"},
    ],
}


class _Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path.startswith("/api/create"):
            return self._json(OPTIONS)
        self._json({"detail": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        # Present so a stray create attempt gets a sane answer; the screenshot
        # stops at the open dialog and never files anything.
        if self.path.startswith("/api/create"):
            return self._json({"id": 1, "public_token": "stub", "title": "stub"})
        self._json({"detail": "not found"}, 404)

    def log_message(self, *_args):
        pass  # quiet: the harness prints its own progress


def gateway(project: str = "meerail-test") -> str:
    """The address the server container can reach this host on."""
    out = subprocess.run(
        ["docker", "network", "inspect", f"{project}_default",
         "-f", "{{range .IPAM.Config}}{{.Gateway}}{{end}}"],
        capture_output=True, text=True, timeout=15,
    )
    addr = out.stdout.strip()
    if not addr:
        raise RuntimeError(
            f"could not find the gateway for the {project}_default network — "
            "is the test stack up? (make test-up)"
        )
    return addr


def serve() -> tuple[HTTPServer, int]:
    """Start the stub on a free port in a daemon thread. Returns (server, port)."""
    httpd = HTTPServer(("0.0.0.0", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]
