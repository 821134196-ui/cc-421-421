"""Core business logic for 工装夹具借用与寿命记录系统.

All write operations are serialized through the process-wide lock in db.py,
which makes the simultaneous pick-up race deterministic (exactly one winner).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from .db import db_lock

DAY = 86400

# 点检异常关键词 → 处置单类型
FINDING_KINDS = [
    ("变形", "deformation", "夹具变形"),
    ("定位销", "pin_missing", "定位销缺失"),
]

TYPE_LABEL = {
    "deformation": "夹具变形",
    "pin_missing": "定位销缺失",
    "over_life": "超寿命",
    "transfer": "跨线调拨",
    "overdue": "归还逾期",
    "scrap": "报废",
}


class ServiceError(Exception):
    """Rule violation (validation / state machine)."""


class PermissionError_(ServiceError):
    """Cross-line or role permission failure."""


class ConflictError(ServiceError):
    """Concurrent claim lost / fixture not in required state."""


# ---------------------------------------------------------------- helpers

def now() -> int:
    return int(time.time())


def _seq(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] + 1


def _new_id(conn, prefix: str, table: str, pad: int = 4) -> str:
    return f"{prefix}-{_seq(conn, table):0{pad}d}"


def _row(conn, sql: str, args=()) -> sqlite3.Row:
    r = conn.execute(sql, args).fetchone()
    if r is None:
        raise ServiceError("记录不存在")
    return r


def _raise_disposal(conn, dtype: str, fixture_id: str, raised_by: str,
                    related_id: str = "", detail: str = "") -> str:
    """Create a disposal order unless an open one of same type exists."""
    exists = conn.execute(
        """SELECT id FROM disposal_orders
           WHERE fixture_id=? AND type=? AND status='open'""",
        (fixture_id, dtype)).fetchone()
    if exists:
        return exists["id"]
    did = _new_id(conn, "DO", "disposal_orders")
    conn.execute(
        """INSERT INTO disposal_orders(id,type,fixture_id,related_id,raised_by,
               detail,created_at) VALUES (?,?,?,?,?,?,?)""",
        (did, dtype, fixture_id, related_id, raised_by, detail, now()))
    return did


# ---------------------------------------------------------------- queries

def _fixture_view(conn, r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["life_pct"] = round(r["use_count"] * 100.0 / r["life_threshold"], 1)
    d["over_life"] = r["use_count"] >= r["life_threshold"]
    d["models"] = [x["model_id"] for x in conn.execute(
        "SELECT model_id FROM fixture_models WHERE fixture_id=?", (r["id"],))]
    if r["station_id"]:
        st = conn.execute("SELECT id,name,line_id FROM stations WHERE id=?",
                          (r["station_id"],)).fetchone()
        d["station_name"] = st["name"] if st else ""
        d["line_id_now"] = st["line_id"] if st else d["line_id"]
    else:
        d["station_name"] = ""
        d["line_id_now"] = d["line_id"]
    ln = conn.execute("SELECT name FROM lines WHERE id=?", (d["line_id"],)).fetchone()
    d["line_name"] = ln["name"] if ln else ""
    d["type_label"] = TYPE_LABEL
    d["remaining"] = max(0, r["life_threshold"] - r["use_count"])
    # 下次保养
    mp = conn.execute(
        """SELECT due_count FROM maintenance_plans
           WHERE fixture_id=? AND done=0 ORDER BY due_count LIMIT 1""",
        (r["id"],)).fetchone()
    d["next_maintenance_at"] = mp["due_count"] if mp else None
    return d


def list_fixtures(conn, status: str | None = None, q: str | None = None) -> list[dict]:
    sql = "SELECT * FROM fixtures WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if q:
        sql += " AND (id LIKE ? OR name LIKE ? OR spec LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY id"
    return [_fixture_view(conn, r) for r in conn.execute(sql, args)]


def get_fixture(conn, fixture_id: str) -> dict[str, Any]:
    return _fixture_view(conn, _row(conn, "SELECT * FROM fixtures WHERE id=?",
                                    (fixture_id,)))


def list_requests(conn, status: str | None = None) -> list[dict]:
    sql = """SELECT r.*, f.name fixture_name, s.name station_name,
                    m.name model_name, st.name applicant_name
             FROM requests r
             JOIN fixtures f ON f.id=r.fixture_id
             JOIN stations s ON s.id=r.station_id
             JOIN models m ON m.id=r.model_id
             JOIN staff st ON st.id=r.applicant_id"""
    args: list[Any] = []
    if status:
        sql += " WHERE r.status=?"
        args.append(status)
    sql += " ORDER BY r.created_at DESC, r.id DESC"
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["overdue"] = bool(r["due_at"] is not None and r["status"] == "picked"
                            and now() > r["due_at"])
        out.append(d)
    return out


def match_station(conn, station_id: str, model_id: str) -> dict[str, Any]:
    """工位匹配：返回该工位生产该机型时可适配的夹具。"""
    station = _row(conn, "SELECT * FROM stations WHERE id=?", (station_id,))
    model = _row(conn, "SELECT * FROM models WHERE id=?", (model_id,))
    station_ok = bool(conn.execute(
        "SELECT 1 FROM station_models WHERE station_id=? AND model_id=?",
        (station_id, model_id)).fetchone())
    rows = conn.execute(
        """SELECT f.* FROM fixtures f
           JOIN fixture_models fm ON fm.fixture_id=f.id
          WHERE fm.model_id=? AND f.status!='scrapped'
          ORDER BY f.line_id, f.id""", (model_id,)).fetchall()
    fixtures = []
    for r in rows:
        d = _fixture_view(conn, r)
        d["fits_station"] = station_ok and r["line_id"] == station["line_id"]
        d["at_station"] = r["station_id"] == station_id
        d["frozen"] = bool(r["frozen"])
        fixtures.append(d)
    return {
        "station": dict(station), "model": dict(model),
        "station_supports_model": station_ok,
        "fixtures": fixtures,
    }


def fixture_stations(conn, fixture_id: str) -> list[dict]:
    """该夹具可服务的工位（按适配机型反查工位）。"""
    rows = conn.execute(
        """SELECT DISTINCT s.*, m.id model_id, m.name model_name
             FROM fixture_models fm
             JOIN station_models sm ON sm.model_id=fm.model_id
             JOIN stations s ON s.id=sm.station_id
             JOIN models m ON m.id=fm.model_id
            WHERE fm.fixture_id=? ORDER BY s.id, m.id""",
        (fixture_id,)).fetchall()
    return [dict(r) for r in rows]


def maintenance_list(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT mp.*, f.name fixture_name, f.use_count, f.life_threshold,
                  f.maintenance_every, f.status fixture_status
             FROM maintenance_plans mp JOIN fixtures f ON f.id=mp.fixture_id
            WHERE mp.done=0 ORDER BY mp.due_count""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pct"] = round(min(100, r["use_count"] * 100.0 / r["due_count"]), 1)
        d["due"] = r["use_count"] >= r["due_count"]
        out.append(d)
    return out


def scrap_list(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT d.*, f.name fixture_name, f.spec, f.use_count,
                  f.life_threshold, f.status fixture_status, f.frozen,
                  st.name raiser_name
             FROM disposal_orders d
             JOIN fixtures f ON f.id=d.fixture_id
             JOIN staff st ON st.id=d.raised_by
            WHERE d.type IN ('over_life','deformation','pin_missing','scrap')
            ORDER BY CASE d.status WHEN 'open' THEN 0 ELSE 1 END, d.created_at DESC"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        insp = conn.execute(
            """SELECT i.*, s.name inspector_name FROM inspections i
                 JOIN staff s ON s.id=i.inspector_id
                WHERE i.fixture_id=? ORDER BY i.created_at DESC LIMIT 1""",
            (r["fixture_id"],)).fetchone()
        d["latest_inspection"] = dict(insp) if insp else None
        out.append(d)
    return out


def disposal_list(conn, status: str | None = None) -> list[dict]:
    sql = """SELECT d.*, f.name fixture_name, s.name raiser_name
               FROM disposal_orders d
               JOIN fixtures f ON f.id=d.fixture_id
               JOIN staff s ON s.id=d.raised_by"""
    args: list[Any] = []
    if status:
        sql += " WHERE d.status=?"
        args.append(status)
    sql += " ORDER BY d.created_at DESC"
    return [dict(r) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- requests

def create_request(conn, fixture_id: str, station_id: str, model_id: str,
                   applicant_id: str, purpose: str = "", hours: int = 24) -> str:
    with db_lock(), conn:
        fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (fixture_id,))
        st = _row(conn, "SELECT * FROM stations WHERE id=?", (station_id,))
        applicant = _row(conn, "SELECT * FROM staff WHERE id=?", (applicant_id,))
        _row(conn, "SELECT 1 FROM models WHERE id=?", (model_id,))

        if fx["status"] == "scrapped":
            raise ServiceError("夹具已报废，无法申请")
        if fx["frozen"]:
            raise ServiceError("夹具因点检不合格已冻结，维修复核前不可申请")
        if fx["status"] == "in_repair":
            raise ServiceError("维修中的夹具不能被预订")
        if fx["status"] in ("reserved", "in_use"):
            raise ConflictError("夹具已被预订或领用中")
        if fx["use_count"] >= fx["life_threshold"]:
            raise ServiceError("夹具已达寿命阈值，等待报废判定")

        # 机型必须同时被工位与夹具支持
        if not conn.execute(
                "SELECT 1 FROM station_models WHERE station_id=? AND model_id=?",
                (station_id, model_id)).fetchone():
            raise ServiceError(f"工位 {st['name']} 不生产机型 {model_id}，匹配失败")
        if not conn.execute(
                "SELECT 1 FROM fixture_models WHERE fixture_id=? AND model_id=?",
                (fixture_id, model_id)).fetchone():
            raise ServiceError(f"夹具 {fixture_id} 不适配机型 {model_id}")

        # 操作员只能在本产线工位申请
        if applicant["role"] == "operator" and applicant["home_line"] != st["line_id"]:
            raise PermissionError_("跨线申请被拒绝：操作员仅可申请本产线工位")

        rid = f"RQ-{time.strftime('%Y%m%d')}-{_seq(conn, 'requests'):03d}"
        conn.execute(
            """INSERT INTO requests(id,fixture_id,station_id,model_id,applicant_id,
                   purpose,status,created_at,due_at)
               VALUES (?,?,?,?,?,?, 'pending', ?, ?)""",
            (rid, fixture_id, station_id, model_id, applicant_id, purpose,
             now(), now() + hours * 3600))
        return rid


def approve_request(conn, request_id: str, approver_id: str) -> None:
    with db_lock(), conn:
        r = _row(conn, "SELECT * FROM requests WHERE id=?", (request_id,))
        approver = _row(conn, "SELECT * FROM staff WHERE id=?", (approver_id,))
        if r["status"] != "pending":
            raise ConflictError("仅待审批申请可批准")
        if approver["role"] not in ("supervisor", "maintainer"):
            raise PermissionError_("仅工段长/维修班可审批申请")
        fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (r["fixture_id"],))
        if fx["status"] != "available" or fx["frozen"]:
            raise ConflictError("夹具当前不可预订（占用/维修/冻结）")
        conn.execute(
            "UPDATE requests SET status='approved', approved_at=? WHERE id=?",
            (now(), request_id))
        conn.execute("UPDATE fixtures SET status='reserved' WHERE id=?",
                     (r["fixture_id"],))


def reject_request(conn, request_id: str, approver_id: str, note: str = "") -> None:
    with db_lock(), conn:
        r = _row(conn, "SELECT * FROM requests WHERE id=?", (request_id,))
        if r["status"] != "pending":
            raise ConflictError("仅待审批申请可驳回")
        conn.execute(
            "UPDATE requests SET status='rejected', note=? WHERE id=?",
            (note, request_id))


def pickup(conn, request_id: str, operator_id: str, token: str | None = None) -> dict:
    """扫码领用。两名操作员同时领取同一夹具时仅一人成功。"""
    with db_lock(), conn:
        r = _row(conn, "SELECT * FROM requests WHERE id=?", (request_id,))
        op = _row(conn, "SELECT * FROM staff WHERE id=?", (operator_id,))
        if r["status"] != "approved":
            raise ConflictError(f"申请当前状态为 {r['status']}，不可领用")
        if op["role"] == "operator" and op["id"] != r["applicant_id"]:
            raise PermissionError_("仅申请人本人可领用")

        # 原子条件更新：仅当夹具仍 available 且未冻结时成功
        cur = conn.execute(
            """UPDATE fixtures SET status='in_use'
                WHERE id=? AND status='reserved' AND frozen=0""",
            (r["fixture_id"],))
        if cur.rowcount == 0:
            raise ConflictError("领用失败：夹具已被他人领取或状态异常")

        conn.execute(
            """UPDATE requests SET status='picked', picked_at=?, lock_owner=?
                WHERE id=? AND status='approved'""",
            (now(), token or f"LOCK-{request_id}", request_id))

        # 寿命累计：每次领用记一次使用
        conn.execute(
            "UPDATE fixtures SET use_count=use_count+1 WHERE id=?",
            (r["fixture_id"],))
        fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (r["fixture_id"],))
        disposal = None
        if fx["use_count"] >= fx["life_threshold"]:
            disposal = _raise_disposal(
                conn, "over_life", fx["id"], operator_id, request_id,
                f"累计 {fx['use_count']} 次 ≥ 寿命阈值 {fx['life_threshold']} 次")
        return {"request_id": request_id, "fixture_id": fx["id"],
                "use_count": fx["use_count"], "over_life_disposal": disposal}


def return_fixture(conn, request_id: str, operator_id: str) -> None:
    """归还：进入待点检状态，点检通过后才回到可用。"""
    with db_lock(), conn:
        r = _row(conn, "SELECT * FROM requests WHERE id=?", (request_id,))
        if r["status"] != "picked":
            raise ConflictError("仅已领用申请可归还")
        conn.execute(
            "UPDATE requests SET status='returned', returned_at=? WHERE id=?",
            (now(), request_id))
        # 夹具保持 in_use（占位）直到点检完成
        overdue = r["due_at"] is not None and now() > r["due_at"]
        if overdue:
            _raise_disposal(
                conn, "overdue", r["fixture_id"], operator_id, request_id,
                f"超过应归还时间 {r['due_at']}")


def inspect(conn, request_id: str, inspector_id: str, result: str,
            findings: str = "", photo_path: str = "") -> dict:
    """归还点检：pass 解锁回可用；fail 冻结并生成变形/缺销处置单。"""
    with db_lock(), conn:
        r = _row(conn, "SELECT * FROM requests WHERE id=?", (request_id,))
        if r["status"] != "returned":
            raise ConflictError("仅已归还申请可点检")
        inspector = _row(conn, "SELECT * FROM staff WHERE id=?", (inspector_id,))
        if inspector["role"] not in ("maintainer", "supervisor"):
            raise PermissionError_("点检须由维修班/工段长执行")
        if result not in ("pass", "fail"):
            raise ServiceError("点检结果必须为 pass/fail")

        iid = _new_id(conn, "IN", "inspections")
        conn.execute(
            """INSERT INTO inspections(id,fixture_id,request_id,inspector_id,
                   result,findings,photo_path,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (iid, r["fixture_id"], request_id, inspector_id, result,
             findings, photo_path, now()))

        disposal_ids: list[str] = []
        fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (r["fixture_id"],))

        if result == "pass":
            if fx["use_count"] >= fx["life_threshold"]:
                # 点检合格但已超寿命：维持占用，等待报废判定
                conn.execute("UPDATE fixtures SET status='reserved' WHERE id=?",
                             (fx["id"],))
            else:
                conn.execute(
                    "UPDATE fixtures SET status='available' WHERE id=?",
                    (fx["id"],))
            conn.execute(
                "UPDATE requests SET status='inspected', inspected_at=?, inspector_id=? WHERE id=?",
                (now(), inspector_id, request_id))
        else:
            # 不合格：冻结后续申请，进入维修
            conn.execute(
                """UPDATE fixtures SET frozen=1, status='in_repair' WHERE id=?""",
                (fx["id"],))
            conn.execute(
                "UPDATE requests SET status='inspected', inspected_at=?, inspector_id=?, note=? WHERE id=?",
                (now(), inspector_id, "点检不合格：" + findings, request_id))
            for kw, dtype, label in FINDING_KINDS:
                if kw in findings:
                    disposal_ids.append(_raise_disposal(
                        conn, dtype, fx["id"], inspector_id, iid,
                        f"点检发现{label}：{findings}"))
        return {"inspection_id": iid, "disposal_ids": disposal_ids}


