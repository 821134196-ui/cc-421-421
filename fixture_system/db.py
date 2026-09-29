"""SQLite 连接与建表脚本。

线程模型：每个业务方法通过 :meth:`Database.connect` 获取独立短连接，
WAL 模式 + ``BEGIN IMMEDIATE`` 保证并发领用（两人同时扫码）时只有一人成功。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS lines (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,      -- 产线编号，如 L-A
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stations (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,      -- 工位编号，如 ST-A11
    name        TEXT NOT NULL,
    line_id     INTEGER NOT NULL REFERENCES lines(id)
);

CREATE TABLE IF NOT EXISTS models (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,      -- 机型编号，如 M-220
    name        TEXT NOT NULL
);

-- 工位允许作业的机型：申请时必须同时匹配工位与机型
CREATE TABLE IF NOT EXISTS station_models (
    station_id  INTEGER NOT NULL REFERENCES stations(id),
    model_id    INTEGER NOT NULL REFERENCES models(id),
    PRIMARY KEY (station_id, model_id)
);

CREATE TABLE IF NOT EXISTS fixtures (
    id                      INTEGER PRIMARY KEY,
    code                    TEXT UNIQUE NOT NULL,   -- 夹具编号 FZ-xxxx
    name                    TEXT NOT NULL,
    spec                    TEXT NOT NULL,          -- 规格
    line_id                 INTEGER NOT NULL REFERENCES lines(id),  -- 所属产线
    station_id              INTEGER REFERENCES stations(id),        -- 当前工位
    use_count               INTEGER NOT NULL DEFAULT 0,             -- 累计使用次数
    life_threshold          INTEGER NOT NULL,                        -- 寿命阈值
    status                  TEXT NOT NULL DEFAULT 'available',
    maintenance_cycle_days  INTEGER NOT NULL DEFAULT 90,             -- 保养周期
    last_maintenance_at     TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
-- 条件更新的关键索引：领用抢锁走 status 过滤
CREATE INDEX IF NOT EXISTS idx_fixtures_status ON fixtures(status);

CREATE TABLE IF NOT EXISTS fixture_models (
    fixture_id  INTEGER NOT NULL REFERENCES fixtures(id),
    model_id    INTEGER NOT NULL REFERENCES models(id),
    PRIMARY KEY (fixture_id, model_id)
);

CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,      -- 工号 OP-101 / MT-301 / QA-401 / MGR-001
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,             -- operator 操作员 / maintainer 维修工 / inspector 点检员 / manager 主任
    line_id     INTEGER REFERENCES lines(id),  -- 所属产线（跨线权限判定用）
    frozen      INTEGER NOT NULL DEFAULT 0     -- 点检不合格后冻结申请
);

-- 借用申请：pending 排队 → locked 已领用 → inspecting 待点检 → closed / failed
CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY,
    code         TEXT UNIQUE NOT NULL,     -- 申请单号 BR-yyyymmdd-xxxx
    station_id   INTEGER NOT NULL REFERENCES stations(id),
    model_id     INTEGER NOT NULL REFERENCES models(id),
    applicant_id INTEGER NOT NULL REFERENCES employees(id),
    fixture_id   INTEGER REFERENCES fixtures(id),
    status       TEXT NOT NULL DEFAULT 'pending',
    note         TEXT DEFAULT '',
    due_at       TEXT,                     -- 约定归还时间（逾期判定）
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);

-- 借用记录（一次领用一行）
CREATE TABLE IF NOT EXISTS borrows (
    id                INTEGER PRIMARY KEY,
    request_id        INTEGER NOT NULL REFERENCES requests(id),
    fixture_id        INTEGER NOT NULL REFERENCES fixtures(id),
    borrower_id       INTEGER NOT NULL REFERENCES employees(id),
    use_delta         INTEGER NOT NULL DEFAULT 1,  -- 本次累计的使用次数
    checked_out_at    TEXT NOT NULL,
    due_at            TEXT NOT NULL,
    returned_at       TEXT,
    overdue_disp_id   INTEGER                     -- 逾期后生成的处置单
);

-- 点检结果（归还后必须先点检）
CREATE TABLE IF NOT EXISTS inspections (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    fixture_id  INTEGER NOT NULL REFERENCES fixtures(id),
    request_id  INTEGER REFERENCES requests(id),
    inspector_id INTEGER NOT NULL REFERENCES employees(id),
    result      TEXT NOT NULL,             -- pass / fail
    defect      TEXT,                      -- deformation 变形 / missing_pin 定位销缺失
    note        TEXT DEFAULT '',
    photo_path  TEXT,                      -- 本地目录中的点检照片
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspections_fixture ON inspections(fixture_id);

-- 维修记录：open → in_review（维修完成待复核）→ done / reopen
CREATE TABLE IF NOT EXISTS repairs (
    id                   INTEGER PRIMARY KEY,
    code                 TEXT UNIQUE NOT NULL,
    fixture_id           INTEGER NOT NULL REFERENCES fixtures(id),
    inspection_id        INTEGER REFERENCES inspections(id),
    description          TEXT NOT NULL DEFAULT '',
    repairman_id         INTEGER REFERENCES employees(id),
    repaired_at          TEXT,
    reviewer_id          INTEGER REFERENCES employees(id),
    reviewed_at          TEXT,
    review_result        TEXT,             -- pass / fail（打回重修）
    status               TEXT NOT NULL DEFAULT 'open',
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repairs_status ON repairs(status);

-- 处置单：deformation 变形 / missing_pin 定位销缺失 / over_life 超寿命
--        / transfer 跨线调拨 / overdue 归还逾期
CREATE TABLE IF NOT EXISTS dispositions (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    fixture_id  INTEGER NOT NULL REFERENCES fixtures(id),
    type        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',  -- open / done / scrapped / rejected
    detail      TEXT DEFAULT '',
    ref         TEXT DEFAULT '',              -- 关联单号
    target_station_id INTEGER REFERENCES stations(id), -- 跨线调拨目标工位
    created_by  INTEGER REFERENCES employees(id),
    decided_by  INTEGER REFERENCES employees(id),
    decided_at  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disp_status ON dispositions(status, type);

-- 保养计划（周期到期自动生成）
CREATE TABLE IF NOT EXISTS maintenance_plans (
    id          INTEGER PRIMARY KEY,
    fixture_id  INTEGER NOT NULL REFERENCES fixtures(id),
    due_at      TEXT NOT NULL,
    done_at     TEXT,
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

-- 进程内任务产生的提醒
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,             -- maintenance_due / overdue / over_life
    ref_table   TEXT NOT NULL,
    ref_id      INTEGER NOT NULL,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(ref_table, ref_id, type)
);
"""


class Database:
    """管理 SQLite 数据库文件与目录。"""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
