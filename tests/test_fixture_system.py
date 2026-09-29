"""测试夹具：每个用例使用独立临时库。"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from fixture_system.db import Database
from fixture_system.service import (
    FixtureService, ServiceError,
    AVAILABLE, LOCKED, REPAIRING, PENDING_SCRAP, SCRAPPED,
)
from fixture_system import seed


class FixtureTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fxtest-")
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.svc = FixtureService(self.db, os.path.join(self.dir, "photos"))
        seed.seed(self.db, self.svc)
        conn = self.db.connect()
        self.stations = {r["code"]: r["id"] for r in
                         conn.execute("SELECT id, code FROM stations")}
        self.models = {r["code"]: r["id"] for r in
                       conn.execute("SELECT id, code FROM models")}
        self.fixtures = {r["code"]: r["id"] for r in
                         conn.execute("SELECT id, code FROM fixtures")}
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- helpers --
    def _apply_checkout_return_inspect(self, applicant="OP-101",
                                       station="ST-A11", model="M-220",
                                       fixture="FZ-1001", delta=10,
                                       inspector="QA-401", result="pass",
                                       defect=None):
        r = self.svc.apply(applicant, self.stations[station],
                           self.models[model])
        self.svc.checkout(r["id"], self.fixtures[fixture], applicant)
        b = next(x for x in self.svc.active_borrows()
                 if x["request_code"] == r["code"])
        self.svc.return_fixture(b["id"], use_delta=delta)
        out = self.svc.inspect(r["id"], inspector, result, defect=defect)
        return r, out


class TestLifeAccumulation(FixtureTestCase):
    def test_count_accumulates_on_passed_inspection(self):
        before = self.svc.fixture_detail(self.fixtures["FZ-1001"])["use_count"]
        _, out = self._apply_checkout_return_inspect(delta=37)
        self.assertEqual(out["use_count"], before + 37)
        self.assertIsNone(out["disposition"])
        f = self.svc.fixture_detail(self.fixtures["FZ-1001"])
        self.assertEqual(f["status"], AVAILABLE)
        self.assertEqual(f["use_count"], before + 37)

    def test_over_life_generates_disposition_and_pending_scrap(self):
        # FZ-1002 已用 3050 / 4000，再用 1000 即超寿命
        r = self.svc.apply("OP-101", self.stations["ST-A12"],
                           self.models["M-220"])
        self.svc.checkout(r["id"], self.fixtures["FZ-1002"], "OP-101")
        b = next(x for x in self.svc.active_borrows()
                 if x["request_code"] == r["code"])
        self.svc.return_fixture(b["id"], use_delta=1000)
        out = self.svc.inspect(r["id"], "QA-401", "pass")
        self.assertIsNotNone(out["disposition"])
        self.assertEqual(out["disposition"]["type"], "over_life")
        f = self.svc.fixture_detail(self.fixtures["FZ-1002"])
        self.assertEqual(f["status"], PENDING_SCRAP)
        self.assertGreaterEqual(f["use_count"], f["life_threshold"])

    def test_near_threshold_not_flagged(self):
        # FZ-2002 已用 3420/3500，用 50 不超
        r = self.svc.apply("OP-201", self.stations["ST-B22"],
                           self.models["M-E90"])
        self.svc.checkout(r["id"], self.fixtures["FZ-2002"], "OP-201")
        b = next(x for x in self.svc.active_borrows()
                 if x["request_code"] == r["code"])
        self.svc.return_fixture(b["id"], use_delta=50)
        out = self.svc.inspect(r["id"], "QA-401", "pass")
        self.assertIsNone(out["disposition"])


class TestCompatibility(FixtureTestCase):
    def test_station_model_mismatch_rejected(self):
        # ST-A11 不生产 M-E90
        with self.assertRaises(ServiceError) as cm:
            self.svc.apply("OP-101", self.stations["ST-A11"],
                           self.models["M-E90"])
        self.assertEqual(cm.exception.code, "model_mismatch")

    def test_fixture_model_mismatch_rejected(self):
        # FZ-2001 只适配 M-E90，拿到 M-220 的申请上
        with self.assertRaises(ServiceError) as cm:
            self.svc.apply("OP-101", self.stations["ST-A12"],
                           self.models["M-220"],
                           preferred_fixture_id=self.fixtures["FZ-2001"])
        self.assertEqual(cm.exception.code, "fixture_model_mismatch")

    def test_checkout_station_mismatch_rejected(self):
        # 申请在 ST-A11，却扫另一工位的夹具 FZ-2001（即使机型也不允许）
        r = self.svc.apply("OP-101", self.stations["ST-A11"],
                           self.models["M-220"])
        with self.assertRaises(ServiceError):
            self.svc.checkout(r["id"], self.fixtures["FZ-2002"], "OP-101")

    def test_repairing_fixture_cannot_be_booked(self):
        # 先把 FZ-2001 打成维修中
        self._apply_checkout_return_inspect(
            applicant="OP-201", station="ST-B21", model="M-E90",
            fixture="FZ-2001", result="fail", defect="missing_pin")
        self.assertEqual(self.svc.fixture_detail(
            self.fixtures["FZ-2001"])["status"], REPAIRING)
        with self.assertRaises(ServiceError) as cm:
            self.svc.apply("OP-202", self.stations["ST-B21"],
                           self.models["M-E90"],
                           preferred_fixture_id=self.fixtures["FZ-2001"])
        self.assertEqual(cm.exception.code, "fixture_in_repair")


class TestCheckoutRace(FixtureTestCase):
    def test_two_operators_only_one_wins_threads(self):
        """真实并发：两个线程同时领取同一把夹具，恰好一人成功。"""
        import threading
        ra = self.svc.apply("OP-101", self.stations["ST-A12"],
                            self.models["M-220"])
        rb = self.svc.apply("OP-102", self.stations["ST-A12"],
                            self.models["M-220"])
        fid = self.fixtures["FZ-1002"]
        results = {}
        barrier = threading.Barrier(2)

        def worker(key, req_id, op):
            barrier.wait()  # 尽量同时进入
            try:
                self.svc.checkout(req_id, fid, op)
                results[key] = ("ok", None)
            except ServiceError as e:
                results[key] = ("fail", e.code)

        t1 = threading.Thread(target=worker, args=("a", ra["id"], "OP-101"))
        t2 = threading.Thread(target=worker, args=("b", rb["id"], "OP-102"))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = [k for k, v in results.items() if v[0] == "ok"]
        self.assertEqual(len(oks), 1, f"应只有一人成功，实际：{results}")
        self.assertEqual(results[oks[0]][1], None)
        loser = "b" if oks[0] == "a" else "a"
        self.assertEqual(results[loser][1], "already_locked")
        self.assertEqual(self.svc.fixture_detail(fid)["status"], LOCKED)

    def test_locked_request_cannot_checkout_again(self):
        r = self.svc.apply("OP-101", self.stations["ST-A11"],
                           self.models["M-220"])
        self.svc.checkout(r["id"], self.fixtures["FZ-1001"], "OP-101")
        with self.assertRaises(ServiceError) as cm:
            self.svc.checkout(r["id"], self.fixtures["FZ-1001"], "OP-101")
        self.assertEqual(cm.exception.code, "request_not_pending")


class TestRepairReview(FixtureTestCase):
    def _open_repair(self):
        _, out = self._apply_checkout_return_inspect(
            applicant="OP-201", station="ST-B21", model="M-E90",
            fixture="FZ-2001", result="fail", defect="missing_pin")
        return out["repair"]["id"]

    def test_failed_inspection_freezes_borrower_and_opens_repair(self):
        rid = self._open_repair()
        emp = next(e for e in self.svc.list_employees() if e["code"] == "OP-201")
        self.assertTrue(emp["frozen"])
        # 冻结后不能再提申请
        with self.assertRaises(ServiceError) as cm:
            self.svc.apply("OP-201", self.stations["ST-B21"],
                           self.models["M-E90"])
        self.assertEqual(cm.exception.code, "applicant_frozen")
        repair = self.svc.list_repairs()[0]
        self.assertEqual(repair["status"], "open")

    def test_same_employee_cannot_review_own_repair(self):
        rid = self._open_repair()
        self.svc.repair_done(rid, "MT-301", "补定位销")
        with self.assertRaises(ServiceError) as cm:
            self.svc.repair_review(rid, "MT-301", True)
        self.assertEqual(cm.exception.code, "self_review")

    def test_second_employee_review_passes_unfreezes(self):
        rid = self._open_repair()
        self.svc.repair_done(rid, "MT-301", "补定位销并校形")
        rev = self.svc.repair_review(rid, "QA-402", True, note="复检合格")
        self.assertEqual(rev["status"], "done")
        self.assertEqual(
            self.svc.fixture_detail(self.fixtures["FZ-2001"])["status"],
            AVAILABLE)
        emp = next(e for e in self.svc.list_employees() if e["code"] == "OP-201")
        self.assertFalse(emp["frozen"])

    def test_review_reject_reopens_repair(self):
        rid = self._open_repair()
        self.svc.repair_done(rid, "MT-301")
        rev = self.svc.repair_review(rid, "QA-402", False, note="仍有偏差")
        self.assertEqual(rev["status"], "open")
        self.assertEqual(
            self.svc.fixture_detail(self.fixtures["FZ-2001"])["status"],
            REPAIRING)


class TestCrossLine(FixtureTestCase):
    def _transfer(self):
        # ST-B22(L-B) 允许 M-225；FZ-1001 属于 L-A 且支持 M-225
        r = self.svc.apply("OP-202", self.stations["ST-B22"],
                           self.models["M-225"],
                           want_transfer_fixture_id=self.fixtures["FZ-1001"])
        disp = r["transfer_disposition"]
        self.assertIsNotNone(disp)
        self.assertEqual(disp["type"], "transfer")
        return r, disp

    def test_checkout_blocked_before_transfer_approval(self):
        r, disp = self._transfer()
        with self.assertRaises(ServiceError) as cm:
            self.svc.checkout(r["id"], self.fixtures["FZ-1001"], "OP-202")
        self.assertEqual(cm.exception.code, "station_mismatch")

    def test_only_manager_can_approve_transfer(self):
        _, disp = self._transfer()
        with self.assertRaises(ServiceError) as cm:
            self.svc.transfer_decide(disp["id"], "MT-301", True)
        self.assertEqual(cm.exception.code, "forbidden")
        with self.assertRaises(ServiceError):
            self.svc.transfer_decide(disp["id"], "OP-101", True)

    def test_approved_transfer_moves_fixture_and_allows_checkout(self):
        r, disp = self._transfer()
        self.svc.transfer_decide(disp["id"], "MGR-001", True)
        f = self.svc.fixture_detail(self.fixtures["FZ-1001"])
        self.assertEqual(f["station"]["id"], self.stations["ST-B22"])
        self.assertEqual(f["line"]["id"], f["station"]["line_id"])
        out = self.svc.checkout(r["id"], self.fixtures["FZ-1001"], "OP-202")
        self.assertEqual(out["station_code"], "ST-B22")

    def test_rejected_transfer_cancels_request(self):
        r, disp = self._transfer()
        self.svc.transfer_decide(disp["id"], "MGR-001", False)
        q = {x["id"]: x for x in self.svc.request_queue()}
        self.assertEqual(q[r["id"]]["status"], "cancelled")


class TestScrapApproval(FixtureTestCase):
    def _scrap_disp(self):
        # FZ-1003 已 5960/6000，用 40 触发超寿命
        r = self.svc.apply("OP-102", self.stations["ST-A13"],
                           self.models["M-220"],
                           preferred_fixture_id=self.fixtures["FZ-1003"])
        self.svc.checkout(r["id"], self.fixtures["FZ-1003"], "OP-102")
        b = next(x for x in self.svc.active_borrows()
                 if x["request_code"] == r["code"])
        self.svc.return_fixture(b["id"], use_delta=40)
        out = self.svc.inspect(r["id"], "QA-402", "pass")
        return out["disposition"]["id"]

    def test_non_manager_cannot_approve_scrap(self):
        did = self._scrap_disp()
        with self.assertRaises(ServiceError) as cm:
            self.svc.scrap_decide(did, "QA-401", True)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_approve_scrap_marks_scrapped(self):
        did = self._scrap_disp()
        out = self.svc.scrap_decide(did, "MGR-001", True)
        self.assertEqual(out["disposition"]["status"], "scrapped")
        self.assertEqual(out["fixture"]["status"], SCRAPPED)
        # 审批核对固定三项
        self.assertIn("use_count", out["checked"])
        self.assertIn("life_threshold", out["checked"])
        self.assertIsNotNone(out["checked"]["latest_inspection"])

    def test_reject_scrap_returns_fixture_to_service(self):
        did = self._scrap_disp()
        out = self.svc.scrap_decide(did, "MGR-001", False, note="大修复用")
        self.assertEqual(out["disposition"]["status"], "rejected")
        self.assertEqual(out["fixture"]["status"], AVAILABLE)
        self.assertEqual(out["fixture"]["use_count"], 0)


class TestMaintenanceAndOverdue(FixtureTestCase):
    def test_due_maintenance_creates_plan_and_reminder(self):
        # FZ-2003 末次保养 95 天前，周期 90
        plans = self.svc.generate_maintenance_plans()
        fids = {p["fixture_id"] for p in plans}
        self.assertIn(self.fixtures["FZ-2003"], fids)
        msgs = " ".join(r["message"] for r in self.svc.reminders())
        self.assertIn("FZ-2003", msgs)
        # 幂等：再扫不重复生成
        again = self.svc.generate_maintenance_plans()
        self.assertFalse(any(p["fixture_id"] == self.fixtures["FZ-2003"]
                             for p in again))

    def test_overdue_borrow_generates_disposition(self):
        # 正常时钟领用（应还 = 现在+24h），再用超前 3 天的时钟扫描
        r = self.svc.apply("OP-101", self.stations["ST-A11"],
                           self.models["M-220"])
        self.svc.checkout(r["id"], self.fixtures["FZ-1001"], "OP-101")
        future = FixtureService(
            self.db, os.path.join(self.dir, "photos"),
            now=lambda: datetime.now() + timedelta(days=3))
        disps = future.scan_overdue()
        self.assertEqual(len(disps), 1)
        self.assertEqual(disps[0]["type"], "overdue")
        # 幂等
        self.assertEqual(future.scan_overdue(), [])


class TestDefectDispositions(FixtureTestCase):
    def test_deformation_and_missing_pin_dispositions(self):
        for defect, fixture, applicant, station, model in [
            ("deformation", "FZ-1001", "OP-101", "ST-A11", "M-220"),
            ("missing_pin", "FZ-2002", "OP-202", "ST-B22", "M-E90"),
        ]:
            self._apply_checkout_return_inspect(
                applicant=applicant, station=station, model=model,
                fixture=fixture, result="fail", defect=defect)
        types = {d["type"] for d in self.svc.list_dispositions()}
        self.assertIn("deformation", types)
        self.assertIn("missing_pin", types)


if __name__ == "__main__":
    unittest.main()