# ---------------------------------------------------------------- repairs

def start_repair(conn, fixture_id: str, raised_by: str, repairer_id: str,
                 reason: str = "") -> str:
    with db_lock(), conn:
        fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (fixture_id,))
        repairer = _row(conn, "SELECT * FROM staff WHERE id=?", (repairer_id,))
        if repairer["role"] != "maintainer":
            raise PermissionError_("维修须由维修班执行")
        if fx["status"] == "scrapped":
            raise ServiceError("夹具已报废")
        busy = conn.execute(
            "SELECT id FROM repairs WHERE fixture_id=? AND status IN ('open','review')",
            (fixture_id,)).fetchone()
        if busy:
            raise ConflictError(f"夹具已有进行中的维修单 {busy['id']}")
        rid = _new_id(conn, "RP", "repairs")
        conn.execute(
            """INSERT INTO repairs(id,fixture_id,request_repair_by,repairer_id,
                   reason,status,created_at) VALUES (?,?,?,?,?, 'open', ?)""",
            (rid, fixture_id, raised_by, repairer_id, reason, now()))
        conn.execute(
            "UPDATE fixtures SET status='in_repair', frozen=1 WHERE id=?",
            (fixture_id,))
        return rid


def finish_repair(conn, repair_id: str, repairer_id: str) -> None:
    with db_lock(), conn:
        r = _row(conn, "SELECT * FROM repairs WHERE id=?", (repair_id,))
        if r["status"] != "open":
            raise ConflictError("仅维修中工单可提交完成")
        if r["repairer_id"] != repairer_id:
            raise PermissionError_("仅承修人本人可提交完成")
        conn.execute(
            "UPDATE repairs SET status='review', finished_at=? WHERE id=?",
            (now(), repair_id))


