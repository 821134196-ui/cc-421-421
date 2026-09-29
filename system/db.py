"""SQLite schema + connection management for the fixture loan system."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 产线 / 工位 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS lines (
    id          TEXT PRIMARY KEY,          -- L1 / L2 ...
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stations (
    id          TEXT PRIMARY KEY,          -- ST-A01
    line_id     TEXT NOT NULL REFERENCES lines(id),
    name        TEXT NOT NULL
);

-- 机型 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS models (
    id          TEXT PRIMARY KEY,          -- MX-100
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_models (
    station_id  TEXT NOT NULL REFERENCES stations(id),
    model_id    TEXT NOT NULL REFERENCES models(id),
    PRIMARY KEY (station_id, model_id)
);

-- 员工 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staff (
    id          TEXT PRIMARY KEY,          -- EMP-001
    name        TEXT NOT NULL,
    home_line   TEXT NOT NULL REFERENCES lines(id),
    role        TEXT NOT NULL CHECK (role IN ('operator','maintainer','supervisor'))
);

-- 夹具台账 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fixtures (
    id               TEXT PRIMARY KEY,     -- FX-1001
    name             TEXT NOT NULL,
    spec             TEXT NOT NULL,        -- 规格
    station_id       TEXT REFERENCES stations(id),   -- 当前工位
    line_id          TEXT NOT NULL REFERENCES lines(id),
    use_count        INTEGER NOT NULL DEFAULT 0,     -- 累计使用次数
    life_threshold   INTEGER NOT NULL,                -- 寿命阈值
    maintenance_every INTEGER NOT NULL DEFAULT 500,   -- 保养周期(次)
    last_maintenance_at INTEGER,                     -- 上次保养时间戳
    status           TEXT NOT NULL DEFAULT 'available'
        CHECK (status IN ('available','reserved','in_use','in_repair','scrapped')),
    frozen           INTEGER NOT NULL DEFAULT 0,     -- 点检不合格冻结
    location_note    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS fixture_models (
    fixture_id  TEXT NOT NULL REFERENCES fixtures(id),
    model_id    TEXT NOT NULL REFERENCES models(id),
    PRIMARY KEY (fixture_id, model_id)
);

-- 借用申请 -------------------------------------------------------------
-- pending 待审批 / approved 已批准待领用 / picked 已领用 / returned 已归还待点检
-- inspected 点检完成(可用) / rejected 驳回 / expired 逾期
CREATE TABLE IF NOT EXISTS requests (
    id            TEXT PRIMARY KEY,        -- RQ-20260929-001
    fixture_id    TEXT NOT NULL REFERENCES fixtures(id),
    station_id    TEXT NOT NULL REFERENCES stations(id),
    model_id      TEXT NOT NULL REFERENCES models(id),
    applicant_id  TEXT NOT NULL REFERENCES staff(id),
    qty           INTEGER NOT NULL DEFAULT 1,
    purpose       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    INTEGER NOT NULL,
    approved_at   INTEGER,
    picked_at     INTEGER,
    due_at        INTEGER,                 -- 应归还时间
    returned_at   INTEGER,
    inspected_at  INTEGER,
    inspector_id  TEXT REFERENCES staff(id),
    lock_owner    TEXT,                    -- 领用并发令牌
    note          TEXT NOT NULL DEFAULT ''
);

-- 点检记录 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inspections (
    id          TEXT PRIMARY KEY,          -- IN-0001
    fixture_id  TEXT NOT NULL REFERENCES fixtures(id),
    request_id  TEXT REFERENCES requests(id),
    inspector_id TEXT NOT NULL REFERENCES staff(id),
    result      TEXT NOT NULL CHECK (result IN ('pass','fail')),
    findings    TEXT NOT NULL DEFAULT '',  -- 变形 / 定位销缺失 ...
    photo_path  TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

-- 维修记录 -------------------------------------------------------------
-- open 维修中 / review 待复核 / done 完成 / rejected 复核驳回
CREATE TABLE IF NOT EXISTS repairs (
    id            TEXT PRIMARY KEY,        -- RP-0001
    fixture_id    TEXT NOT NULL REFERENCES fixtures(id),
    request_repair_by TEXT NOT NULL REFERENCES staff(id),
    repairer_id   TEXT NOT NULL REFERENCES staff(id),
    reviewer_id   TEXT REFERENCES staff(id),
    reason        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',
    created_at    INTEGER NOT NULL,
    finished_at   INTEGER,
    reviewed_at   INTEGER,
    review_note   TEXT NOT NULL DEFAULT ''
);

-- 保养计划（每次保养生成下一次） ----------------------------------------
CREATE TABLE IF NOT EXISTS maintenance_plans (
    id          TEXT PRIMARY KEY,          -- MP-0001
    fixture_id  TEXT NOT NULL REFERENCES fixtures(id),
    due_count   INTEGER NOT NULL,          -- 达到该累计次数时到期
    done        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    done_at     INTEGER
);

-- 处置单 ---------------------------------------------------------------
-- deformation 夹具变形 / pin_missing 定位销缺失 / over_life 超寿命
-- transfer 跨线调拨 / overdue 归还逾期 / scrap 报废
CREATE TABLE IF NOT EXISTS disposal_orders (
    id          TEXT PRIMARY KEY,          -- DO-0001
    type        TEXT NOT NULL,
    fixture_id  TEXT NOT NULL REFERENCES fixtures(id),
    related_id  TEXT NOT NULL DEFAULT '',  -- 关联的点检/申请/维修/调拨
    raised_by   TEXT NOT NULL REFERENCES staff(id),
    status      TEXT NOT NULL DEFAULT 'open',  -- open/approved/rejected
    detail      TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    decided_at  INTEGER,
    decider_id  TEXT REFERENCES staff(id)
);

-- 跨线调拨申请 ----------------------------------------------------------
-- pending / approved / rejected
CREATE TABLE IF NOT EXISTS transfers (
    id           TEXT PRIMARY KEY,         -- TR-0001
    fixture_id   TEXT NOT NULL REFERENCES fixtures(id),
    from_station TEXT REFERENCES stations(id),
    to_station   TEXT NOT NULL REFERENCES stations(id),
    to_line      TEXT NOT NULL REFERENCES lines(id),
    raised_by    TEXT NOT NULL REFERENCES staff(id),
    approver_id  TEXT REFERENCES staff(id),
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   INTEGER NOT NULL,
    decided_at   INTEGER,
    note         TEXT NOT NULL DEFAULT ''
);

-- 保养/逾期提醒（后台任务写入） ----------------------------------------
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,             -- maintenance_due / overdue
    fixture_id  TEXT NOT NULL REFERENCES fixtures(id),
    ref_id      TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    read        INTEGER NOT NULL DEFAULT 0
);
"""

_lock = threading.RLock()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with _lock, conn:
        conn.executescript(SCHEMA)


def db_lock() -> threading.RLock:
    """Process-wide advisory lock used to serialize the pick-up race."""
    return _lock
