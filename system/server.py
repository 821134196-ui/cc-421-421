"""Stdlib HTTP server: JSON API + static UI + photo files. Zero dependencies."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import service
from .db import connect, init_db
from .sample import bootstrap_samples
from .seed import seed_if_empty

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".json": "application/json; charset=utf-8",
}


class App:
    def __init__(self, db_path: Path, photo_dir: Path):
        self.conn = connect(db_path)
        init_db(self.conn)
        seeded = seed_if_empty(self.conn)
        photo_dir.mkdir(parents=True, exist_ok=True)
        bootstrap_samples(self.conn, photo_dir)
        self.photo_dir = photo_dir
        self._scheduler_stop = threading.Event()

    # -- background: in-process maintenance/overdue reminders -------------
    def start_scheduler(self, interval: float = 30.0) -> None:
        def loop():
            # immediate first tick, then periodic
            while not self._scheduler_stop.wait(interval):
                try:
                    service.tick(self.conn)
                except Exception as exc:  # never let the daemon die
                    print("[scheduler] tick error:", exc)

        threading.Thread(target=loop, name="maint-scheduler", daemon=True).start()


def make_handler(app: App):
    conn = app.conn
    photo_dir = app.photo_dir

    class Handler(BaseHTTPRequestHandler):
        server_version = "FixtureSystem/1.0"

        def log_message(self, fmt, *args):
            pass  # quiet; tests don't care

        # ------------------------------------------------------------ utils
        def _send_json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_err(self, exc: Exception, status: int = 400):
            kind = type(exc).__name__
            code = {
                "PermissionError_": 403,
                "ConflictError": 409,
                "ServiceError": 400,
            }.get(kind, 400)
            self._send_json({"error": str(exc), "kind": kind}, code)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                raise service.ServiceError("请求体须为 JSON")

        def _file(self, path: Path, cache: bool = False):
            if not path.is_file():
                self.send_error(404, "Not found")
                return
            data = path.read_bytes()
            self.send_response(200)
            ctype = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if cache:
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        # ------------------------------------------------------------- GET
        def do_GET(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            try:
                if p == "/api/meta":
                    return self._send_json(self._meta())
                if p == "/api/fixtures":
                    return self._send_json(service.list_fixtures(
                        conn, q.get("status", [None])[0], q.get("q", [None])[0]))
                m = re.fullmatch(r"/api/fixtures/([\w-]+)", p)
                if m:
                    fx = service.get_fixture(conn, m.group(1))
                    fx["serving_stations"] = service.fixture_stations(conn, m.group(1))
                    fx["inspections"] = service.list_inspections(conn, m.group(1))[:5]
                    return self._send_json(fx)
                if p == "/api/requests":
                    return self._send_json(service.list_requests(
                        conn, q.get("status", [None])[0]))
                if p == "/api/repairs":
                    return self._send_json(service.list_repairs(
                        conn, q.get("status", [None])[0]))
                if p == "/api/plans":
                    return self._send_json(service.maintenance_list(conn))
                if p == "/api/transfers":
                    return self._send_json(service.list_transfers(conn))
                if p == "/api/disposals":
                    return self._send_json(service.disposal_list(
                        conn, q.get("status", [None])[0]))
                if p == "/api/scrap":
                    return self._send_json(service.scrap_list(conn))
                if p == "/api/match":
                    if "station" not in q or "model" not in q:
                        raise service.ServiceError("需要 station 与 model 参数")
                    return self._send_json(service.match_station(
                        conn, q["station"][0], q["model"][0]))
                if p == "/api/reminders":
                    return self._send_json(service.list_reminders(conn))
                if p == "/api/inspections":
                    return self._send_json(service.list_inspections(
                        conn, q.get("fixture", [None])[0]))
                if p.startswith("/photos/"):
                    name = Path(p[len("/photos/"):]).name
                    return self._file(photo_dir / name)
                # static / index
                if p == "/":
                    return self._file(STATIC_DIR / "index.html")
                if p.startswith("/static/"):
                    rel = Path(p[len("/static/"):])
                    return self._file(self._safe_static(rel))
                self.send_error(404, "Not found")
            except Exception as exc:
                self._send_err(exc)

        def _safe_static(self, rel: Path) -> Path:
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
                raise service.ServiceError("非法路径")
            return target

        def _meta(self) -> dict:
            def rows(table):
                return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            data = {
                "lines": rows("lines"),
                "stations": rows("stations"),
                "models": rows("models"),
                "staff": rows("staff"),
                "station_models": [dict(r) for r in conn.execute(
                    "SELECT * FROM station_models")],
                "fixture_models": [dict(r) for r in conn.execute(
                    "SELECT * FROM fixture_models")],
                "type_labels": service.TYPE_LABEL,
                "status_labels": {
                    "available": "可用", "reserved": "已预订", "in_use": "领用中",
                    "in_repair": "维修中", "scrapped": "已报废",
                },
                "request_status_labels": {
                    "pending": "待审批", "approved": "已批准待领用",
                    "picked": "已领用", "returned": "已归还待点检",
                    "inspected": "点检完成", "rejected": "已驳回", "expired": "已逾期",
                },
            }
            return data

        # ------------------------------------------------------------ POST
        def do_POST(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            try:
                # photo upload: raw body, name in query
                if p == "/api/photos":
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    name = q.get("name", [""])[0].strip()
                    if not re.fullmatch(r"[\w.\-]+\.(?:svg|png|jpg|jpeg)", name):
                        raise service.ServiceError("照片名仅允许字母数字，扩展名 svg/png/jpg")
                    (photo_dir / name).write_bytes(raw)
                    return self._send_json({"photo_path": name, "bytes": len(raw)})

                b = self._body()
                actor = b.pop("actor_id", None) or q.get("actor", [""])[0]
                if not actor:
                    raise service.ServiceError("缺少 actor_id")

                if p == "/api/requests":
                    rid = service.create_request(
                        conn, b["fixture_id"], b["station_id"], b["model_id"],
                        actor, b.get("purpose", ""), int(b.get("hours", 24)))
                    return self._send_json({"id": rid}, 201)

                m = re.fullmatch(r"/api/requests/([\w-]+)/(approve|reject|pickup|return|inspect)", p)
                if m:
                    rid, action = m.group(1), m.group(2)
                    if action == "approve":
                        service.approve_request(conn, rid, actor)
                    elif action == "reject":
                        service.reject_request(conn, rid, actor, b.get("note", ""))
                    elif action == "pickup":
                        res = service.pickup(conn, rid, actor, b.get("token"))
                        return self._send_json(res)
                    elif action == "return":
                        service.return_fixture(conn, rid, actor)
                    else:
                        res = service.inspect(
                            conn, rid, actor, b["result"],
                            b.get("findings", ""), b.get("photo_path", ""))
                        return self._send_json(res)
                    return self._send_json({"ok": True})

                if p == "/api/repairs":
                    rid = service.start_repair(
                        conn, b["fixture_id"], actor, b["repairer_id"],
                        b.get("reason", ""))
                    return self._send_json({"id": rid}, 201)

                m = re.fullmatch(r"/api/repairs/([\w-]+)/(finish|review)", p)
                if m:
                    rid, action = m.group(1), m.group(2)
                    if action == "finish":
                        service.finish_repair(conn, rid, actor)
                    else:
                        service.review_repair(
                            conn, rid, actor, bool(b.get("approved", True)),
                            b.get("note", ""))
                    return self._send_json({"ok": True})

                if p == "/api/transfers":
                    tid = service.request_transfer(
                        conn, b["fixture_id"], b["to_station"], actor,
                        b.get("note", ""))
                    return self._send_json({"id": tid}, 201)

                m = re.fullmatch(r"/api/transfers/([\w-]+)/(approve|reject)", p)
                if m:
                    tid, action = m.group(1), m.group(2)
                    if action == "approve":
                        did = service.approve_transfer(conn, tid, actor)
                        return self._send_json({"ok": True, "disposal_id": did})
                    service.reject_transfer(conn, tid, actor, b.get("note", ""))
                    return self._send_json({"ok": True})

                m = re.fullmatch(r"/api/scrap/([\w-]+)/decide", p)
                if m:
                    service.decide_scrap(
                        conn, m.group(1), actor, bool(b.get("approved", True)),
                        b.get("note", ""))
                    return self._send_json({"ok": True})

                m = re.fullmatch(r"/api/plans/([\w-]+)/done", p)
                if m:
                    service.complete_maintenance(conn, m.group(1), actor)
                    return self._send_json({"ok": True})

                if p == "/api/reminders/read":
                    service.mark_reminders_read(conn)
                    return self._send_json({"ok": True})

                self.send_error(404, "Not found")
            except service.ServiceError as exc:
                return self._send_err(exc)
            except KeyError as exc:
                return self._send_err(service.ServiceError(f"缺少字段 {exc}"))
            except Exception as exc:
                return self._send_json({"error": str(exc), "kind": "ServerError"}, 500)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8000,
          db_path: Path | None = None, photo_dir: Path | None = None) -> None:
    app = App(db_path or ROOT / "data" / "factory.db",
              photo_dir or ROOT / "data" / "photos")
    app.start_scheduler()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"工装夹具借用与寿命记录系统 → http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