def review_repair(conn, repair_id: str, reviewer_id: str,
                  approved: bool, note: str = "") -> None:
    """维修复核必须由另一名员工执行（且为工段长/维修班）。"""
    with db_lock(), conn:
        r = _row(conn, "SELECT * FROM repairs WHERE id=?", (repair_id,))
        reviewer = _row(conn, "SELECT * FROM staff WHERE id=?", (reviewer_id,))
        if r["status"] != "review":
            raise ConflictError("工单不在待复核状态")
        if reviewer["id"] == r["repairer_id"]:
            raise PermissionError_("复核必须由承修人之外的另一名员工执行")
        if reviewer["role"] not in ("supervisor", "maintainer"):
            raise PermissionError_("复核须由工段长或另一名维修班成员执行")

        if approved:
            conn.execute(
                """UPDATE repairs SET status='done', reviewer_id=?, reviewed_at=?,
                       review_note=? WHERE id=?""",
                (reviewer_id, now(), note, repair_id))
            fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (r["fixture_id"],))
            if fx["use_count"] >= fx["life_threshold"]:
                conn.execute("UPDATE fixtures SET frozen=0, status='reserved' WHERE id=?",
                             (fx["id"],))
                _raise_disposal(conn, "over_life", fx["id"], reviewer_id, repair_id,
                                "维修复核合格但已超寿命，待报废判定")
            else:
                conn.execute(
                    "UPDATE fixtures SET frozen=0, status='available' WHERE id=?",
                    (fx["id"],))
            # 维修视同一次保养：寿命计数不变，安排下一轮保养计划
            _roll_maintenance(conn, fx["id"], reviewer_id)
        else:
            conn.execute(
                """UPDATE repairs SET status='open', review_note=? WHERE id=?""",
                ("复核驳回：" + note, repair_id))


