"""Loomlet UI server — serves the agent-built dashboard + read-only fleet status.

Exposed through a Cloudflare Tunnel. Read-only by construction: the only
endpoints are static files from ui/ and GET /api/status assembled from the
orchestrator's state files. No control surface, no secrets.
"""
import json
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
PORT = 8077
MAX_EVENTS = 300


def status_payload() -> bytes:
    agents = {}
    agents_file = STATE / "agents.json"
    if agents_file.exists():
        agents = json.loads(agents_file.read_text(encoding="utf-8"))
    events = []
    activity = STATE / "activity.jsonl"
    if activity.exists():
        lines = activity.read_text(encoding="utf-8").splitlines()
        for line in lines[-MAX_EVENTS:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    events.reverse()  # newest first
    return json.dumps({"agents": agents, "events": events}, ensure_ascii=False).encode("utf-8")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "ui"), **kwargs)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/status":
            body = status_payload()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # quiet; the orchestrator's activity log is the audit trail


if __name__ == "__main__":
    print(f"loomlet ui server on http://127.0.0.1:{PORT}", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
