"""业务服务层：夹具寿命、申请借还、点检、维修复核、处置单与保养。

所有写操作使用短连接 + ``BEGIN IMMEDIATE``，领用抢锁依靠条件 UPDATE
原子完成（``WHERE status='available'``），两名操作员同时领取只有一人成功。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from .db import Database

# 夹具状态
AVAILABLE = "available"     # 可用
LOCKED = "locked"           # 已领用（锁定）
INSPECTING = "inspecting"   # 归还待点检
REPAIRING = "repairing"     # 维修中
PENDING_SCRAP = "pending_scrap"  # 待报废审批
SCRAPPED = "scrapped"       # 已报废

# 申请状态
REQ_PENDING = "pending"
REQ_LOCKED = "locked"
REQ_INSPECTING = "inspecting"
REQ_CLOSED = "closed"
REQ_FAILED = "failed"
REQ_CANCELLED = "cancelled"

# 处置单类型
DISP_DEFORMATION = "deformation"      # 夹具变形
DISP_MISSING_PIN = "missing_pin"      # 定位销缺失
DISP_OVER_LIFE = "over_life"          # 超寿命
DISP_TRANSFER = "transfer"            # 跨线调拨
DISP_OVERDUE = "overdue"              # 归还逾期

FIXTURE_STATUS_TEXT = {
    AVAILABLE: "可用",
    LOCKED: "已领用",
    INSPECTING: "待点检",
    REPAIRING: "维修中",
    PENDING_SCRAP: "待报废",
    SCRAPPED: "已报废",
}

DEFECT_TEXT = {
    DISP_DEFORMATION: "夹具变形",
    DISP_MISSING_PIN: "定位销缺失",
}


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


def _row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    return conn.execute(sql, params).fetchone()


def _dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


class FixtureService:
    def __init__(
        self,
        db: Database,
        photos_dir: str,
        now: Optional[Callable[[], datetime]] = None,
        loan_hours: int = 24,
    ):
        self.db = db
        self.photos_dir = photos_dir
        self._now_fn = now or datetime.now
        self.loan_hours = loan_hours
        os.makedirs(photos_dir, exist_ok=True)

    # ------------------------------------------------------------------ utils
    def now(self) -> datetime:
        return self._now_fn()

    def _code(self, prefix: str) -> str:
        return f"{prefix}-{self.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"

    # ------------------------------------------------------------- 基础查询
    def list_lines(self) -> list[dict]:
        conn = self.db.connect()
        try:
            return _dicts(conn.execute("SELECT * FROM lines ORDER BY code"))
        finally:
            conn.close()

    def list_stations(self, line_id: Optional[int] = None) -> list[dict]:
        conn = self.db.connect()
        try:
            if line_id:
                rows = conn.execute(
                    "SELECT * FROM stations WHERE line_id=? ORDER BY code", (line_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM stations ORDER BY code")
            return _dicts(rows)
        finally:
            conn.close()

    def list_models(self) -> list[dict]:
        conn = self.db.connect()
        try:
            return _dicts(conn.execute("SELECT * FROM models ORDER BY code"))
        finally:
            conn.close()

    def list_employees(self) -> list[dict]:
        conn = self.db.connect()
        try:
            return _dicts(conn.execute(
                "SELECT e.*, l.name AS line_name FROM employees e "
                "LEFT JOIN lines l ON l.id=e.line_id ORDER BY e.code"
            ))
        finally:
            conn.close()

    def _employee(self, conn, code: str) -> sqlite3.Row:
        emp = _row(conn, "SELECT * FROM employees WHERE code=?", (code,))
        if not emp:
            raise ServiceError(f"人员编号不存在：{code}", "unknown_employee")
        return emp

    def fixture_detail(self, fixture_id: int) -> dict:
        conn = self.db.connect()
        try:
            f = _row(conn, "SELECT * FROM fixtures WHERE id=?", (fixture_id,))
            if not f:
                raise ServiceError("夹具不存在", "not_found")
            data = self._fixture_with_extras(conn, dict(f))
            return data
        finally:
            conn.close()

    def _fixture_models(self, conn, fixture_id: int) -> list[dict]:
        return _dicts(conn.execute(
            "SELECT m.* FROM models m JOIN fixture_models fm ON fm.model_id=m.id "
            "WHERE fm.fixture_id=? ORDER BY m.code", (fixture_id,)
        ))

    def latest_inspection(self, conn, fixture_id: int) -> Optional[dict]:
        r = _row(conn,
            "SELECT i.*, e.name AS inspector_name FROM inspections i "
            "JOIN employees e ON e.id=i.inspector_id "
            "WHERE i.fixture_id=? ORDER BY i.created_at DESC, i.id DESC LIMIT 1",
            (fixture_id,))
        return dict(r) if r else None

    def _fixture_with_extras(self, conn, f: dict) -> dict:
        f["models"] = self._fixture_models(conn, f["id"])
        f["status_text"] = FIXTURE_STATUS_TEXT.get(f["status"], f["status"])
        f["life_pct"] = round(min(f["use_count"] / f["life_threshold"], 1) * 100)
        f["life_remaining"] = max(f["life_threshold"] - f["use_count"], 0)
        if f["last_maintenance_at"]:
            due = datetime.fromisoformat(f["last_maintenance_at"]) + timedelta(
                days=f["maintenance_cycle_days"])
            f["next_maintenance_at"] = due.isoformat(timespec="seconds")
        else:
            f["next_maintenance_at"] = None
        f["latest_inspection"] = self.latest_inspection(conn, f["id"])
        st = _row(conn,
            "SELECT s.*, l.code AS line_code, l.name AS line_name FROM stations s "
            "JOIN lines l ON l.id=s.line_id WHERE s.id=?", (f["station_id"],))
        f["station"] = dict(st) if st else None
        ln = _row(conn, "SELECT * FROM lines WHERE id=?", (f["line_id"],))
        f["line"] = dict(ln) if ln else None
        return f

    def ledger(self, status: Optional[str] = None, line_id: Optional[int] = None) -> list[dict]:
        """夹具台账。"""
        sql = "SELECT * FROM fixtures WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if line_id:
            sql += " AND line_id=?"
            params.append(line_id)
        sql += " ORDER BY code"
        conn = self.db.connect()
        try:
            return [self._fixture_with_extras(conn, dict(r))
                    for r in conn.execute(sql, params)]
        finally:
            conn.close()

    # ------------------------------------------------------------- 工位匹配
    def station_view(self, station_id: int, model_id: Optional[int] = None) -> dict:
        conn = self.db.connect()
        try:
            st = _row(conn,
                "SELECT s.*, l.code AS line_code, l.name AS line_name FROM stations s "
                "JOIN lines l ON l.id=s.line_id WHERE s.id=?", (station_id,))
            if not st:
                raise ServiceError("工位不存在", "unknown_station")
            allowed = _dicts(conn.execute(
                "SELECT m.* FROM models m JOIN station_models sm ON sm.model_id=m.id "
                "WHERE sm.station_id=? ORDER BY m.code", (station_id,)))
            # 直接匹配：夹具当前就在该工位、支持该机型、状态可用
            matches: list[dict] = []
            if model_id:
                if not _row(conn,
                    "SELECT 1 FROM station_models WHERE station_id=? AND model_id=?",
                    (station_id, model_id)):
                    raise ServiceError("该工位不允许生产所选机型（工位/机型适配校验失败）",
                                       "model_mismatch")
                rows = conn.execute(
                    "SELECT f.* FROM fixtures f "
                    "JOIN fixture_models fm ON fm.fixture_id=f.id AND fm.model_id=? "
                    "WHERE f.station_id=? AND f.status=? ORDER BY f.code",
                    (model_id, station_id, AVAILABLE))
                matches = [self._fixture_with_extras(conn, dict(r)) for r in rows]
            return {
                "station": dict(st),
                "allowed_models": allowed,
                "matches": matches,
            }
        finally:
            conn.close()

    def cross_line_candidates(self, station_id: int, model_id: int) -> list[dict]:
        """其他产线上支持机型、可跨线调拨的夹具。"""
        conn = self.db.connect()
        try:
            st = _row(conn, "SELECT line_id FROM stations WHERE id=?", (station_id,))
            rows = conn.execute(
                "SELECT f.* FROM fixtures f "
                "JOIN fixture_models fm ON fm.fixture_id=f.id AND fm.model_id=? "
                "WHERE f.line_id<>? AND f.status=? ORDER BY f.code",
                (model_id, st["line_id"], AVAILABLE))
            return [self._fixture_with_extras(conn, dict(r)) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------- 借用申请
    def apply(self, applicant_code: str, station_id: int, model_id: int,
              note: str = "", preferred_fixture_id: Optional[int] = None,
              want_transfer_fixture_id: Optional[int] = None) -> dict:
        """提交借用申请。必须匹配工位与机型；跨线夹具走调拨处置单。"""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            emp = self._employee(conn, applicant_code)
            if emp["frozen"]:
                raise ServiceError(
                    f"{emp['name']} 因上次点检不合格已被冻结，请等待维修复核完成",
                    "applicant_frozen")
            st = _row(conn, "SELECT * FROM stations WHERE id=?", (station_id,))
            if not st:
                raise ServiceError("工位不存在", "unknown_station")
            mdl = _row(conn, "SELECT * FROM models WHERE id=?", (model_id,))
            if not mdl:
                raise ServiceError("机型不存在", "unknown_model")
            if not _row(conn,
                "SELECT 1 FROM station_models WHERE station_id=? AND model_id=?",
                (station_id, model_id)):
                raise ServiceError(
                    f"工位 {st['code']} 不支持机型 {mdl['code']}，适配校验失败",
                    "model_mismatch")

            due = (self.now() + timedelta(hours=self.loan_hours)).isoformat(timespec="seconds")
            code = self._code("BR")
            cur = conn.execute(
                "INSERT INTO requests(code, station_id, model_id, applicant_id,"
                " fixture_id, status, note, due_at, created_at, updated_at)"
                " VALUES(?,?,?,?,NULL,?,?,?,?,?)",
                (code, station_id, model_id, emp["id"], REQ_PENDING, note,
                 due, self.now().isoformat(timespec="seconds"),
                 self.now().isoformat(timespec="seconds")))
            req_id = cur.lastrowid

            transfer_disp = None
            target_fid = want_transfer_fixture_id or preferred_fixture_id
            if target_fid:
                f = _row(conn, "SELECT * FROM fixtures WHERE id=?", (target_fid,))
                if not f:
                    raise ServiceError("指定的夹具不存在", "unknown_fixture")
                if f["status"] == REPAIRING:
                    raise ServiceError(
                        f"夹具 {f['code']} 维修中，不能被预订", "fixture_in_repair")
                if f["status"] in (SCRAPPED, PENDING_SCRAP):
                    raise ServiceError(f"夹具 {f['code']} 已进入报废流程，不能预订",
                                       "fixture_scrapping")
                if not _row(conn,
                    "SELECT 1 FROM fixture_models WHERE fixture_id=? AND model_id=?",
                    (target_fid, model_id)):
                    raise ServiceError(
                        f"夹具 {f['code']} 不支持机型 {mdl['code']}，适配校验失败",
                        "fixture_model_mismatch")
                if f["line_id"] != st["line_id"]:
                    # 跨线：生成调拨处置单，主任审批后方可领用
                    dcode = self._code("DP")
                    dc = conn.execute(
                        "INSERT INTO dispositions(code, fixture_id, type, status, detail,"
                        " ref, target_station_id, created_by, created_at)"
                        " VALUES(?,?,?, 'open', ?, ?, ?, ?, ?)",
                        (dcode, f["id"], DISP_TRANSFER,
                         f"跨线调拨申请：{f['code']} 调至工位 {st['code']}（申请单 {code}）",
                         code, station_id, emp["id"],
                         self.now().isoformat(timespec="seconds")))
                    conn.execute("UPDATE requests SET fixture_id=? WHERE id=?",
                                 (f["id"], req_id))
                    transfer_disp = dict(_row(conn,
                        "SELECT * FROM dispositions WHERE id=?", (dc.lastrowid,)))
            conn.commit()
            req = dict(_row(conn, "SELECT * FROM requests WHERE id=?", (req_id,)))
            req["transfer_disposition"] = transfer_disp
            return req
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def request_queue(self) -> list[dict]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT r.*, s.code AS station_code, s.name AS station_name,"
                " l.code AS station_line_code,"
                " m.code AS model_code, m.name AS model_name,"
                " e.code AS applicant_code, e.name AS applicant_name, e.frozen,"
                " f.code AS fixture_code, f.status AS fixture_status,"
                " d.id AS transfer_disp_id, d.status AS transfer_disp_status"
                " FROM requests r"
                " JOIN stations s ON s.id=r.station_id"
                " JOIN lines l ON l.id=s.line_id"
                " JOIN models m ON m.id=r.model_id"
                " JOIN employees e ON e.id=r.applicant_id"
                " LEFT JOIN fixtures f ON f.id=r.fixture_id"
                " LEFT JOIN dispositions d ON d.type=? AND d.ref=r.code"
                " ORDER BY r.created_at",
                (DISP_TRANSFER,))
            return _dicts(rows)
        finally:
            conn.close()

    # ------------------------------------------------------------- 领用（抢锁）
    def checkout(self, request_id: int, fixture_id: int,
                 operator_code: str) -> dict:
        """扫码领用。原子条件更新保证并发下只成功一人。"""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            emp = self._employee(conn, operator_code)
            if emp["frozen"]:
                raise ServiceError("账号已被点检冻结，不能领用", "operator_frozen")
            req = _row(conn, "SELECT * FROM requests WHERE id=?", (request_id,))
            if not req:
                raise ServiceError("申请单不存在", "unknown_request")
            if req["status"] != REQ_PENDING:
                raise ServiceError(
                    f"申请单 {req['code']} 当前状态 {req['status']}，不能领用",
                    "request_not_pending")
            if req["applicant_id"] != emp["id"]:
                raise ServiceError("只能由申请人本人领用", "not_applicant")

            f = _row(conn, "SELECT * FROM fixtures WHERE id=?", (fixture_id,))
            if not f:
                raise ServiceError("夹具不存在", "unknown_fixture")
            st = _row(conn, "SELECT * FROM stations WHERE id=?", (req["station_id"],))
            # 适配：机型
            if not _row(conn,
                "SELECT 1 FROM fixture_models WHERE fixture_id=? AND model_id=?",
                (fixture_id, req["model_id"])):
                raise ServiceError("夹具与申请机型不匹配，禁止领用", "model_mismatch")
            # 适配：工位（当前工位一致，或跨线调拨已审批）
            if f["station_id"] != req["station_id"]:
                approved = _row(conn,
                    "SELECT 1 FROM dispositions WHERE type=? AND fixture_id=?"
                    " AND target_station_id=? AND status='done'",
                    (DISP_TRANSFER, fixture_id, req["station_id"]))
                if not approved:
                    raise ServiceError(
                        "夹具不在申请工位，且无已审批的跨线调拨单，禁止领用",
                        "station_mismatch")
            if f["status"] == REPAIRING:
                raise ServiceError("维修中的夹具不能领用", "fixture_in_repair")
            if f["status"] in (SCRAPPED, PENDING_SCRAP):
                raise ServiceError("夹具已进入报废流程，不能领用", "fixture_scrapping")
            if f["status"] == LOCKED:
                raise ServiceError(f"夹具 {f['code']} 已被他人领取，竞争失败",
                                   "already_locked")

            # ★ 原子抢锁：只有 status='available' 时更新成功
            cur = conn.execute(
                "UPDATE fixtures SET status=?, updated_at=? "
                "WHERE id=? AND status=?",
                (LOCKED, self.now().isoformat(timespec="seconds"),
                 fixture_id, AVAILABLE))
            if cur.rowcount == 0:
                raise ServiceError(
                    f"夹具 {f['code']} 刚刚被他人锁定，竞争失败", "lock_lost")

            due = (self.now() + timedelta(hours=self.loan_hours)).isoformat(timespec="seconds")
            bc = conn.execute(
                "INSERT INTO borrows(request_id, fixture_id, borrower_id,"
                " checked_out_at, due_at) VALUES(?,?,?,?,?)",
                (request_id, fixture_id, emp["id"],
                 self.now().isoformat(timespec="seconds"), due))
            conn.execute(
                "UPDATE requests SET status=?, fixture_id=?, updated_at=? WHERE id=?",
                (REQ_LOCKED, fixture_id,
                 self.now().isoformat(timespec="seconds"), request_id))
            conn.commit()
            return {
                "borrow": dict(_row(conn, "SELECT * FROM borrows WHERE id=?",
                                    (bc.lastrowid,))),
                "fixture_code": f["code"],
                "station_code": st["code"],
                "operator": emp["name"],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def active_borrows(self) -> list[dict]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT b.*, f.code AS fixture_code, f.name AS fixture_name,"
                " s.code AS station_code, e.code AS borrower_code, e.name AS borrower_name,"
                " r.code AS request_code"
                " FROM borrows b"
                " JOIN fixtures f ON f.id=b.fixture_id"
                " JOIN requests r ON r.id=b.request_id"
                " JOIN stations s ON s.id=r.station_id"
                " JOIN employees e ON e.id=b.borrower_id"
                " WHERE b.returned_at IS NULL ORDER BY b.checked_out_at")
            return _dicts(rows)
        finally:
            conn.close()

    # ------------------------------------------------------------- 归还 → 点检
    def return_fixture(self, borrow_id: int, use_delta: int = 1) -> dict:
        """归还：进入待点检，不会直接变回可用。"""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            b = _row(conn, "SELECT * FROM borrows WHERE id=?", (borrow_id,))
            if not b:
                raise ServiceError("借用记录不存在", "unknown_borrow")
            if b["returned_at"]:
                raise ServiceError("该夹具已归还", "already_returned")
            ts = self.now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE borrows SET returned_at=?, use_delta=? WHERE id=?",
                (ts, max(use_delta, 1), borrow_id))
            conn.execute("UPDATE fixtures SET status=?, updated_at=? WHERE id=?",
                         (INSPECTING, ts, b["fixture_id"]))
            conn.execute("UPDATE requests SET status=?, updated_at=? WHERE id=?",
                         (REQ_INSPECTING, ts, b["request_id"]))
            conn.commit()
            return dict(_row(conn, "SELECT * FROM borrows WHERE id=?", (borrow_id,)))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def inspect(self, request_id: int, inspector_code: str, result: str,
                defect: Optional[str] = None, note: str = "",
                photo_path: Optional[str] = None) -> dict:
        """归还点检。合格：累计寿命并判断超寿命；不合格：冻结申请人、开维修。"""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            req = _row(conn, "SELECT * FROM requests WHERE id=?", (request_id,))
            if not req:
                raise ServiceError("申请单不存在", "unknown_request")
            if req["status"] != REQ_INSPECTING:
                raise ServiceError("只有归还待点检的申请可以点检", "not_inspecting")
            inspector = self._employee(conn, inspector_code)
            b = _row(conn,
                "SELECT * FROM borrows WHERE request_id=? AND returned_at IS NOT NULL"
                " ORDER BY id DESC LIMIT 1", (request_id,))
            f = _row(conn, "SELECT * FROM fixtures WHERE id=?", (req["fixture_id"],))
            ts = self.now().isoformat(timespec="seconds")
            icode = self._code("IC")
            cur = conn.execute(
                "INSERT INTO inspections(code, fixture_id, request_id, inspector_id,"
                " result, defect, note, photo_path, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (icode, f["id"], request_id, inspector["id"], result, defect,
                 note, photo_path, ts))
            inspection = dict(_row(conn, "SELECT * FROM inspections WHERE id=?",
                                   (cur.lastrowid,)))

            if result == "pass":
                new_count = f["use_count"] + max(b["use_delta"], 1)
                conn.execute("UPDATE fixtures SET use_count=?, updated_at=? WHERE id=?",
                             (new_count, ts, f["id"]))
                disposition = None
                if new_count >= f["life_threshold"]:
                    # 超寿命 → 处置单 + 待报废审批
                    dcode = self._code("DP")
                    dc = conn.execute(
                        "INSERT INTO dispositions(code, fixture_id, type, status, detail,"
                        " ref, created_by, created_at) VALUES(?,?,?, 'open', ?, ?, ?, ?)",
                        (dcode, f["id"], DISP_OVER_LIFE,
                         f"累计使用 {new_count} 次达到寿命阈值 {f['life_threshold']}（点检单 {icode}）",
                         icode, inspector["id"], ts))
                    conn.execute("UPDATE fixtures SET status=? WHERE id=?",
                                 (PENDING_SCRAP, f["id"]))
                    disposition = dict(_row(conn,
                        "SELECT * FROM dispositions WHERE id=?", (dc.lastrowid,)))
                    conn.execute("UPDATE requests SET status=?, updated_at=? WHERE id=?",
                                 (REQ_CLOSED, ts, request_id))
                else:
                    conn.execute("UPDATE fixtures SET status=?, updated_at=? WHERE id=?",
                                 (AVAILABLE, ts, f["id"]))
                    conn.execute("UPDATE requests SET status=?, updated_at=? WHERE id=?",
                                 (REQ_CLOSED, ts, request_id))
                conn.commit()
                return {"inspection": inspection, "disposition": disposition,
                        "use_count": new_count}

            # 不合格：变形 / 定位销缺失 → 处置单 + 维修单，冻结借用人
            dtype = defect if defect in (DISP_DEFORMATION, DISP_MISSING_PIN) \
                else DISP_DEFORMATION
            dcode = self._code("DP")
            dc = conn.execute(
                "INSERT INTO dispositions(code, fixture_id, type, status, detail,"
                " ref, created_by, created_at) VALUES(?,?,?, 'open', ?, ?, ?, ?)",
                (dcode, f["id"], dtype,
                 f"点检不合格：{DEFECT_TEXT[dtype]}（点检单 {icode}）。{note}",
                 icode, inspector["id"], ts))
            rcode = self._code("RP")
            rc = conn.execute(
                "INSERT INTO repairs(code, fixture_id, inspection_id, description,"
                " status, created_at) VALUES(?,?,?,?, 'open', ?)",
                (rcode, f["id"], cur.lastrowid,
                 f"{DEFECT_TEXT[dtype]}，待维修（处置单 {dcode}）", ts))
            conn.execute("UPDATE fixtures SET status=?, updated_at=? WHERE id=?",
                         (REPAIRING, ts, f["id"]))
            conn.execute("UPDATE employees SET frozen=1 WHERE id=?",
                         (b["borrower_id"],))
            conn.execute("UPDATE requests SET status=?, updated_at=? WHERE id=?",
                         (REQ_FAILED, ts, request_id))
            conn.commit()
            return {
                "inspection": inspection,
                "disposition": dict(_row(conn,
                    "SELECT * FROM dispositions WHERE id=?", (dc.lastrowid,))),
                "repair": dict(_row(conn,
                    "SELECT * FROM repairs WHERE id=?", (rc.lastrowid,))),
                "frozen_borrower_id": b["borrower_id"],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_inspections(self, fixture_id: Optional[int] = None) -> list[dict]:
        conn = self.db.connect()
        try:
            sql = ("SELECT i.*, e.name AS inspector_name, f.code AS fixture_code "
                   "FROM inspections i JOIN employees e ON e.id=i.inspector_id "
                   "JOIN fixtures f ON f.id=i.fixture_id")
            params: list[Any] = []
            if fixture_id:
                sql += " WHERE i.fixture_id=?"
                params.append(fixture_id)
            sql += " ORDER BY i.created_at DESC, i.id DESC"
            return _dicts(conn.execute(sql, params))
        finally:
            conn.close()

    # ------------------------------------------------------------- 维修与复核
    def list_repairs(self, status: Optional[str] = None) -> list[dict]:
        conn = self.db.connect()
        try:
            sql = ("SELECT r.*, f.code AS fixture_code, f.name AS fixture_name,"
                   " e1.name AS repairman_name, e2.name AS reviewer_name"
                   " FROM repairs r JOIN fixtures f ON f.id=r.fixture_id"
                   " LEFT JOIN employees e1 ON e1.id=r.repairman_id"
                   " LEFT JOIN employees e2 ON e2.id=r.reviewer_id")
            params: list[Any] = []
            if status:
                sql += " WHERE r.status=?"
                params.append(status)
            sql += " ORDER BY r.created_at DESC"
            return _dicts(conn.execute(sql, params))
        finally:
            conn.close()

    def repair_done(self, repair_id: int, repairman_code: str,
                    description: str = "") -> dict:
        """维修工完成维修 → 待另一名员工复核。"""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = _row(conn, "SELECT * FROM repairs WHERE id=?", (repair_id,))
            if not r:
                raise ServiceError("维修单不存在", "unknown_repair")
            if r["status"] != "open":
                raise ServiceError("只有待处理维修单可以填报完成", "repair_not_open")
            man = self._employee(conn, repairman_code)
            ts = self.now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE repairs SET repairman_id=?, repaired_at=?, status='in_review',"
                " description=? WHERE id=?",
                (man["id"], ts,
                 (description or r["description"]), repair_id))
            conn.commit()
            return dict(_row(conn, "SELECT * FROM repairs WHERE id=?", (repair_id,)))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def repair_review(self, repair_id: int, reviewer_code: str,
                      passed: bool, note: str = "") -> dict:
        """另一名员工复核；同一人不能复核自己的维修。通过后夹具恢复可用。"""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = _row(conn, "SELECT * FROM repairs WHERE id=?", (repair_id,))
            if not r:
                raise ServiceError("维修单不存在", "unknown_repair")
            if r["status"] != "in_review":
                raise ServiceError("只有待复核的维修单可以复核", "not_in_review")
            reviewer = self._employee(conn, reviewer_code)
            if reviewer["id"] == r["repairman_id"]:
                raise ServiceError("维修人与复核人不能是同一名员工", "self_review")
            ts = self.now().isoformat(timespec="seconds")
            result = "pass" if passed else "fail"

            # 找到本次维修对应的借用申请人（点检失败时被冻结的人）
            insp = _row(conn, "SELECT * FROM inspections WHERE id=?",
                        (r["inspection_id"],))
            frozen_emp_id = None
            if insp:
                req = _row(conn, "SELECT * FROM requests WHERE id=?",
                           (insp["request_id"],))
                if req:
                    frozen_emp_id = req["applicant_id"]

            if passed:
                conn.execute(
                    "UPDATE repairs SET reviewer_id=?, reviewed_at=?,"
                    " review_result='pass', status='done', description=? WHERE id=?",
                    (reviewer["id"], ts,
                     r["description"] + (f"｜复核意见：{note}" if note else ""),
                     repair_id))
                conn.execute("UPDATE fixtures SET status=?, updated_at=? WHERE id=?",
                             (AVAILABLE, ts, r["fixture_id"]))
                conn.execute(
                    "UPDATE dispositions SET status='done', decided_by=?, decided_at=?"
                    " WHERE fixture_id=? AND type IN (?, ?) AND status='open'",
                    (reviewer["id"], ts, r["fixture_id"],
                     DISP_DEFORMATION, DISP_MISSING_PIN))
                if frozen_emp_id:
                    conn.execute("UPDATE employees SET frozen=0 WHERE id=?",
                                 (frozen_emp_id,))
            else:
                conn.execute(
                    "UPDATE repairs SET reviewer_id=?, reviewed_at=?,"
                    " review_result='fail', status='open', description=? WHERE id=?",
                    (reviewer["id"], ts,
                     r["description"] + (f"｜复核打回：{note}" if note else ""),
                     repair_id))
            conn.commit()
            return dict(_row(conn, "SELECT * FROM repairs WHERE id=?", (repair_id,)))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------- 处置 / 报废 / 调拨
    def list_dispositions(self, status: Optional[str] = None,
                          type: Optional[str] = None) -> list[dict]:
        conn = self.db.connect()
        try:
            sql = ("SELECT d.*, f.code AS fixture_code, f.name AS fixture_name,"
                   " f.use_count, f.life_threshold, f.status AS fixture_status,"
                   " s.code AS target_station_code,"
                   " e1.name AS created_by_name, e2.name AS decided_by_name"
                   " FROM dispositions d JOIN fixtures f ON f.id=d.fixture_id"
                   " LEFT JOIN stations s ON s.id=d.target_station_id"
                   " LEFT JOIN employees e1 ON e1.id=d.created_by"
                   " LEFT JOIN employees e2 ON e2.id=d.decided_by")
            where, params = [], []
            if status:
                where.append("d.status=?")
                params.append(status)
            if type:
                where.append("d.type=?")
                params.append(type)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY d.created_at DESC"
            result = _dicts(conn.execute(sql, params))
            for d in result:
                d["latest_inspection"] = self.latest_inspection(conn, d["fixture_id"])
            return result
        finally:
            conn.close()

    def scrap_decide(self, disposition_id: int, manager_code: str,
                     approve: bool, note: str = "") -> dict:
        """报废审批（仅主任）：固定核对累计次数/阈值/最近点检。批准报废或驳回复用。"""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            d = _row(conn, "SELECT * FROM dispositions WHERE id=?", (disposition_id,))
            if not d:
                raise ServiceError("处置单不存在", "unknown_disposition")
            if d["type"] != DISP_OVER_LIFE:
                raise ServiceError("只有超寿命处置单走报废审批", "not_scrap")
            if d["status"] != "open":
                raise ServiceError("该处置单已审批", "already_decided")
            mgr = self._employee(conn, manager_code)
            if mgr["role"] != "manager":
                raise ServiceError("只有车间主任可以审批报废（跨线/越权拦截）",
                                   "forbidden")
            f = _row(conn, "SELECT * FROM fixtures WHERE id=?", (d["fixture_id"],))
            latest = self.latest_inspection(conn, f["id"])
            ts = self.now().isoformat(timespec="seconds")
            if approve:
                conn.execute(
                    "UPDATE fixtures SET status=?, updated_at=? WHERE id=?",
                    (SCRAPPED, ts, f["id"]))
                conn.execute(
                    "UPDATE dispositions SET status='scrapped', decided_by=?,"
                    " decided_at=?, detail=? WHERE id=?",
                    (mgr["id"], ts,
                     d["detail"] + (f"｜批准报废：{note}" if note else "｜批准报废"),
                     disposition_id))
            else:
                # 驳回：大修/降级后继续使用，寿命阈值上调（等效延寿备案）
                conn.execute(
                    "UPDATE fixtures SET status=?, use_count=0, updated_at=? WHERE id=?",
                    (AVAILABLE, ts, f["id"]))
                conn.execute(
                    "UPDATE dispositions SET status='rejected', decided_by=?,"
                    " decided_at=?, detail=? WHERE id=?",
                    (mgr["id"], ts,
                     d["detail"] + (f"｜驳回，大修后复用：{note}" if note else "｜驳回复用"),
                     disposition_id))
            conn.commit()
            return {
                "disposition": dict(_row(conn,
                    "SELECT * FROM dispositions WHERE id=?", (disposition_id,))),
                "fixture": dict(_row(conn, "SELECT * FROM fixtures WHERE id=?",
                                     (f["id"],))),
                "checked": {
                    "use_count": f["use_count"],
                    "life_threshold": f["life_threshold"],
                    "latest_inspection": latest,
                },
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def transfer_decide(self, disposition_id: int, manager_code: str,
                        approve: bool, note: str = "") -> dict:
        """跨线调拨审批（仅主任）。通过后夹具划归目标工位/产线，申请方可领用。"""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            d = _row(conn, "SELECT * FROM dispositions WHERE id=?", (disposition_id,))
            if not d:
                raise ServiceError("处置单不存在", "unknown_disposition")
            if d["type"] != DISP_TRANSFER:
                raise ServiceError("该接口仅处理跨线调拨单", "not_transfer")
            if d["status"] != "open":
                raise ServiceError("调拨单已审批", "already_decided")
            mgr = self._employee(conn, manager_code)
            if mgr["role"] != "manager":
                raise ServiceError("只有车间主任可以审批跨线调拨", "forbidden")
            ts = self.now().isoformat(timespec="seconds")
            if approve:
                st = _row(conn, "SELECT * FROM stations WHERE id=?",
                          (d["target_station_id"],))
                conn.execute(
                    "UPDATE fixtures SET station_id=?, line_id=?, updated_at=? WHERE id=?",
                    (st["id"], st["line_id"], ts, d["fixture_id"]))
                conn.execute(
                    "UPDATE dispositions SET status='done', decided_by=?,"
                    " decided_at=?, detail=? WHERE id=?",
                    (mgr["id"], ts,
                     d["detail"] + (f"｜已调拨：{note}" if note else "｜已调拨"),
                     disposition_id))
            else:
                conn.execute(
                    "UPDATE dispositions SET status='rejected', decided_by=?,"
                    " decided_at=? WHERE id=?",
                    (mgr["id"], ts, disposition_id))
                conn.execute(
                    "UPDATE requests SET status=?, updated_at=? WHERE code=?",
                    (REQ_CANCELLED, ts, d["ref"]))
            conn.commit()
            return dict(_row(conn, "SELECT * FROM dispositions WHERE id=?",
                             (disposition_id,)))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------- 保养
    def maintenance_plans(self, open_only: bool = False) -> list[dict]:
        conn = self.db.connect()
        try:
            sql = ("SELECT p.*, f.code AS fixture_code, f.name AS fixture_name,"
                   " f.status AS fixture_status, f.maintenance_cycle_days,"
                   " f.last_maintenance_at"
                   " FROM maintenance_plans p JOIN fixtures f ON f.id=p.fixture_id")
            if open_only:
                sql += " WHERE p.done_at IS NULL"
            sql += " ORDER BY p.due_at"
            return _dicts(conn.execute(sql))
        finally:
            conn.close()

    def generate_maintenance_plans(self) -> list[dict]:
        """扫描所有在役夹具，到期（或从未保养）生成计划与提醒。"""
        conn = self.db.connect()
        created: list[dict] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self.now()
            rows = conn.execute(
                "SELECT * FROM fixtures WHERE status<>?", (SCRAPPED,)).fetchall()
            for f in rows:
                base = (datetime.fromisoformat(f["last_maintenance_at"])
                        if f["last_maintenance_at"]
                        else datetime.fromisoformat(f["created_at"]))
                due = base + timedelta(days=f["maintenance_cycle_days"])
                if due <= now and not _row(conn,
                    "SELECT 1 FROM maintenance_plans WHERE fixture_id=? AND done_at IS NULL",
                    (f["id"],)):
                    cur = conn.execute(
                        "INSERT INTO maintenance_plans(fixture_id, due_at, created_at)"
                        " VALUES(?,?,?)",
                        (f["id"], due.isoformat(timespec="seconds"),
                         now.isoformat(timespec="seconds")))
                    plan = dict(_row(conn,
                        "SELECT * FROM maintenance_plans WHERE id=?", (cur.lastrowid,)))
                    self._add_reminder(conn, "maintenance_due", "maintenance_plans",
                                       cur.lastrowid,
                                       f"夹具 {f['code']} 保养已到期（应于 {due:%Y-%m-%d} 完成）")
                    created.append(plan)
            conn.commit()
            return created
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete_maintenance(self, plan_id: int, employee_code: str,
                             note: str = "") -> dict:
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            p = _row(conn, "SELECT * FROM maintenance_plans WHERE id=?", (plan_id,))
            if not p:
                raise ServiceError("保养计划不存在", "unknown_plan")
            if p["done_at"]:
                raise ServiceError("该保养计划已完成", "already_done")
            emp = self._employee(conn, employee_code)
            ts = self.now().isoformat(timespec="seconds")
            conn.execute("UPDATE maintenance_plans SET done_at=?, note=? WHERE id=?",
                         (ts, f"{emp['name']} 完成保养。{note}", plan_id))
            conn.execute("UPDATE fixtures SET last_maintenance_at=?, updated_at=? WHERE id=?",
                         (ts, ts, p["fixture_id"]))
            conn.commit()
            return dict(_row(conn, "SELECT * FROM maintenance_plans WHERE id=?",
                             (plan_id,)))
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------- 逾期扫描
    def scan_overdue(self) -> list[dict]:
        """归还逾期：为每个逾期未还借用生成处置单与提醒。"""
        conn = self.db.connect()
        created: list[dict] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self.now()
            rows = conn.execute(
                "SELECT b.*, f.code AS fixture_code FROM borrows b"
                " JOIN fixtures f ON f.id=b.fixture_id"
                " WHERE b.returned_at IS NULL AND b.overdue_disp_id IS NULL"
                " AND b.due_at < ?", (now.isoformat(timespec="seconds"),))
            for b in rows:
                fcode = b["fixture_code"]
                dcode = self._code("DP")
                cur = conn.execute(
                    "INSERT INTO dispositions(code, fixture_id, type, status, detail,"
                    " created_at) VALUES(?,?,?, 'open', ?, ?)",
                    (dcode, b["fixture_id"], DISP_OVERDUE,
                     f"借用逾期：{fcode} 应于 {b['due_at']} 归还",
                     now.isoformat(timespec="seconds")))
                conn.execute("UPDATE borrows SET overdue_disp_id=? WHERE id=?",
                             (cur.lastrowid, b["id"]))
                self._add_reminder(conn, "overdue", "borrows", b["id"],
                                   f"夹具 {b['fixture_code']} 归还逾期（截止 {b['due_at']}）")
                created.append(dict(_row(conn,
                    "SELECT * FROM dispositions WHERE id=?", (cur.lastrowid,))))
            conn.commit()
            return created
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _add_reminder(self, conn, rtype: str, table: str, ref_id: int,
                      message: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO reminders(type, ref_table, ref_id, message, created_at)"
            " VALUES(?,?,?,?,?)",
            (rtype, table, ref_id, message,
             self.now().isoformat(timespec="seconds")))

    def reminders(self) -> list[dict]:
        conn = self.db.connect()
        try:
            return _dicts(conn.execute(
                "SELECT * FROM reminders ORDER BY created_at DESC, id DESC LIMIT 50"))
        finally:
            conn.close()

    def save_photo(self, filename: str, data: bytes) -> str:
        safe = f"{self.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}-{filename}"
        safe = safe.replace("/", "_").replace("\\", "_")
        path = os.path.join(self.photos_dir, safe)
        with open(path, "wb") as fh:
            fh.write(data)
        return safe
