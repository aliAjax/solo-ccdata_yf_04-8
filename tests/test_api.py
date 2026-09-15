import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import create_server  # noqa: E402


def http(port, method, path, body=None, headers=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as res:
            raw = res.read().decode()
            ct = res.headers.get("Content-Type", "")
            return res.status, (json.loads(raw) if "json" in ct else raw), ct
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), ""


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "api.db")
        cls.server = create_server(0, cls.db)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        return http(self.port, method, path, body, headers)

    def test_00_static_index(self):
        status, body, ct = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("退款清算台", body)

    def test_01_import_sample_and_state(self):
        status, r, _ = self.call("POST", "/api/import/sample", {})
        self.assertEqual(status, 200)
        self.assertEqual(r["orders"]["imported"], 5)
        status, st, _ = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(st["orders"]), 5)
        o = next(x for x in st["orders"] if x["order_no"] == "ORD1001")
        self.assertEqual(o["pool_minor"], 120000)
        self.assertEqual(o["available_minor"], 120000)

    def test_02_trial_freeze_confirm(self):
        status, t, _ = self.call("POST", "/api/trial",
                                 {"order_no": "ORD1001", "amount": "600.00"})
        self.assertEqual(status, 200)
        self.assertEqual(sum(l["amount_minor"] for l in t["lines"]), 60000)

        status, r, _ = self.call(
            "POST", "/api/refunds",
            {"order_no": "ORD1001", "amount": "600.00", "reason": "质量问题",
             "idempotency_key": "api-k1"},
            {"Idempotency-Key": "api-k1"})
        self.assertEqual(status, 200)
        self.assertTrue(r["created"])
        no = r["refund"]["refund_no"]

        # 重复提交（网络重试）：同键同参 -> 返回原单，不重复冻结
        status, r2, _ = self.call(
            "POST", "/api/refunds",
            {"order_no": "ORD1001", "amount": "600.00", "reason": "质量问题",
             "idempotency_key": "api-k1"},
            {"Idempotency-Key": "api-k1"})
        self.assertFalse(r2["created"])
        self.assertEqual(r2["refund"]["refund_no"], no)

        status, st, _ = self.call("GET", "/api/state")
        o = next(x for x in st["orders"] if x["order_no"] == "ORD1001")
        self.assertEqual(o["frozen_minor"], 60000)

        status, c, _ = self.call("POST", f"/api/refunds/{no}/confirm", {})
        self.assertTrue(c["changed"])
        status, st, _ = self.call("GET", "/api/state")
        o = next(x for x in st["orders"] if x["order_no"] == "ORD1001")
        self.assertEqual((o["frozen_minor"], o["confirmed_minor"]), (0, 60000))

    def test_03_freeze_release_restores(self):
        _, r, _ = self.call("POST", "/api/refunds",
                            {"order_no": "ORD1001", "amount": "100.00",
                             "idempotency_key": "api-k2"})
        no = r["refund"]["refund_no"]
        _, rel, _ = self.call("POST", f"/api/refunds/{no}/release",
                              {"note": "用户撤销"})
        self.assertTrue(rel["changed"])
        _, st, _ = self.call("GET", "/api/state")
        o = next(x for x in st["orders"] if x["order_no"] == "ORD1001")
        self.assertEqual(o["frozen_minor"], 0)
        self.assertEqual(o["available_minor"], 60000)  # 1200 - 600 已确认

    def test_04_cross_currency_and_reverse(self):
        # USD 订单按基准币 CNY 发起退款：按成交汇率 7.10 折算
        _, r, _ = self.call("POST", "/api/refunds",
                            {"order_no": "ORD1002", "amount": "71.00",
                             "currency": "CNY", "idempotency_key": "api-k3"})
        self.assertEqual(r["refund"]["amount_minor"], 1000)  # 10.00 USD
        self.assertEqual(r["refund"]["amount_base_minor"], 7100)
        no = r["refund"]["refund_no"]
        self.call("POST", f"/api/refunds/{no}/confirm", {})
        _, rev, _ = self.call("POST", f"/api/refunds/{no}/reverse",
                              {"note": "冲正"})
        self.assertTrue(rev["changed"])
        _, st, _ = self.call("GET", "/api/state")
        o = next(x for x in st["orders"] if x["order_no"] == "ORD1002")
        self.assertEqual(o["confirmed_minor"], 0)
        self.assertEqual(o["available_minor"], 89999)

    def test_05_over_refund_and_bad_input(self):
        status, e, _ = self.call("POST", "/api/refunds",
                                 {"order_no": "ORD1005", "amount": "999.00",
                                  "idempotency_key": "api-k4"})
        self.assertEqual(status, 409)
        self.assertEqual(e["error"]["code"], "INSUFFICIENT_BALANCE")
        status, e, _ = self.call("POST", "/api/refunds",
                                 {"order_no": "ORD1005", "amount": "abc",
                                  "idempotency_key": "api-k5"})
        self.assertEqual(status, 400)
        status, e, _ = self.call("POST", "/api/refunds",
                                 {"order_no": "NOPE", "amount": "1.00",
                                  "idempotency_key": "api-k6"})
        self.assertEqual(status, 404)

    def test_06_verify_and_export(self):
        _, v, _ = self.call("GET", "/api/verify")
        self.assertTrue(v["ok"], v["issues"])
        status, csv_text, ct = self.call("GET", "/api/export/reconciliation.csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", ct)
        self.assertIn("ORD1001", csv_text)
        status, csv_text, _ = self.call("GET", "/api/export/ledger.csv")
        self.assertIn("FREEZE", csv_text)
        self.assertIn("REVERSE", csv_text)
        status, csv_text, _ = self.call("GET", "/api/export/refunds.csv")
        self.assertIn("R000001", csv_text)

    def test_07_persistence_across_restart(self):
        # 模拟服务重启：同一 DB 文件起新服务，状态完整恢复
        server2 = create_server(0, self.db)
        port2 = server2.server_address[1]
        t = threading.Thread(target=server2.serve_forever, daemon=True)
        t.start()
        try:
            status, st, _ = http(port2, "GET", "/api/state")
            self.assertEqual(status, 200)
            o = next(x for x in st["orders"] if x["order_no"] == "ORD1001")
            self.assertEqual(o["confirmed_minor"], 60000)
            self.assertGreater(len(st["ledger"]), 0)
        finally:
            server2.shutdown()
            server2.server_close()


if __name__ == "__main__":
    unittest.main()
