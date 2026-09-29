"""First-launch demo samples: 领用 / 维修 / 报废 / 逾期 全链路样例。

仅在 requests 表为空时执行，重复启动不会重复造数。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import service

SAMPLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">
<rect width="320" height="200" fill="#2b2f36"/>
<rect x="40" y="55" width="240" height="90" rx="8" fill="none"
 stroke="#ffd23f" stroke-width="3" stroke-dasharray="8 6"/>
<circle cx="100" cy="100" r="10" fill="#ffd23f"/>
<circle cx="220" cy="100" r="10" fill="none" stroke="#ffd23f" stroke-width="3"/>
<text x="160" y="35" fill="#f4f6f8" font-size="18" text-anchor="middle"
 font-family="sans-serif">点检照片样例</text>
<text x="160" y="175" fill="#9aa3ad" font-size="12" text-anchor="middle"
 font-family="sans-serif">2026-09-29 鸿鹄精密一厂 · 虚构影像</text>
</svg>"""


def bootstrap_samples(conn, photo_dir: Path) -> None:
    if conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]:
        return

    photo_dir.mkdir(parents=True, exist_ok=True)
    sample_photo = photo_dir / "IN-SAMPLE.svg"
    if not sample_photo.exists():
        sample_photo.write_text(SAMPLE_SVG, encoding="utf-8")

    t = int(time.time())

    # 1) 领用在途样例：FX-1001 已批准并扫码领用 --------------------------
    r1 = service.create_request(
        conn, "FX-1001", "ST-A01", "MX-100", "EMP-001",
        "白班 H100 批量生产，A01 工位领用")
    service.approve_request(conn, r1, "EMP-021")
    service.pickup(conn, r1, "EMP-001", token="SAMPLE-LOCK-1")

    # 2) 待审批样例：申请队列里有一条 pending -----------------------------
    service.create_request(
        conn, "FX-2001", "ST-B01", "MX-200", "EMP-003",
        "S200 试制批次合装使用")

    # 3) 归还逾期样例：FX-1002 已领用，手工把应归还时间调到过去 -----------
    r3 = service.create_request(
        conn, "FX-1002", "ST-A02", "MX-100", "EMP-002",
        "夜班加急，登记借用")
    service.approve_request(conn, r3, "EMP-021")
    service.pickup(conn, r3, "EMP-002", token="SAMPLE-LOCK-2")
    with conn:
        conn.execute("UPDATE requests SET due_at=? WHERE id=?",
                     (t - 2 * 86400, r3))
    service.tick(conn)   # 生成逾期提醒与处置单

    # 4) 维修样例：FX-3001 归还点检不合格 → 定位销缺失+变形 → 维修待复核 --
    r4 = service.create_request(
        conn, "FX-3001", "ST-C01", "MX-100", "EMP-005",
        "焊接支架定位使用，归还时操作工反映销位松动")
    service.approve_request(conn, r4, "EMP-012")
    service.pickup(conn, r4, "EMP-005", token="SAMPLE-LOCK-3")
    service.return_fixture(conn, r4, "EMP-005")
    service.inspect(
        conn, r4, "EMP-012", "fail",
        "定位销缺失 1 处，压紧基座目视有轻微变形",
        photo_path="IN-SAMPLE.svg")
    rp = service.start_repair(
        conn, "FX-3001", "EMP-012", "EMP-012",
        "补装定位销、校正基座")
    service.finish_repair(conn, rp, "EMP-012")   # 等待另一名员工复核

    # 5) 报废样例：FX-2003 累计 2530 次 ≥ 阈值 2500，超寿命处置单待审批 --
    with conn:
        iid = f"IN-{conn.execute('SELECT COUNT(*) c FROM inspections').fetchone()['c'] + 1:04d}"
        conn.execute(
            """INSERT INTO inspections(id,fixture_id,request_id,inspector_id,
                   result,findings,photo_path,created_at)
               VALUES (?,?,NULL,?,?, '累计使用超阈值，功能尚可但精度风险高', ?, ?)""",
            (iid, "FX-2003", "EMP-012", "pass", "IN-SAMPLE.svg", t - 3600))
        did = f"DO-{conn.execute('SELECT COUNT(*) c FROM disposal_orders').fetchone()['c'] + 1:04d}"
        conn.execute(
            """INSERT INTO disposal_orders(id,type,fixture_id,related_id,raised_by,
                   detail,created_at)
               VALUES (?, 'over_life', 'FX-2003', ?, 'EMP-021',
                       '累计 2530 次 ≥ 寿命阈值 2500 次，系统自动判定待报废审批', ?)""",
            (did, iid, t - 1800))
