"""Static UI server for one fleet project. Read-only by construction.

Usage:
  python server.py <project> <ui_dir> <port> [--status <shared_state_dir>]

If --status is given, GET /api/status returns the fleet's view-only payload
assembled from the shared control-plane state (used by the loomlet dashboard).
Otherwise only static files are served (used by product projects like inkwell).
The only verbs are GET; there is no control surface and no secrets in any payload.
"""
import json
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

MAX_EVENTS = 400

PROJECT = sys.argv[1]
UI_DIR = str(Path(sys.argv[2]).resolve())
PORT = int(sys.argv[3])
STATE_DIR = None
if "--status" in sys.argv:
    STATE_DIR = Path(sys.argv[sys.argv.index("--status") + 1]).resolve()


def _read_json(path: Path, default):
    """BOM-tolerant, error-tolerant state read — a bad file must never 502 the site."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def status_payload() -> bytes:
    agents_all = {}
    meta_all = {}
    if STATE_DIR:
        agents_all = _read_json(STATE_DIR / "agents.json", {})
        meta_all = _read_json(STATE_DIR / "projects_meta.json", {})
    events = []
    if STATE_DIR:
        act = STATE_DIR / "activity.jsonl"
        if act.exists():
            try:
                lines = act.read_text(encoding="utf-8-sig").splitlines()[-MAX_EVENTS:]
            except OSError:
                lines = []
            for line in lines:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    events.reverse()  # newest first

    projects = {}
    for key, info in agents_all.items():
        proj, _, agent = key.partition(":")
        projects.setdefault(proj, {"agents": {}, "events": []})
        projects[proj]["agents"][agent] = info
    for ev in events:
        proj = ev.get("project", "fleet")
        projects.setdefault(proj, {"agents": {}, "events": []})
        projects[proj]["events"].append(ev)
    for proj, meta in meta_all.items():
        projects.setdefault(proj, {"agents": {}, "events": []}).update(
            {k: meta.get(k) for k in ("goal", "done", "total", "complete", "repo")})

    payload = {"projects": projects, "agents": agents_all, "events": events}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    def do_GET(self):
        if STATE_DIR and self.path.split("?")[0] == "/api/status":
            try:
                body = status_payload()
            except Exception:
                body = b'{"projects": {}, "agents": {}, "events": []}'
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
        pass


if __name__ == "__main__":
    print(f"[{PROJECT}] ui server on http://127.0.0.1:{PORT} "
          f"(status={'on' if STATE_DIR else 'off'})", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
