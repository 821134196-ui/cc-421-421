"""虚构种子数据与样例生成。

工厂「海川重工·第三智造中心」全部为虚构数据：
产线 L-A 总装一线 / L-B 总装二线；工位、机型、人员、夹具均为编造编号。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .db import Database
from .service import (
    AVAILABLE, FixtureService, REPAIRING, LOCKED,
    DISP_DEFORMATION,
)

# ---------------------------------------------------------------- 主数据
LINES = [
    ("L-A", "总装一线"),
    ("L-B", "总装二线"),
]

STATIONS = [
    ("ST-A11", "前桥装配位", "L-A"),
    ("ST-A12", "发动机悬置位", "L-A"),
    ("ST-A13", "底盘合装位", "L-A"),
    ("ST-B21", "前桥装配位", "L-B"),
    ("ST-B22", "电池包装配位", "L-B"),
]

MODELS = [
    ("M-220", "海象 HX220 燃油版"),
    ("M-225", "海象 HX225 混动版"),
    ("M-E90", "星鲸 E90 纯电版"),
]

# 工位 ↔ 机型
STATION_MODELS = {
    "ST-A11": ["M-220", "M-225"],
    "ST-A12": ["M-220", "M-225"],
    "ST-A13": ["M-220", "M-225", "M-E90"],
    "ST-B21": ["M-E90"],
    "ST-B22": ["M-E90", "M-225"],
}

# 人员：工号 / 姓名 / 角色 / 所属产线
EMPLOYEES = [
    ("OP-101", "梁砺",   "operator",  "L-A"),
    ("OP-102", "祁小桐", "operator",  "L-A"),
    ("OP-201", "童一帆", "operator",  "L-B"),
    ("OP-202", "桑柔",   "operator",  "L-B"),
    ("MT-301", "赫连峰", "maintainer", None),
    ("MT-302", "南宫曜", "maintainer", None),
    ("QA-401", "闻知秋", "inspector",  None),
    ("QA-402", "苏映雪", "inspector",  None),
    ("MGR-001", "车间主任·霍青山", "manager", None),
]

# 夹具：编号/名称/规格/产线/工位/寿命阈值/保养周期天/已用次数/适配机型/末次保养距今天数
FIXTURES = [
    ("FZ-1001", "前桥定位焊夹具", "2400×900 液压压紧 / 定位销 4×φ20",
     "L-A", "ST-A11", 5000, 90, 480,
     ["M-220", "M-225"], 12),
    ("FZ-1002", "发动机悬置合装夹具", "1600×1200 双导柱 / 扭矩臂 2",
     "L-A", "ST-A12", 4000, 60, 3050,
     ["M-220", "M-225"], 58),
    ("FZ-1003", "底盘整体举升夹具", "3200×1800 四柱举升 / 防滑垫 8",
     "L-A", "ST-A13", 6000, 120, 5960,   # 已超寿命
     ["M-220", "M-225"], 100),
    ("FZ-1004", "混动总成托架夹具", "2000×1100 快换压板 / 绝缘衬套",
     "L-A", "ST-A12", 4500, 90, 900,
     ["M-225", "M-E90"], 20),
    ("FZ-2001", "电驱前桥定位夹具", "2300×880 电驱避让槽 / 销 4×φ18",
     "L-B", "ST-B21", 5000, 90, 2100,
     ["M-E90"], 5),
    ("FZ-2002", "电池包吊具", "1900×1400 真空吸盘×6 / 防摇摆",
     "L-B", "ST-B22", 3500, 60, 3420,    # 临近寿命
     ["M-E90"], 61),
    ("FZ-2003", "电池包压紧夹具", "1850×1350 伺服压紧×8",
     "L-B", "ST-B22", 4000, 90, 600,
     ["M-E90", "M-225"], 95),  # 保养到期
]


def seed(db: Database, svc: FixtureService) -> None:
    """写入全部主数据与夹具台账（幂等：已存在则跳过）。"""
    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) c FROM fixtures").fetchone()["c"]:
            return
        conn.execute("BEGIN IMMEDIATE")
        now = svc.now()
        line_ids = {}
        for code, name in LINES:
            cur = conn.execute("INSERT INTO lines(code, name) VALUES(?,?)",
                               (code, name))
            line_ids[code] = cur.lastrowid
        station_ids = {}
        for code, name, line in STATIONS:
            cur = conn.execute(
                "INSERT INTO stations(code, name, line_id) VALUES(?,?,?)",
                (code, name, line_ids[line]))
            station_ids[code] = cur.lastrowid
        model_ids = {}
        for code, name in MODELS:
            cur = conn.execute("INSERT INTO models(code, name) VALUES(?,?)",
                               (code, name))
            model_ids[code] = cur.lastrowid
        for scode, mcs in STATION_MODELS.items():
            for mc in mcs:
                conn.execute(
                    "INSERT OR IGNORE INTO station_models(station_id, model_id)"
                    " VALUES(?,?)", (station_ids[scode], model_ids[mc]))
        for code, name, role, line in EMPLOYEES:
            conn.execute(
                "INSERT INTO employees(code, name, role, line_id, frozen)"
                " VALUES(?,?,?,?,0)",
                (code, name, role, line_ids[line] if line else None))
        for (fcode, name, spec, line, station, threshold, cycle,
             used, models, maint_age) in FIXTURES:
            last_maint = (now - timedelta(days=maint_age)).isoformat(timespec="seconds")
            cur = conn.execute(
                "INSERT INTO fixtures(code, name, spec, line_id, station_id,"
                " use_count, life_threshold, status, maintenance_cycle_days,"
                " last_maintenance_at, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (fcode, name, spec, line_ids[line], station_ids[station],
                 used, threshold, AVAILABLE, cycle, last_maint,
                 (now - timedelta(days=200)).isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds")))
            fid = cur.lastrowid
            for mc in models:
                conn.execute(
                    "INSERT INTO fixture_models(fixture_id, model_id) VALUES(?,?)",
                    (fid, model_ids[mc]))
        conn.commit()
    finally:
        conn.close()


def generate_samples(db: Database, svc: FixtureService,
                     base: datetime | None = None) -> dict:
    """生成领用、维修、报废样例（单条命令调用）。

    - 领用样例：OP-101 正常申请并领用 FZ-1001（寿命正常累计）
    - 竞争样例：OP-101 / 祁小桐 抢同一把 FZ-1002，只一人成功
    - 报废样例：超寿命的 FZ-1003 直接走超寿命处置 + 待报废审批
    - 维修样例：FZ-2001 归还点检不合格（定位销缺失）→ 维修 → 他人复核
    """
    seed(db, svc)
    summary: dict = {"checkout": None, "race": {}, "repair": {}, "scrap": None}
    conn = db.connect()
    ids = {r["code"]: r["id"] for r in conn.execute(
        "SELECT id, code FROM stations")}
    mids = {r["code"]: r["id"] for r in conn.execute(
        "SELECT id, code FROM models")}
    fids = {r["code"]: r["id"] for r in conn.execute(
        "SELECT id, code FROM fixtures")}
    conn.close()

    # 1) 正常领用：申请 → 领用 → 归还 → 点检合格（寿命 +25）
    r1 = svc.apply("OP-101", ids["ST-A11"], mids["M-220"],
                   note="样例：前桥装配领用")
    c1 = svc.checkout(r1["id"], fids["FZ-1001"], "OP-101")
    b1 = svc.active_borrows()
    borrow = next(b for b in b1 if b["request_code"] == r1["code"])
    svc.return_fixture(borrow["id"], use_delta=25)
    i1 = svc.inspect(r1["id"], "QA-401", "pass", note="样例：点检合格")
    summary["checkout"] = {
        "request": r1["code"], "fixture": "FZ-1001",
        "use_count_after": i1["use_count"]}

    # 2) 领用竞争：两人抢 FZ-1002
    ra = svc.apply("OP-101", ids["ST-A12"], mids["M-220"], note="样例：竞争 A")
    rb = svc.apply("OP-102", ids["ST-A12"], mids["M-220"], note="样例：竞争 B")
    ca = svc.checkout(ra["id"], fids["FZ-1002"], "OP-101")
    summary["race"]["winner"] = ca["operator"]
    try:
        svc.checkout(rb["id"], fids["FZ-1002"], "OP-102")
        summary["race"]["loser_error"] = None
    except Exception as exc:  # 预期失败
        summary["race"]["loser"] = "祁小桐"
        summary["race"]["loser_error"] = str(exc)

    # 3) 维修样例：FZ-2001（L-B 电驱前桥）OP-201 借用 → 归还点检不合格
    r3 = svc.apply("OP-201", ids["ST-B21"], mids["M-E90"],
                   note="样例：疑似定位销缺失")
    c3 = svc.checkout(r3["id"], fids["FZ-2001"], "OP-201")
    b3 = next(b for b in svc.active_borrows()
              if b["request_code"] == r3["code"])
    svc.return_fixture(b3["id"], use_delta=10)
    insp = svc.inspect(r3["id"], "QA-401", "fail",
                       defect="missing_pin", note="样例：2 号定位销缺失")
    summary["repair"]["frozen"] = "童一帆"
    repair_id = insp["repair"]["id"]
    try:  # 维修工不能自复核
        svc.repair_done(repair_id, "MT-301", "补装定位销并打表校验")
        svc.repair_review(repair_id, "MT-301", True)
        summary["repair"]["self_review_error"] = "未拦截"
    except Exception as exc:
        summary["repair"]["self_review_error"] = str(exc)
    # 另一人复核通过 → 夹具回可用、借用人解冻
    rev = svc.repair_review(repair_id, "QA-402", True, note="样例：复核合格")
    summary["repair"]["reviewer"] = "苏映雪"
    summary["repair"]["status"] = rev["status"]

    # 4) 报废样例：FZ-1003 已超寿命，补一条合格点检触发超寿命处置
    r4 = svc.apply("OP-102", ids["ST-A13"], mids["M-220"],
                   note="样例：超寿命夹具末班次使用",
                   preferred_fixture_id=fids["FZ-1003"])
    c4 = svc.checkout(r4["id"], fids["FZ-1003"], "OP-102")
    b4 = next(b for b in svc.active_borrows()
              if b["request_code"] == r4["code"])
    svc.return_fixture(b4["id"], use_delta=40)
    i4 = svc.inspect(r4["id"], "QA-402", "pass", note="样例：末班次点检合格待报废审批")
    summary["scrap"] = {
        "fixture": "FZ-1003",
        "disposition": i4["disposition"]["code"],
        "use_count": i4["use_count"]}

    # 保养提醒与逾期扫描
    svc.generate_maintenance_plans()
    svc.scan_overdue()
    return summary
