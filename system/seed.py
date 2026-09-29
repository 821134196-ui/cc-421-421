"""Fictional seed data: 鸿鹄精密一厂 — lines, stations, models, staff, fixtures."""
from __future__ import annotations

from .db import init_db

SEED = {
    "lines": [
        ("L1", "总装一线"),
        ("L2", "总装二线"),
        ("L3", "部装三线"),
    ],
    "stations": [
        # id, line, name
        ("ST-A01", "L1", "A01 发动机托架定位台"),
        ("ST-A02", "L1", "A02 前梁压紧工位"),
        ("ST-B01", "L2", "B01 后副车架合装台"),
        ("ST-B02", "L2", "B02 电池托盘定位台"),
        ("ST-C01", "L3", "C01 支架焊接工装位"),
    ],
    "models": [
        ("MX-100", "鸿鹄 H100 轿车"),
        ("MX-200", "鸿鹄 S200 SUV"),
        ("MX-300", "鸿鹄 E300 新能源车"),
    ],
    # 工位生产机型
    "station_models": [
        ("ST-A01", "MX-100"), ("ST-A01", "MX-200"),
        ("ST-A02", "MX-100"),
        ("ST-B01", "MX-200"), ("ST-B01", "MX-300"),
        ("ST-B02", "MX-300"),
        ("ST-C01", "MX-100"), ("ST-C01", "MX-300"),
    ],
    "staff": [
        # id, name, home_line, role
        ("EMP-001", "韩立诚", "L1", "operator"),
        ("EMP-002", "赵南星", "L1", "operator"),
        ("EMP-003", "钱书瑶", "L2", "operator"),
        ("EMP-004", "孙柏舟", "L2", "operator"),
        ("EMP-005", "冯远山", "L3", "operator"),
        ("EMP-011", "尉迟维修班·周工", "L1", "maintainer"),
        ("EMP-012", "维修班·吴工", "L2", "maintainer"),
        ("EMP-021", "工段长·郑主管", "L1", "supervisor"),
        ("EMP-022", "工段长·王主管", "L2", "supervisor"),
    ],
    # fixture: id, name, spec, station, line, use_count, threshold, maint_every, status
    "fixtures": [
        ("FX-1001", "发动机托架定位夹具", "2400×900 mm / 4 销定位 / 液压压紧",
         "ST-A01", "L1", 320, 2000, 500, "available"),
        ("FX-1002", "前梁压紧夹具", "1800×600 mm / 双偏心轮",
         "ST-A02", "L1", 610, 1500, 400, "available"),
        ("FX-1003", "前梁压紧夹具(备)", "1800×600 mm / 双偏心轮",
         "ST-A02", "L1", 1480, 1500, 400, "available"),   # 接近寿命
        ("FX-2001", "后副车架合装夹具", "2600×1100 mm / 6 销定位",
         "ST-B01", "L2", 905, 2500, 600, "available"),
        ("FX-2002", "电池托盘定位夹具", "2100×1400 mm / 真空吸附+挡销",
         "ST-B02", "L2", 420, 2000, 500, "available"),
        ("FX-2003", "电池托盘检具", "2050×1380 mm / 通止规 12 件",
         "ST-B02", "L2", 2530, 2500, 600, "available"),    # 已超寿命 → 报废样例
        ("FX-3001", "支架焊接工装", "900×700 mm / 快速夹钳×4",
         "ST-C01", "L3", 1760, 1800, 450, "available"),
        ("FX-3002", "小件通用夹具", "600×400 mm / 槽系组合",
         "ST-C01", "L3", 0, 1200, 300, "available"),
    ],
    # 夹具适配机型
    "fixture_models": [
        ("FX-1001", "MX-100"), ("FX-1001", "MX-200"),
        ("FX-1002", "MX-100"),
        ("FX-1003", "MX-100"),
        ("FX-2001", "MX-200"), ("FX-2001", "MX-300"),
        ("FX-2002", "MX-300"),
        ("FX-2003", "MX-300"),
        ("FX-3001", "MX-100"), ("FX-3001", "MX-300"),
        ("FX-3002", "MX-100"), ("FX-3002", "MX-200"), ("FX-3002", "MX-300"),
    ],
}


def seed_if_empty(conn) -> bool:
    """Populate fictional base data on first launch. Returns True if seeded."""
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM fixtures").fetchone()["c"]:
        return False
    with conn:
        conn.executemany("INSERT INTO lines(id,name) VALUES (?,?)", SEED["lines"])
        conn.executemany("INSERT INTO stations(id,line_id,name) VALUES (?,?,?)", SEED["stations"])
        conn.executemany("INSERT INTO models(id,name) VALUES (?,?)", SEED["models"])
        conn.executemany("INSERT INTO station_models(station_id,model_id) VALUES (?,?)",
                         SEED["station_models"])
        conn.executemany(
            "INSERT INTO staff(id,name,home_line,role) VALUES (?,?,?,?)", SEED["staff"])
        conn.executemany(
            """INSERT INTO fixtures(id,name,spec,station_id,line_id,use_count,
                   life_threshold,maintenance_every,status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            SEED["fixtures"])
        conn.executemany("INSERT INTO fixture_models(fixture_id,model_id) VALUES (?,?)",
                         SEED["fixture_models"])
        # 每件可用夹具建立首条保养计划
        for fx in conn.execute("SELECT id, use_count, maintenance_every FROM fixtures"):
            conn.execute(
                """INSERT INTO maintenance_plans(id,fixture_id,due_count,created_at)
                   VALUES ('MP-'||?, ?, ?, strftime('%s','now'))""",
                (fx["id"].split("-")[1], fx["id"],
                 fx["use_count"] + fx["maintenance_every"]))
    return True
