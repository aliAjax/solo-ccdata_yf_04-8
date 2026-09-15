import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from refunddesk.store import Store, ApiError


def seeded_store():
    s = Store(":memory:")
    s.import_orders([{"order_no": "ORD1", "currency": "CNY", "fx_rate": "1",
                      "total": "100.00"}])
    s.import_payments([
        {"payment_no": "P1", "order_no": "ORD1", "method": "银行卡",
         "amount": "60.00"},
        {"payment_no": "P2", "order_no": "ORD1", "method": "支付宝",
         "amount": "40.00"},
    ])
    return s


def available(s):
    st = s.state()
    return next(o for o in st["orders"] if o["order_no"] == "ORD1")


class TestConcurrentFreeze(unittest.TestCase):
    def test_no_over_refund_under_race(self):
        """20 个线程各冻 10 元，池子 100 元：恰好 10 个成功，不超退。"""
        s = seeded_store()

        def attempt(i):
            try:
                r = s.freeze("ORD1", "10.00", None, "", f"race-{i}")
                return r["created"]
            except ApiError as e:
                if e.code == "INSUFFICIENT_BALANCE":
                    return False
                raise

        with ThreadPoolExecutor(max_workers=20) as ex:
            results = list(ex.map(attempt, range(20)))

        self.assertEqual(sum(results), 10)
        o = available(s)
        self.assertEqual(o["frozen_minor"], 10000)
        self.assertEqual(o["available_minor"], 0)
        self.assertTrue(s.verify()["ok"])

    def test_same_idempotency_key_concurrent(self):
        """同一幂等键并发提交：只冻结一次，所有请求拿到同一单。"""
        s = seeded_store()

        def attempt(_):
            return s.freeze("ORD1", "25.00", None, "", "same-key")

        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(attempt, range(10)))

        created = [r for r in results if r["created"]]
        self.assertEqual(len(created), 1)
        self.assertEqual(len({r["refund"]["refund_no"] for r in results}), 1)
        self.assertEqual(available(s)["frozen_minor"], 2500)

    def test_confirm_vs_release_race_exactly_one_wins(self):
        """确认与撤销并发：状态机保证只有一个生效，资金不多不少。"""
        for _ in range(20):
            s = seeded_store()
            no = s.freeze("ORD1", "30.00", None, "", "k-x")["refund"]["refund_no"]
            outcomes = []

            def act(fn):
                try:
                    outcomes.append((fn.__name__, fn(no)["changed"]))
                except ApiError:
                    outcomes.append((fn.__name__, "conflict"))

            t1 = threading.Thread(target=act, args=(s.confirm,))
            t2 = threading.Thread(target=act, args=(s.release,))
            t1.start(); t2.start(); t1.join(); t2.join()

            r = [x for x in s.state()["refunds"] if x["refund_no"] == no][0]
            self.assertIn(r["status"], ("CONFIRMED", "RELEASED"))
            o = available(s)
            if r["status"] == "CONFIRMED":
                self.assertEqual((o["frozen_minor"], o["confirmed_minor"]), (0, 3000))
            else:
                self.assertEqual((o["frozen_minor"], o["confirmed_minor"]), (0, 0))
                self.assertEqual(o["available_minor"], 10000)
            self.assertTrue(s.verify()["ok"])

    def test_mixed_storm_invariants_hold(self):
        """冻结/确认/撤销/冲正混合风暴后：账务核对必须通过。"""
        s = seeded_store()
        errors = []

        def worker(i):
            try:
                no = s.freeze("ORD1", "5.00", None, "", f"storm-{i}")["refund"]["refund_no"]
                if i % 3 == 0:
                    s.confirm(no)
                    if i % 6 == 0:
                        s.reverse(no)
                elif i % 3 == 1:
                    s.release(no)
                # i % 3 == 2: 保持冻结
            except ApiError as e:
                if e.code != "INSUFFICIENT_BALANCE":
                    errors.append(e)

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(worker, range(60)))

        self.assertEqual(errors, [])
        o = available(s)
        self.assertGreaterEqual(o["available_minor"], 0)
        self.assertGreaterEqual(o["frozen_minor"], 0)
        self.assertGreaterEqual(o["confirmed_minor"], 0)
        self.assertLessEqual(o["frozen_minor"] + o["confirmed_minor"], o["pool_minor"])
        v = s.verify()
        self.assertTrue(v["ok"], v["issues"])


if __name__ == "__main__":
    unittest.main()
