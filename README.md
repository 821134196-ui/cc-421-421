# 工厂工装夹具借用与寿命记录系统

面向「海川重工·第三智造中心」（**全部工厂、人员、设备编号均为虚构**）的工装夹具全流程管理：
夹具台账、工位/机型适配、借用申请、扫码借还、归还点检、维修复核、
保养提醒、报废审批与跨线调拨。

- **零第三方依赖**：Python 3.10+ 标准库（`http.server` + `sqlite3`）+ 原生 HTML/CSS/JS
- **持久化**：SQLite（WAL 模式）保存夹具寿命与申请
- **点检照片**：multipart 上传，落盘到本地 `data/photos/`
- **进程内任务**：守护线程每 30 秒扫描保养到期与归还逾期，生成提醒/处置单
- **并发安全**：领用时条件更新 `WHERE status='available'`，两人同时扫码只有一人成功

## 一条命令本地启动

```bash
./run.sh
# 等价于：python3 -m fixture_system.main --reset --samples
```

打开 http://127.0.0.1:8000 （可用 `PORT=8200 ./run.sh` 改端口）。
`--samples` 会自动生成领用、领用竞争、维修复核、报废审批四类样例数据。

其他用法：

```bash
python3 -m fixture_system.main                  # 仅主数据，不起样例
python3 -m fixture_system.main --make-samples   # 只生成样例后退出
python3 -m fixture_system.main --reset          # 删库重建
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

27 个用例，覆盖：寿命累计与超寿命判定、工位/机型适配校验、
**真实线程的领用竞争**、维修冻结与他人复核（禁止自复核/打回重修）、
跨线调拨权限（仅主任，审批前后领用拦截）、报废审批权限、
保养计划幂等生成、逾期处置，以及 HTTP 层的 multipart 照片上传。

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 夹具保存规格、当前工位、累计使用次数、寿命阈值 | `fixtures` 表；台账卡片显示寿命进度条 |
| 申请必须匹配工位允许机型与夹具适配机型 | `station_models` / `fixture_models` 双重校验 |
| 领用时锁定夹具 | 原子条件更新 `status available → locked` |
| 归还后先点检再进入可用 | `return` 仅置 `inspecting`；点检合格才回 `available` |
| 维修中不能被预订 | 申请指定夹具时拦截 `fixture_in_repair` |
| 变形 / 定位销缺失 | 点检不合格生成对应处置单 + 维修单 |
| 超寿命 | 合格点检时若累计次数 ≥ 阈值，生成超寿命处置单并进入待报废 |
| 跨线调拨 | 申请时指定其他产线夹具 → 调拨处置单，主任审批后夹具划归目标工位 |
| 归还逾期 | 守护任务扫描 `due_at`，逾期生成处置单与提醒 |
| 点检不合格冻结后续申请 | 借用人 `frozen=1`，复核通过后解冻 |
| 维修完成由另一名员工复核 | 复核人不能等于维修人；打回则维修单重开 |
| 两人同时领用只一人成功 | `BEGIN IMMEDIATE` + 条件 UPDATE，失败者返回 `already_locked` |

### 状态机

```
夹具: available ──领用──▶ locked ──归还──▶ inspecting
          ▲                   │              │
          │             (逾期处置单)     合格 │ 不合格
          │                                  ├─▶ available（寿命+本次次数；达阈值则 pending_scrap）
          │                                  └─▶ repairing ──维修完成──▶ in_review
          │                                        ▲          复核通过 │  ▲ 打回
          └────────────────────────────────────────┴──────────────────┘
        pending_scrap ──主任批准──▶ scrapped / 驳回则计数清零回 available
```

## 页面

- **夹具台账**：等轴测 SVG 小图按状态着色；寿命进度条（绿 → 信号黄 ≥80% → 红且闪烁 ≥100%）
- **工位匹配**：选工位+机型做适配校验，列出本工位可用夹具与跨线候选
- **申请队列**：新建申请（可指定夹具/触发跨线调拨），展示冻结、调拨待审等状态
- **借还扫码**：突出工位与夹具编号，大输入框；归还录入使用次数；点检坞支持拍照上传
- **保养计划**：到期计划、完成登记；可手动触发扫描
- **报废审批**：固定显示**累计次数 / 寿命阈值 / 最近点检**三栏；主任批准报废或驳回复用

配色：石墨灰（顶栏/正文）、信号黄（主操作/警示）、冷白（背景）。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/fixtures` | 夹具台账（`status`、`line` 过滤） |
| GET | `/api/stations/{id}?model=` | 工位匹配校验与可用夹具 |
| GET | `/api/stations/{id}/crossline?model=` | 跨线候选 |
| POST | `/api/requests` | 提交申请（`fixture_id` 同线指定 / `transfer_fixture_id` 跨线） |
| POST | `/api/borrows/checkout` | 扫码领用（并发抢锁） |
| POST | `/api/borrows/{id}/return` | 归还（`use_delta` 本次次数） |
| POST | `/api/inspections` | 点检（multipart：字段 + `photo` 文件） |
| POST | `/api/repairs/{id}/done` `/review` | 维修完成 / 他人复核 |
| POST | `/api/dispositions/{id}/scrap` `/transfer` | 报废审批 / 跨线调拨审批 |
| GET/POST | `/api/maintenance` `/api/maintenance/scan` | 保养计划与手动扫描 |
| GET | `/api/reminders` | 进程内任务生成的提醒 |

## 目录结构

```
fixture_system/
  db.py          SQLite 建表（WAL）
  service.py     全部业务规则与事务
  seed.py        虚构主数据 + 样例生成（领用/竞争/维修/报废）
  app.py         HTTP 服务 + multipart 解析 + 守护任务
  main.py        命令行入口
  static/        前端页面（index.html / styles.css / app.js）
tests/           unittest：领域规则 + HTTP 集成
data/            运行时生成：fixtures.db、photos/（已 gitignore）
```
