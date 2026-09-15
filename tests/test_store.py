import os
import tempfile
import unittest

from refunddesk.store import Store, ApiError


def make_store():
    return Store(":memory:")


def seed_basic(s):
    """ORD1 CNY 1200.00 = 卡700 + 支付宝400 + 券100。"""
    s.import_orders([{"order_no": "ORD1", "currency": "CNY", "fx_rate": "1",
                      "total": "1200.00"}])
    s.import_payments([
        {"payment_no": "P1", "order_no": "ORD1", "method": "银行卡", "amount": "700.00"},
        {"payment_no": "P2", "order_no": "ORD1", "method": "支付宝", "amount": "400.00"},
    ])
    s.import_discounts([
        {"alloc_no": "D1", "order_no": "ORD1", "coupon_no": "CPN1", "amount": "100.00"},
    ])


def balance(s, order_no="ORD1"):
    st = s.state()
    o = next(o for o in st["orders"] if o["order_no"] == order_no)
    return o


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.s = make_store()
        seed_basic(self.s)

    def test_import_pool(self):
        o = balance(self.s)
        self.assertEqual(o["pool_minor"], 120000)
        self.assertEqual(o["available_minor"], 120000)
        self.assertEqual(o["frozen_minor"], 0)

    def test_trial_split_proportional(self):
        t = self.s.trial("ORD1", "600.00")
        self.assertEqual(t["amount_minor"], 60000)
        got = {ln["source_ref"]: ln["amount_minor"] for ln in t["lines"]}
        self.assertEqual(got, {"P1": 35000, "P2": 20000, "D1": 5000})
        self.assertEqual(sum(ln["amount_base_minor"] for ln in t["lines"]),
                         t["amount_base_minor"])

    def test_freeze_confirm_release_flow(self):
        # 冻结
        r = self.s.freeze("ORD1", "100.00", None, "测试", "k-1")["refund"]
        self.assertEqual(r["status"], "FROZEN")
        o = balance(self.s)
        self.assertEqual((o["frozen_minor"], o["available_minor"]), (10000, 110000))
        # 确认记账
        self.s.confirm(r["refund_no"])
        o = balance(self.s)
        self.assertEqual((o["frozen_minor"], o["confirmed_minor"],
                          o["available_minor"]), (0, 10000, 110000))
        # 再冻结一笔然后撤销：额度原样释放
        r2 = self.s.freeze("ORD1", "50.00", None, "", "k-2")["refund"]
        self.assertEqual(balance(self.s)["frozen_minor"], 5000)
        self.s.release(r2["refund_no"], "用户取消")
        o = balance(self.s)
        self.assertEqual((o["frozen_minor"], o["available_minor"]), (0, 110000))
        self.assertTrue(self.s.verify()["ok"])

    def test_over_refund_rejected(self):
        self.s.freeze("ORD1", "1200.00", None, "", "k-full")
        with self.assertRaises(ApiError) as ctx:
            self.s.freeze("ORD1", "0.01", None, "", "k-more")
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_BALANCE")

    def test_negative_impossible(self):
        with self.assertRaises(ApiError):
            self.s.freeze("ORD1", "1200.01", None, "", "k-neg")
        self.assertEqual(balance(self.s)["available_minor"], 120000)

    def test_multiple_partial_refunds_exhaust_pool_exactly(self):
        amounts = ["300.00", "500.00", "399.99", "0.01"]
        for i, a in enumerate(amounts):
            r = self.s.freeze("ORD1", a, None, "", f"k-{i}")["refund"]
            self.s.confirm(r["refund_no"])
        o = balance(self.s)
        self.assertEqual(o["available_minor"], 0)
        self.assertEqual(o["confirmed_minor"], 120000)
        with self.assertRaises(ApiError):
            self.s.freeze("ORD1", "0.01", None, "", "k-overflow")
        self.assertTrue(self.s.verify()["ok"])

    def test_source_level_never_negative_after_many_refunds(self):
        # 多次部分退款后，任一来源累计回冲不得超过其原始份额
        for i in range(12):
            self.s.freeze("ORD1", "100.00", None, "", f"k-{i}")
        st = self.s.state()
        used = {}
        for r in st["refunds"]:
            for ln in r["lines"]:
                used[ln["source_ref"]] = used.get(ln["source_ref"], 0) + ln["amount_minor"]
        self.assertLessEqual(used.get("P1", 0), 70000)
        self.assertLessEqual(used.get("P2", 0), 40000)
        self.assertLessEqual(used.get("D1", 0), 10000)
        self.assertEqual(sum(used.values()), 120000)

    def test_reverse_restores_and_is_auditable(self):
        r = self.s.freeze("ORD1", "200.00", None, "", "k-r")["refund"]
        self.s.confirm(r["refund_no"])
        self.s.reverse(r["refund_no"], "误操作冲正")
        o = balance(self.s)
        self.assertEqual((o["confirmed_minor"], o["available_minor"]), (0, 120000))
        st = self.s.state()
        rev = [e for e in st["ledger"] if e["action"] == "REVERSE"]
        self.assertEqual(sum(e["amount_minor"] for e in rev), 20000)
        self.assertTrue(all(e["refund_no"] == r["refund_no"] for e in rev))
        self.assertTrue(self.s.verify()["ok"])

    def test_illegal_transitions(self):
        r = self.s.freeze("ORD1", "10.00", None, "", "k-t")["refund"]
        with self.assertRaises(ApiError):  # 未确认不能冲正
            self.s.reverse(r["refund_no"])
        self.s.confirm(r["refund_no"])
        with self.assertRaises(ApiError):  # 已记账不能撤销
            self.s.release(r["refund_no"])
        with self.assertRaises(ApiError):  # 已记账不能重复确认以外的迁移
            self.s.release(r["refund_no"], "再试")


