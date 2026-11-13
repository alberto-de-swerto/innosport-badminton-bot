"""Read-only portfolio demo. Never connects to Telegram or the university website."""

from datetime import datetime, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile

from registry_database import RegistryDatabase, REVISION
from sportsite import Config, ROOT, SportError, write_json
from violation_registry import ViolationRegistry


def prepare_demo():
    if os.environ.get("INNOSPORT_DEMO") != "1":
        raise SportError("The demo requires INNOSPORT_DEMO=1 and a separate demo database.")
    root = Path(tempfile.mkdtemp(prefix="innosport-demo-"))
    raw = json.loads((ROOT / "config.example.json").read_text())
    raw.update(registry_backend="postgresql", registry_dataset="demo", group_ids=[17])
    write_json(root / "config.json", raw)
    config = Config.load(root / "config.json")
    database = RegistryDatabase(config)
    database.upgrade()
    state = {"schema_version": 1, "sessions": {}, "dismissed": [], "confirmed_cases": {},
             "undo_history": [], "undo_results": {}}
    people = [{"student_id": 1001, "full_name": "Example Player One"},
              {"student_id": 1002, "full_name": "Example Player Two"}]
    start = datetime.fromisoformat("2026-11-05T20:00:00+03:00")
    for ident, offset, cancelled in ((10001, 0, people), (10002, 2, people[:1])):
        session = {"group_id": 17, "training_id": ident,
                   "training_start": (start + timedelta(days=offset)).isoformat(),
                   "checked_at": (start + timedelta(days=offset, hours=3)).isoformat(), "cancelled": cancelled}
        state["sessions"]["17:" + str(ident)] = session
    if database.import_legacy(state, b"fictional-demo-v1", dataset="demo"):
        registry = ViolationRegistry(config, actor="demo:club-leader", reason="Demonstration of forgiveness and undo")
        row = registry.rows()[0]
        registry.decrement(row["student_id"], row["revision"])
        registry.undo(request_id="demo-seed-undo")
    return config


def handler_for(config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request paths and headers may contain untrusted values. Keep logs minimal.
            pass

        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health/live":
                return self.respond(200, {"status": "alive", "mode": "demo"})
            try:
                registry = ViolationRegistry(config)
                # An accidentally supplied live database must never expose personal data.
                with registry.database.transaction(dataset="demo"):
                    pass
                if path == "/health/ready":
                    return self.respond(200, {"status": "ready", "mode": "demo", "schema": REVISION,
                                              "revision": os.environ.get("APP_REVISION", "development")})
                if path == "/api/violations":
                    return self.respond(200, {"mode": "demo", "participants": registry.rows()})
                if path == "/api/audit":
                    return self.respond(200, {"mode": "demo", "events": registry.audit(20)})
                if path == "/":
                    rows = registry.rows()
                    table = "".join("<tr><td>%s</td><td>%s</td></tr>" % (escape(p["full_name"]), p["count"]) for p in rows)
                    events = "".join("<li><strong>%s</strong> · %s<br>%s</li>" % (
                        escape(e["kind"]), escape(e["actor"]), escape(e["reason"])) for e in registry.audit(10))
                    html = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>InnoSport · Demo</title>
<style>body{font:17px system-ui;max-width:850px;margin:55px auto;padding:0 24px;background:#f4f7f6;color:#17342c}
main{background:white;padding:30px;border-radius:18px}h1{font-size:30px}p{line-height:1.6}.tag{color:#29705b}
table{border-collapse:collapse;width:100%%}td,th{text-align:left;padding:14px;border-bottom:1px solid #e1e9e5}
li{margin:18px 0;line-height:1.6}code{font-size:13px;overflow-wrap:anywhere}a{color:#17654e}</style>
<main><p class="tag">PORTFOLIO DEMO · FICTIONAL DATA ONLY</p><h1>🏸 InnoSport Badminton</h1>
<p>A PostgreSQL-backed cancellation register with transactional audit history, forgiveness and undo.
This read-only demo uses no university account and sends no Telegram messages.</p>
<h2>Cancellation counters</h2><table><tr><th>Participant</th><th>Cases</th></tr>%s</table>
<h2>Recent actions</h2><ul>%s</ul><p>API: <a href="/api/violations">counters</a> · <a href="/api/audit">audit</a>
 · <a href="/health/ready">readiness</a></p><p>Release: <code>%s</code></p></main></html>""" % (
                        table, events, escape(os.environ.get("APP_REVISION", "development")))
                    return self.respond(200, html, "text/html; charset=utf-8")
                self.respond(404, {"error": "not_found"})
            except SportError:
                self.respond(503, {"error": "database_unavailable_or_wrong_dataset"})

    return Handler


def main():
    config = prepare_demo()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), handler_for(config))
    print("Fictional read-only demo listening on port 8080", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except SportError as exc:
        print(str(exc), flush=True)
        raise SystemExit(1)