def _roll_maintenance(conn, fixture_id: str, staff_id: str) -> None:
    fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (fixture_id,))
    conn.execute("UPDATE maintenance_plans SET done=1, done_at=? WHERE fixture_id=? AND done=0",
                 (now(), fixture_id))
    conn.execute(
        """INSERT INTO maintenance_plans(id,fixture_id,due_count,created_at)
           VALUES (?,?,?,?)""",
        (_new_id(conn, "MP", "maintenance_plans"), fixture_id,
         fx["use_count"] + fx["maintenance_every"], now()))
    conn.execute("UPDATE fixtures SET last_maintenance_at=? WHERE id=?",
                 (now(), fixture_id))


def complete_maintenance(conn, plan_id: str, staff_id: str) -> None:
    with db_lock(), conn:
        staff = _row(conn, "SELECT * FROM staff WHERE id=?", (staff_id,))
        if staff["role"] not in ("maintainer", "supervisor"):
            raise PermissionError_("保养须由维修班/工段长登记")
        p = _row(conn, "SELECT * FROM maintenance_plans WHERE id=?", (plan_id,))
        if p["done"]:
            raise ConflictError("保养计划已完成")
        conn.execute(
            "UPDATE maintenance_plans SET done=1, done_at=? WHERE id=?",
            (now(), plan_id))
        _roll_maintenance(conn, p["fixture_id"], staff_id)