class TestIdempotency(unittest.TestCase):
    def setUp(self):
        self.s = make_store()
        seed_basic(self.s)

    def test_same_key_returns_same_refund(self):
        r1 = self.s.freeze("ORD1", "100.00", None, "a", "dup-key")
        r2 = self.s.freeze("ORD1", "100.00", None, "a", "dup-key")
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        self.assertEqual(r1["refund"]["refund_no"], r2["refund"]["refund_no"])
        self.assertEqual(balance(self.s)["frozen_minor"], 10000)  # 只冻结一次

    def test_same_key_different_payload_conflicts(self):
        self.s.freeze("ORD1", "100.00", None, "a", "dup-key")
        with self.assertRaises(ApiError) as ctx:
            self.s.freeze("ORD1", "200.00", None, "a", "dup-key")
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_KEY_REUSE")

    def test_confirm_release_reverse_idempotent(self):
        no = self.s.freeze("ORD1", "10.00", None, "", "k-c")["refund"]["refund_no"]
        self.assertTrue(self.s.confirm(no)["changed"])
        self.assertFalse(self.s.confirm(no)["changed"])  # 重复确认不再记账
        self.assertTrue(self.s.reverse(no)["changed"])
        self.assertFalse(self.s.reverse(no)["changed"])  # 重复冲正不再回冲
        r2 = self.s.freeze("ORD1", "10.00", None, "", "k-r2")["refund"]["refund_no"]
        self.assertTrue(self.s.release(r2)["changed"])
        self.assertFalse(self.s.release(r2)["changed"])  # 重复释放不再返还
        o = balance(self.s)
        self.assertEqual((o["frozen_minor"], o["confirmed_minor"],
                          o["available_minor"]), (0, 0, 120000))
        self.assertTrue(self.s.verify()["ok"])


class TestCrossCurrency(unittest.TestCase):
    def setUp(self):
        self.s = make_store()
        self.s.import_orders([{"order_no": "ORDU", "currency": "USD",
                               "fx_rate": "7.10", "total": "899.99"}])
        self.s.import_payments([
            {"payment_no": "PU1", "order_no": "ORDU", "method": "信用卡",
             "amount": "600.00"},
            {"payment_no": "PU2", "order_no": "ORDU", "method": "余额",
             "amount": "199.99"},
        ])
        self.s.import_discounts([
            {"alloc_no": "DU1", "order_no": "ORDU", "coupon_no": "CPNV",
             "amount": "100.00"},
        ])

    def test_refund_in_order_currency_derives_base(self):
        r = self.s.freeze("ORDU", "100.00", "USD", "", "k-usd")["refund"]
        self.assertEqual(r["amount_minor"], 10000)
        self.assertEqual(r["amount_base_minor"], 71000)  # 100 USD @7.10
        self.assertEqual(sum(l["amount_base_minor"] for l in r["lines"]), 71000)

    def test_refund_in_base_currency_uses_order_fx_rate(self):
        # 以基准币发起：按订单成交汇率折算回订单币种
        r = self.s.freeze("ORDU", "71.00", "CNY", "", "k-cny")["refund"]
        self.assertEqual(r["amount_minor"], 1000)  # 71 CNY / 7.10 = 10.00 USD
        self.assertEqual(r["amount_base_minor"], 7100)

    def test_line_base_amounts_sum_to_header(self):
        # 明细基准币合计必须等于单据基准币（舍入差并入最大行）
        r = self.s.freeze("ORDU", "33.33", "USD", "", "k-round")["refund"]
        self.assertEqual(sum(l["amount_base_minor"] for l in r["lines"]),
                         r["amount_base_minor"])

    def test_unsupported_currency_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            self.s.freeze("ORDU", "10.00", "EUR", "", "k-eur")
        self.assertEqual(ctx.exception.code, "CURRENCY_UNSUPPORTED")


class TestPersistence(unittest.TestCase):
    def test_state_survives_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.db")
            s1 = Store(path)
            seed_basic(s1)
            no = s1.freeze("ORD1", "88.00", None, "", "k-p")["refund"]["refund_no"]
            s1.confirm(no)
            # 模拟重启：同一数据库文件重新打开
            s2 = Store(path)
            o = balance(s2)
            self.assertEqual((o["confirmed_minor"], o["available_minor"]),
                             (8800, 111200))
            st = s2.state()
            self.assertEqual(len(st["refunds"]), 1)
            self.assertTrue(s2.verify()["ok"])


class TestImportIntegrity(unittest.TestCase):
    def test_unknown_order_rejected(self):
        s = make_store()
        with self.assertRaises(ApiError):
            s.import_payments([{"payment_no": "PX", "order_no": "NOPE",
                                "method": "卡", "amount": "1.00"}])

    def test_reimport_is_upsert_not_duplicate(self):
        s = make_store()
        seed_basic(s)
        seed_basic(s)  # 再导一遍
        self.assertEqual(balance(s)["pool_minor"], 120000)

    def test_cannot_change_sources_after_refund(self):
        s = make_store()
        seed_basic(s)
        s.freeze("ORD1", "1.00", None, "", "k-b")
        with self.assertRaises(ApiError) as ctx:
            s.import_payments([{"payment_no": "P9", "order_no": "ORD1",
                                "method": "卡", "amount": "1.00"}])
        self.assertEqual(ctx.exception.code, "ORDER_HAS_REFUNDS")


if __name__ == "__main__":
    unittest.main()
