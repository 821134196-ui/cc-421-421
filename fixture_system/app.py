"""HTTP 服务：标准库实现，无第三方依赖。

- JSON API 前缀 ``/api/``
- 点检照片 multipart 上传，落盘到本地 photos 目录
- 进程内守护线程周期生成保养提醒、扫描归还逾期
"""
from __future__ import annotations

import cgi
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .db import Database
from .service import FixtureService, ServiceError
from . import seed

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, db_path: str, photos_dir: str,
                 with_samples: bool = False, scan_interval: int = 30):
        self.db = Database(db_path)
        self.svc = FixtureService(self.db, photos_dir)
        seed.seed(self.db, self.svc)
        if with_samples:
            seed.generate_samples(self.db, self.svc)
        self._stop_evt = threading.Event()
        self._scheduler = threading.Thread(
            target=self._scheduler_loop, args=(scan_interval,), daemon=True)
        self._scheduler.start()
        super().__init__(addr, Handler)

    def _scheduler_loop(self, interval: int) -> None:
        # 启动即扫描一次，之后周期执行
        while not self._stop_evt.wait(1):
            try:
                self.svc.generate_maintenance_plans()
                self.svc.scan_overdue()
            except Exception:
                pass
            self._stop_evt.wait(interval)

    def shutdown(self) -> None:  # type: ignore[override]
        self._stop_evt.set()
        super().shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = "FixtureSystem/1.0"

    @property
    def svc(self) -> FixtureService:
        return self.server.svc  # type: ignore[attr-defined]

    def log_message(self, fmt, *args) -> None:  # 安静日志
        pass

    # ------------------------------------------------------------ 输出工具
    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: ServiceError) -> None:
        status = {"not_found": 404, "forbidden": 403}.get(exc.code, 400)
        self._json({"error": str(exc), "code": exc.code}, status)

    def _file(self, path: str, ctype: str) -> None:
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode()) or {}
        except json.JSONDecodeError:
            raise ServiceError("请求体不是合法 JSON", "bad_json")

    def read_form(self) -> tuple[dict, dict]:
        """返回 (普通字段, 文件字段)。文件字段: {name: (filename, bytes)}。"""
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self.read_json(), {}
        env = {"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype}
        fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                              environ=env, keep_blank_values=True)
        fields, files = {}, {}
        for key in fs.keys():
            item = fs[key]
            if getattr(item, "filename", None):
                files[key] = (item.filename, item.file.read())
            else:
                fields[key] = item.value
        return fields, files

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        try:
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            if p == "/" or p == "/index.html":
                return self._file(os.path.join(STATIC_DIR, "index.html"),
                                  "text/html; charset=utf-8")
            if p.startswith("/static/"):
                name = p[len("/static/"):]
                safe = os.path.normpath(os.path.join(STATIC_DIR, name))
                if not safe.startswith(STATIC_DIR + os.sep):
                    return self.send_error(403)
                ctype = ("text/css; charset=utf-8" if name.endswith(".css")
                         else "application/javascript; charset=utf-8"
                         if name.endswith(".js") else "application/octet-stream")
                return self._file(safe, ctype)
            if p.startswith("/photos/"):
                name = os.path.basename(p[len("/photos/"):])
                return self._file(os.path.join(self.svc.photos_dir, name),
                                  "image/jpeg")
            if p == "/api/meta":
                return self._json({
                    "lines": self.svc.list_lines(),
                    "stations": self.svc.list_stations(),
                    "models": self.svc.list_models(),
                    "employees": self.svc.list_employees(),
                    "reminders": self.svc.reminders(),
                })
            if p == "/api/lines":
                return self._json(self.svc.list_lines())
            if p == "/api/stations":
                return self._json(self.svc.list_stations(
                    int(q["line"][0]) if q.get("line") else None))
            if p == "/api/models":
                return self._json(self.svc.list_models())
            if p == "/api/employees":
                return self._json(self.svc.list_employees())
            if p == "/api/fixtures":
                return self._json(self.svc.ledger(
                    q.get("status", [None])[0],
                    int(q["line"][0]) if q.get("line") else None))
            if p.startswith("/api/fixtures/"):
                return self._json(self.svc.fixture_detail(int(p.rsplit("/", 1)[1])))
            if p.startswith("/api/stations/"):
                parts = p.strip("/").split("/")
                sid = int(parts[2])
                if len(parts) == 4 and parts[3] == "crossline":
                    return self._json(self.svc.cross_line_candidates(
                        sid, int(q["model"][0])))
                return self._json(self.svc.station_view(
                    sid, int(q["model"][0]) if q.get("model") else None))
            if p == "/api/requests":
                return self._json(self.svc.request_queue())
            if p == "/api/borrows/active":
                return self._json(self.svc.active_borrows())
            if p == "/api/inspections":
                return self._json(self.svc.list_inspections(
                    int(q["fixture"][0]) if q.get("fixture") else None))
            if p == "/api/repairs":
                return self._json(self.svc.list_repairs(
                    q.get("status", [None])[0]))
            if p == "/api/dispositions":
                return self._json(self.svc.list_dispositions(
                    q.get("status", [None])[0], q.get("type", [None])[0]))
            if p == "/api/maintenance":
                return self._json(self.svc.maintenance_plans(
                    q.get("open", ["0"])[0] == "1"))
            if p == "/api/reminders":
                return self._json(self.svc.reminders())
            self.send_error(404)
        except ServiceError as exc:
            self._error(exc)
        except (ValueError, IndexError):
            self._json({"error": "路径或参数错误", "code": "bad_path"}, 400)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc), "code": "server_error"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            p = urlparse(self.path).path
            files: dict = {}
            if p == "/api/inspections":
                fields, files = self.read_form()
                body = fields
            else:
                body = self.read_json()

            if p == "/api/requests":
                return self._json(self.svc.apply(
                    body["applicant"], int(body["station_id"]),
                    int(body["model_id"]), body.get("note", ""),
                    int(body["fixture_id"]) if body.get("fixture_id") else None,
                    int(body["transfer_fixture_id"])
                    if body.get("transfer_fixture_id") else None))
            if p == "/api/borrows/checkout":
                return self._json(self.svc.checkout(
                    int(body["request_id"]), int(body["fixture_id"]),
                    body["operator"]))
            if p.startswith("/api/borrows/") and p.endswith("/return"):
                bid = int(p.split("/")[3])
                return self._json(self.svc.return_fixture(
                    bid, int(body.get("use_delta", 1))))
            if p == "/api/inspections":
                photo_name = None
                if files.get("photo"):
                    fname, data = files["photo"]
                    photo_name = self.svc.save_photo(fname, data)
                return self._json(self.svc.inspect(
                    int(body["request_id"]), body["inspector"],
                    body["result"], body.get("defect") or None,
                    body.get("note", ""), photo_name))
            if p.startswith("/api/repairs/"):
                rid = int(p.split("/")[3])
                if p.endswith("/done"):
                    return self._json(self.svc.repair_done(
                        rid, body["repairman"], body.get("description", "")))
                if p.endswith("/review"):
                    return self._json(self.svc.repair_review(
                        rid, body["reviewer"], bool(body["passed"]),
                        body.get("note", "")))
            if p.startswith("/api/dispositions/"):
                did = int(p.split("/")[3])
                if p.endswith("/scrap"):
                    return self._json(self.svc.scrap_decide(
                        did, body["manager"], bool(body["approve"]),
                        body.get("note", "")))
                if p.endswith("/transfer"):
                    return self._json(self.svc.transfer_decide(
                        did, body["manager"], bool(body["approve"]),
                        body.get("note", "")))
            if p == "/api/maintenance/scan":
                plans = self.svc.generate_maintenance_plans()
                overdue = self.svc.scan_overdue()
                return self._json({"maintenance_created": len(plans),
                                   "overdue_created": len(overdue)})
            if p.startswith("/api/maintenance/") and p.endswith("/complete"):
                pid = int(p.split("/")[3])
                return self._json(self.svc.complete_maintenance(
                    pid, body["employee"], body.get("note", "")))
            self.send_error(404)
        except ServiceError as exc:
            self._error(exc)
        except KeyError as exc:
            self._json({"error": f"缺少字段：{exc.args[0]}",
                        "code": "missing_field"}, 400)
        except (ValueError, IndexError):
            self._json({"error": "路径或参数错误", "code": "bad_path"}, 400)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc), "code": "server_error"}, 500)


def serve(host: str = "127.0.0.1", port: int = 8000,
          data_dir: str = "data", with_samples: bool = False,
          scan_interval: int = 30) -> AppServer:
    db_path = os.path.join(data_dir, "fixtures.db")
    photos_dir = os.path.join(data_dir, "photos")
    httpd = AppServer((host, port), db_path, photos_dir,
                      with_samples=with_samples, scan_interval=scan_interval)
    return httpd