# ---------------------------------------------------------------- transfers

def request_transfer(conn, fixture_id: str, to_station_id: str,
                     raised_by: str, note: str = "") -> str:
    """跨线调拨申请。操作员无权发起；维修班可申请、目标线工段长审批。"""
    with db_lock(), conn:
        fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (fixture_id,))
        to_st = _row(conn, "SELECT * FROM stations WHERE id=?", (to_station_id,))
        raiser = _row(conn, "SELECT * FROM staff WHERE id=?", (raised_by,))
        if fx["line_id"] == to_st["line_id"]:
            raise ServiceError("目标工位与夹具同线，不构成跨线调拨")
        if raiser["role"] == "operator":
            raise PermissionError_("跨线调拨需维修班申请、工段长审批，操作员无权发起")
        if fx["status"] in ("in_repair", "scrapped"):
            raise ConflictError("维修中/已报废夹具不可调拨")
        if fx["status"] in ("reserved", "in_use"):
            raise ConflictError("夹具被预订或领用中，不可调拨")
        tid = _new_id(conn, "TR", "transfers")
        conn.execute(
            """INSERT INTO transfers(id,fixture_id,from_station,to_station,to_line,
                   raised_by,status,created_at,note)
               VALUES (?,?,?,?,?,?,'pending',?,?)""",
            (tid, fixture_id, fx["station_id"], to_station_id, to_st["line_id"],
             raised_by, now(), note))
        return tid


