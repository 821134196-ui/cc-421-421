"""五项核心规则测试：

  1. 寿命累计             test_life_accumulation
  2. 适配校验             test_request_match_validation
  3. 领用竞争（双线程）   test_concurrent_pickup_only_one_wins
  4. 维修复核（须另一人） test_repair_review_must_be_another_employee
  5. 跨线权限             test_cross_line_permissions

运行:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from system import service
from system.db import connect, init_db
from system.seed import seed_if_empty


class FixtureTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)
        seeded = seed_if_empty(self.conn)
        self.assertTrue(seeded)
        self.photo_dir = Path(self.tmp.name) / "photos"
        self.photo_dir.mkdir()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def fixture(self, fid):
        return self.conn.execute(
            "SELECT * FROM fixtures WHERE id=?", (fid,)).fetchone()

    # 夹具申请 → 批准 → 领用 的快捷链路
    def borrow_flow(self, fixture="FX-3002", station="ST-C01",
                    model="MX-300", applicant="EMP-005", approver="EMP-012"):
        rid = service.create_request(
            self.conn, fixture, station, model, applicant, "测试借用")
        service.approve_request(self.conn, rid, approver)
        return rid


# ---------------------------------------------------------------- 1 寿命
class TestLifeAccumulation(FixtureTestBase):
    def test_life_accumulation(self):
        before = self.fixture("FX-3002")["use_count"]
        rid = self.borrow_flow()
        res = service.pickup(self.conn, rid, "EMP-005")
        after = self.fixture("FX-3002")

        # 每次领用累计 +1
        self.assertEqual(after["use_count"], before + 1)
        self.assertEqual(res["use_count"], after["use_count"])
        self.assertLess(after["use_count"], after["life_threshold"])
        self.assertIsNone(res["over_life_disposal"])

        # 寿命条/剩余次数视图正确
        view = service.get_fixture(self.conn, "FX-3002")
        self.assertEqual(view["remaining"],
                         view["life_threshold"] - view["use_count"])

    def test_over_life_generates_disposal_and_blocks_new_request(self):
        # FX-1003 当前 1480/1500，直接推到阈值再领用
        with self.conn:
            self.conn.execute(
                "UPDATE fixtures SET use_count=1499 WHERE id='FX-1003'")
        rid = service.create_request(
            self.conn, "FX-1003", "ST-A02", "MX-100", "EMP-001")
        service.approve_request(self.conn, rid, "EMP-021")
        res = service.pickup(self.conn, rid, "EMP-001")

        self.assertEqual(res["use_count"], 1500)
        self.assertIsNotNone(res["over_life_disposal"])
        order = self.conn.execute(
            "SELECT * FROM disposal_orders WHERE id=?",
            (res["over_life_disposal"],)).fetchone()
        self.assertEqual(order["type"], "over_life")

        # 归还点检合格后夹具停在 reserved 等待报废判定，不回可用
        service.return_fixture(self.conn, rid, "EMP-001")
        service.inspect(self.conn, rid, "EMP-011", "pass", "功能正常")
        self.assertEqual(self.fixture("FX-1003")["status"], "reserved")

        # 已达寿命阈值的夹具不能再发起新申请
        with self.assertRaises(service.ServiceError):
            service.create_request(
                self.conn, "FX-1003", "ST-A02", "MX-100", "EMP-002")


# ------------------------------------------------------------ 2 适配校验
class TestMatchValidation(FixtureTestBase):
    def test_request_match_validation(self):
        # 工位不生产该机型
        with self.assertRaises(service.ServiceError):
            service.create_request(
                self.conn, "FX-1001", "ST-A02", "MX-200", "EMP-001")

        # 夹具不适配该机型（B01 生产 MX-300，但 FX-1001 只适配 100/200）
        with self.assertRaises(service.ServiceError):
            service.create_request(
                self.conn, "FX-1001", "ST-B01", "MX-300", "EMP-003")

        # 匹配成功
        rid = service.create_request(
            self.conn, "FX-1001", "ST-A01", "MX-100", "EMP-001")
        self.assertTrue(rid.startswith("RQ-"))

        # 已被预订后，第二个申请冲突
        service.approve_request(self.conn, rid, "EMP-021")
        with self.assertRaises(service.ConflictError):
            service.create_request(
                self.conn, "FX-1001", "ST-A01", "MX-100", "EMP-002")

        # 点检不合格 → 冻结后续申请
        service.pickup(self.conn, rid, "EMP-001")
        service.return_fixture(self.conn, rid, "EMP-001")
        service.inspect(self.conn, rid, "EMP-011", "fail", "定位销缺失")
        fx = self.fixture("FX-1001")
        self.assertEqual(fx["status"], "in_repair")
        self.assertEqual(fx["frozen"], 1)
        with self.assertRaises(service.ServiceError):
            service.create_request(
                self.conn, "FX-1001", "ST-A01", "MX-100", "EMP-002")

        # 维修中的夹具不能被预订（独立入口同样被拦）
        with self.assertRaises(service.ServiceError):
            service.create_request(
                self.conn, "FX-1001", "ST-A01", "MX-200", "EMP-002")

    def test_match_endpoint_lists_only_compatible(self):
        m = service.match_station(self.conn, "ST-A01", "MX-100")
        self.assertTrue(m["station_supports_model"])
        ids = {f["id"] for f in m["fixtures"]}
        self.assertIn("FX-1001", ids)
        self.assertNotIn("FX-2002", ids)  # 只适配 MX-300
        # FX-1001 属 L1 与 A01 同线且当前就在 A01
        f1001 = next(f for f in m["fixtures"] if f["id"] == "FX-1001")
        self.assertTrue(f1001["fits_station"])
        self.assertTrue(f1001["at_station"])


# --------------------------------------------------------- 3 领用竞争
class TestConcurrentPickup(FixtureTestBase):
    def test_concurrent_pickup_only_one_wins(self):
        """两名操作员各自走完申请-批准后同时抢同一夹具：只允许一人成功。"""
        barrier = threading.Barrier(2)
        results: list[Exception | str] = []
        results_lock = threading.Lock()

        # 两条申请都想领 FX-3002：只有先被批准的一条能把夹具锁成 reserved；
        # 为了精确模拟“同一夹具、同时领用”，让两条申请先后批准，
        # 再让两个线程在同一屏障释放后并发 pickup。
        r1 = service.create_request(
            self.conn, "FX-3002", "ST-C01", "MX-300", "EMP-005")
        service.approve_request(self.conn, r1, "EMP-012")

        # 第二个人无法再对同一夹具申请成功（已 reserved），
        # 因此并发竞争直接针对同一张已批准申请 + 两个线程同时扫码。
        def worker():
            try:
                barrier.wait(timeout=5)
                # 任意线程都以申请人身份扫码；服务层原子条件更新决定赢家
                res = service.pickup(self.conn, r1, "EMP-005",
                                     token=threading.current_thread().name)
                with results_lock:
                    results.append(("ok", res))
            except Exception as exc:
                with results_lock:
                    results.append(("err", exc))

        t1 = threading.Thread(target=worker, name="scanner-A")
        t2 = threading.Thread(target=worker, name="scanner-B")
        t1.start(); t2.start(); t1.join(); t2.join()

        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "err"]
        self.assertEqual(len(oks), 1, f"应恰有一人成功，实际: {results}")
        self.assertEqual(len(errs), 1)
        self.assertIsInstance(errs[0][1], service.ConflictError)

        fx = self.fixture("FX-3002")
        self.assertEqual(fx["status"], "in_use")
        # 寿命只累计一次，没有重复计数
        self.assertEqual(fx["use_count"], 1)

        # 赢家令牌（线程名）被记录在申请单上
        rq = self.conn.execute("SELECT * FROM requests WHERE id=?",
                               (r1,)).fetchone()
        self.assertEqual(rq["status"], "picked")
        self.assertIn(rq["lock_owner"], ("scanner-A", "scanner-B"))


# --------------------------------------------------------- 4 维修复核
class TestRepairReview(FixtureTestBase):
    def _fixture_under_review(self):
        rid = self.borrow_flow()
        service.pickup(self.conn, rid, "EMP-005")
        service.return_fixture(self.conn, rid, "EMP-005")
        service.inspect(self.conn, rid, "EMP-011", "fail",
                        "压紧面变形，定位销缺失")
        rp = service.start_repair(
            self.conn, "FX-3002", "EMP-011", "EMP-011", "校正压紧面并补销")
        service.finish_repair(self.conn, rp, "EMP-011")
        return rid, rp

    def test_repair_review_must_be_another_employee(self):
        _, rp = self._fixture_under_review()

        # 承修人本人不能复核
        with self.assertRaises(service.PermissionError_):
            service.review_repair(self.conn, rp, "EMP-011", True, "我修的我批")

        # 操作员不能复核
        with self.assertRaises(service.PermissionError_):
            service.review_repair(self.conn, rp, "EMP-005", True)

        # 另一名员工（另一线维修班）复核通过 → 解冻回可用
        service.review_repair(self.conn, rp, "EMP-012", True, "复测合格")
        fx = self.fixture("FX-3002")
        self.assertEqual(fx["status"], "available")
        self.assertEqual(fx["frozen"], 0)
        done = self.conn.execute("SELECT * FROM repairs WHERE id=?",
                                 (rp,)).fetchone()
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["reviewer_id"], "EMP-012")
        self.assertNotEqual(done["reviewer_id"], done["repairer_id"])

    def test_review_rejection_reopens_repair(self):
        _, rp = self._fixture_under_review()
        service.review_repair(self.conn, rp, "EMP-021", False, "销位仍偏 0.2mm")
        cur = self.conn.execute("SELECT * FROM repairs WHERE id=?",
                                (rp,)).fetchone()
        self.assertEqual(cur["status"], "open")
        # 夹具仍冻结
        self.assertEqual(self.fixture("FX-3002")["frozen"], 1)

    def test_failed_inspection_creates_two_disposal_orders(self):
        self._fixture_under_review()
        types = {r["type"] for r in self.conn.execute(
            "SELECT type FROM disposal_orders WHERE fixture_id='FX-3002'")}
        self.assertIn("deformation", types)
        self.assertIn("pin_missing", types)


# --------------------------------------------------------- 5 跨线权限
class TestCrossLinePermission(FixtureTestBase):
    def test_cross_line_permissions(self):
        # FX-3002 在 L3/ST-C01，目标 ST-A01 在 L1
        # 操作员无权发起调拨
        with self.assertRaises(service.PermissionError_):
            service.request_transfer(
                self.conn, "FX-3002", "ST-A01", "EMP-005")

        # 同线工位不构成跨线
        with self.assertRaises(service.ServiceError):
            service.request_transfer(
                self.conn, "FX-3002", "ST-C01", "EMP-011")

        # 维修班可发起
        tid = service.request_transfer(
            self.conn, "FX-3002", "ST-A01", "EMP-012", "产能支援一线")

        # 操作员不能审批；非调入线（L2）工段长也不能审批
        with self.assertRaises(service.PermissionError_):
            service.approve_transfer(self.conn, tid, "EMP-005")
        with self.assertRaises(service.PermissionError_):
            service.approve_transfer(self.conn, tid, "EMP-022")

        # 调入线 L1 工段长审批通过 → 夹具换线换工位，生成跨线处置单
        did = service.approve_transfer(self.conn, tid, "EMP-021")
        fx = self.fixture("FX-3002")
        self.assertEqual(fx["station_id"], "ST-A01")
        self.assertEqual(fx["line_id"], "L1")
        order = self.conn.execute(
            "SELECT * FROM disposal_orders WHERE id=?", (did,)).fetchone()
        self.assertEqual(order["type"], "transfer")

        # 调拨后，原 L3 操作员跨线申请 L1 工位被拒；L1 操作员可申请
        with self.assertRaises(service.PermissionError_):
            service.create_request(
                self.conn, "FX-3002", "ST-A01", "MX-100", "EMP-005")
        rid = service.create_request(
            self.conn, "FX-3002", "ST-A01", "MX-100", "EMP-001")
        self.assertTrue(rid)

    def test_overdue_return_generates_disposal(self):
        # 归还逾期处置单
        rid = self.borrow_flow()
        service.pickup(self.conn, rid, "EMP-005")
        with self.conn:
            self.conn.execute("UPDATE requests SET due_at=? WHERE id=?",
                              (0, rid))
        service.return_fixture(self.conn, rid, "EMP-005")
        order = self.conn.execute(
            "SELECT * FROM disposal_orders WHERE type='overdue'").fetchone()
        self.assertIsNotNone(order)
        self.assertEqual(order["fixture_id"], "FX-3002")


if __name__ == "__main__":
    unittest.main()
