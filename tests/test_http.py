"""HTTP 端到端测试：启动真实的 AppServer（随机端口），验证 JSON API
与 multipart 点检照片上传。"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import uuid

from fixture_system.app import AppServer


def _multipart(fields: dict, file_field=None, filename="q.jpg",
               data=b"\xff\xd8\xff\xe0fake"):
    boundary = "----fxboundary" + uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode()
    if file_field:
        body += (f"--{boundary}\r\nContent-Disposition: form-data;"
                 f" name=\"{file_field}\"; filename=\"{filename}\"\r\n"
                 f"Content-Type: image/jpeg\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class HttpTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="fxhttp-")
        cls.httpd = AppServer(
            ("127.0.0.1", 0),
            os.path.join(cls.dir, "t.db"),
            os.path.join(cls.dir, "photos"))
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def call(self, path, payload=None, raw=None, ctype="application/json"):
        url = self.base + path
        if raw is not None:
            data, ct = raw
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": ct}, method="POST")
        elif payload is not None:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": ctype}, method="POST")
        else:
            req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_meta_and_pages(self):
        st, meta = self.call("/api/meta")
        self.assertEqual(st, 200)
        self.assertEqual(len(meta["lines"]), 2)
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn("text/html", r.headers["Content-Type"])
            self.assertIn("夹具".encode(), r.read())

    def test_full_borrow_flow_with_photo_upload(self):
        _, stns = self.call("/api/stations")
        _, mdls = self.call("/api/models")
        _, fxs = self.call("/api/fixtures")
        sid = {s["code"]: s["id"] for s in stns}["ST-A11"]
        mid = {m["code"]: m["id"] for m in mdls}["M-220"]
        fid = {f["code"]: f["id"] for f in fxs}["FZ-1001"]

        _, req = self.call("/api/requests",
                           {"applicant": "OP-101", "station_id": sid,
                            "model_id": mid, "note": "http流程"})
        _, out = self.call("/api/borrows/checkout",
                           {"request_id": req["id"], "fixture_id": fid,
                            "operator": "OP-101"})
        self.assertEqual(out["fixture_code"], "FZ-1001")
        _, borrows = self.call("/api/borrows/active")
        bid = next(b["id"] for b in borrows if b["request_id"] == req["id"])
        self.call(f"/api/borrows/{bid}/return", {"use_delta": 12})

        # multipart 照片点检
        body, ct = _multipart(
            {"request_id": req["id"], "inspector": "QA-401",
             "result": "pass", "note": "照片点检"},
            file_field="photo", filename="station-check.jpg")
        st, insp = self.call("/api/inspections", raw=(body, ct))
        self.assertEqual(st, 200)
        photo = insp["inspection"]["photo_path"]
        self.assertTrue(photo)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "photos", photo)))
        with urllib.request.urlopen(self.base + "/photos/" + photo) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(insp["use_count"], 480 + 12)

    def test_compatibility_error_payload(self):
        _, stns = self.call("/api/stations")
        _, mdls = self.call("/api/models")
        sid = {s["code"]: s["id"] for s in stns}["ST-A11"]
        me90 = {m["code"]: m["id"] for m in mdls}["M-E90"]
        st, body = self.call("/api/requests",
                             {"applicant": "OP-101", "station_id": sid,
                              "model_id": me90})
        self.assertEqual(st, 400)
        self.assertEqual(body["code"], "model_mismatch")


class SampleStartupTestCase(unittest.TestCase):
    """--samples 启动路径：AppServer(with_samples=True) 自带样例。"""
    def test_samples_generated(self):
        d = tempfile.mkdtemp(prefix="fxsample-")
        httpd = AppServer(
            ("127.0.0.1", 0),
            os.path.join(d, "t.db"), os.path.join(d, "photos"),
            with_samples=True, scan_interval=3600)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/fixtures") as r:
                fxs = json.load(r)
            by_code = {f["code"]: f for f in fxs}
            self.assertEqual(by_code["FZ-1003"]["status"], "pending_scrap")
            self.assertGreaterEqual(by_code["FZ-1001"]["use_count"], 505)
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/repairs") as r:
                repairs = json.load(r)
            self.assertTrue(any(x["status"] == "done" for x in repairs))
        finally:
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