def approve_transfer(conn, transfer_id: str, approver_id: str) -> str:
    with db_lock(), conn:
        t = _row(conn, "SELECT * FROM transfers WHERE id=?", (transfer_id,))
        approver = _row(conn, "SELECT * FROM staff WHERE id=?", (approver_id,))
        if t["status"] != "pending":
            raise ConflictError("调拨单不在待审批状态")
        if approver["role"] != "supervisor":
            raise PermissionError_("跨线调拨须由工段长审批")
        if approver["home_line"] != t["to_line"]:
            raise PermissionError_(
                f"跨线权限不足：须由调入线 {t['to_line']} 工段长审批")
        fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (t["fixture_id"],))
        if fx["status"] not in ("available",):
            raise ConflictError("夹具已被占用，调拨终止")
        conn.execute(
            """UPDATE transfers SET status='approved', approver_id=?, decided_at=?
               WHERE id=?""", (approver_id, now(), transfer_id))
        conn.execute(
            "UPDATE fixtures SET station_id=?, line_id=? WHERE id=?",
            (t["to_station"], t["to_line"], t["fixture_id"]))
        did = _raise_disposal(
            conn, "transfer", t["fixture_id"], approver_id, transfer_id,
            f"{t['from_station']} → {t['to_station']}（跨线）")
        return did


def reject_transfer(conn, transfer_id: str, approver_id: str, note: str = "") -> None:
    with db_lock(), conn:
        t = _row(conn, "SELECT * FROM transfers WHERE id=?", (transfer_id,))
        approver = _row(conn, "SELECT * FROM staff WHERE id=?", (approver_id,))
        if approver["role"] != "supervisor":
            raise PermissionError_("跨线调拨须由工段长审批")
        conn.execute(
            """UPDATE transfers SET status='rejected', approver_id=?, decided_at=?,
                   note=? WHERE id=?""",
            (approver_id, now(), note, transfer_id))


def list_transfers(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT t.*, f.name fixture_name, s1.name from_name, s2.name to_name,
                  st.name raiser_name
             FROM transfers t
             JOIN fixtures f ON f.id=t.fixture_id
             LEFT JOIN stations s1 ON s1.id=t.from_station
             JOIN stations s2 ON s2.id=t.to_station
             JOIN staff st ON st.id=t.raised_by
             ORDER BY t.created_at DESC""").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- scrap

def decide_scrap(conn, disposal_id: str, decider_id: str,
                 approved: bool, note: str = "") -> None:
    with db_lock(), conn:
        d = _row(conn, "SELECT * FROM disposal_orders WHERE id=?", (disposal_id,))
        decider = _row(conn, "SELECT * FROM staff WHERE id=?", (decider_id,))
        if d["status"] != "open":
            raise ConflictError("处置单已判定")
        if decider["role"] != "supervisor":
            raise PermissionError_("报废审批须由工段长执行")
        if d["type"] not in ("over_life", "deformation", "pin_missing", "scrap"):
            raise ServiceError("该处置单不属于报废判定")
        conn.execute(
            """UPDATE disposal_orders SET status=?, decider_id=?, decided_at=?,
                   detail=detail||? WHERE id=?""",
            ("approved" if approved else "rejected", decider_id, now(),
             f"｜审批意见：{note}", disposal_id))
        if approved:
            conn.execute(
                "UPDATE fixtures SET status='scrapped', frozen=1 WHERE id=?",
                (d["fixture_id"],))
            # 终止未完成申请
            conn.execute(
                """UPDATE requests SET status='rejected',
                       note=note||' 夹具已报废'
                 WHERE fixture_id=? AND status IN ('pending','approved')""",
                (d["fixture_id"],))
        else:
            # 驳回 = 评估后继续使用（如延寿/已修复）；解冻并恢复可用
            fx = _row(conn, "SELECT * FROM fixtures WHERE id=?", (d["fixture_id"],))
            if fx["status"] not in ("in_use", "scrapped") and not _has_open_blocker(conn, fx["id"]):
                conn.execute(
                    "UPDATE fixtures SET frozen=0, status='available' WHERE id=?",
                    (fx["id"],))


def _has_open_blocker(conn, fixture_id: str) -> bool:
    return bool(conn.execute(
        """SELECT 1 FROM repairs WHERE fixture_id=? AND status IN ('open','review')""",
        (fixture_id,)).fetchone())


# ---------------------------------------------------------------- reminders

def _add_reminder(conn, kind: str, fixture_id: str, ref_id: str, msg: str) -> bool:
    dup = conn.execute(
        """SELECT 1 FROM reminders WHERE kind=? AND ref_id=? AND read=0""",
        (kind, ref_id)).fetchone()
    if dup:
        return False
    conn.execute(
        """INSERT INTO reminders(kind,fixture_id,ref_id,message,created_at)
           VALUES (?,?,?,?,?)""",
        (kind, fixture_id, ref_id, msg, now()))
    return True


def tick(conn) -> dict[str, Any]:
    """周期任务：保养到期提醒、归还逾期处置单。可安全重复执行。"""
    created_r, created_o = 0, 0
    with db_lock(), conn:
        for p in conn.execute(
                """SELECT mp.*, f.name FROM maintenance_plans mp
                     JOIN fixtures f ON f.id=mp.fixture_id
                    WHERE mp.done=0""").fetchall():
            fx = conn.execute("SELECT use_count,name FROM fixtures WHERE id=?",
                              (p["fixture_id"],)).fetchone()
            if fx["use_count"] >= p["due_count"]:
                if _add_reminder(conn, "maintenance_due", p["fixture_id"], p["id"],
                                 f"{fx['name']} 已到保养点（累计 {fx['use_count']} 次，"
                                 f"计划点 {p['due_count']} 次）"):
                    created_r += 1
        for r in conn.execute(
                """SELECT r.*, f.name fname FROM requests r
                     JOIN fixtures f ON f.id=r.fixture_id
                    WHERE r.status='picked' AND r.due_at < ?""", (now(),)).fetchall():
            if _add_reminder(conn, "overdue", r["fixture_id"], r["id"],
                             f"{r['fname']} 借用 {r['id']} 已逾期未归还"):
                created_o += 1
            _raise_disposal(conn, "overdue", r["fixture_id"], r["applicant_id"],
                            r["id"], "后台任务检出归还逾期")
    return {"maintenance_reminders": created_r, "overdue_reminders": created_o}


def list_reminders(conn, only_unread: bool = True) -> list[dict]:
    sql = "SELECT * FROM reminders"
    if only_unread:
        sql += " WHERE read=0"
    sql += " ORDER BY created_at DESC LIMIT 50"
    rows = conn.execute(sql).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        fx = conn.execute("SELECT name FROM fixtures WHERE id=?",
                          (r["fixture_id"],)).fetchone()
        d["fixture_name"] = fx["name"] if fx else ""
        out.append(d)
    return out


def mark_reminders_read(conn) -> None:
    with db_lock(), conn:
        conn.execute("UPDATE reminders SET read=1")


# ---------------------------------------------------------------- repairs list

def list_repairs(conn, status: str | None = None) -> list[dict]:
    sql = """SELECT rp.*, f.name fixture_name,
                    a.name repairer_name, b.name reviewer_name,
                    c.name requester_name
               FROM repairs rp
               JOIN fixtures f ON f.id=rp.fixture_id
               JOIN staff a ON a.id=rp.repairer_id
               LEFT JOIN staff b ON b.id=rp.reviewer_id
               JOIN staff c ON c.id=rp.request_repair_by"""
    args: list[Any] = []
    if status:
        sql += " WHERE rp.status=?"
        args.append(status)
    sql += " ORDER BY rp.created_at DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def list_inspections(conn, fixture_id: str | None = None) -> list[dict]:
    sql = """SELECT i.*, s.name inspector_name, f.name fixture_name
               FROM inspections i
               JOIN staff s ON s.id=i.inspector_id
               JOIN fixtures f ON f.id=i.fixture_id"""
    args: list[Any] = []
    if fixture_id:
        sql += " WHERE i.fixture_id=?"
        args.append(fixture_id)
    sql += " ORDER BY i.created_at DESC LIMIT 100"
    return [dict(r) for r in conn.execute(sql, args)]
