import unittest

from refunddesk.split import proportional_split, SplitError


def src(key, weight, remaining=None):
    return {"key": key, "weight": weight,
            "remaining": weight if remaining is None else remaining}


class TestSplit(unittest.TestCase):
    def test_proportional_exact(self):
        # 700/400/100 的资金构成退 1200 -> 原样退回
        r = proportional_split(120000, [src("a", 70000), src("b", 40000), src("c", 10000)])
        self.assertEqual(r, {"a": 70000, "b": 40000, "c": 10000})
        # 退一半 -> 严格按比例
        r = proportional_split(60000, [src("a", 70000), src("b", 40000), src("c", 10000)])
        self.assertEqual(r, {"a": 35000, "b": 20000, "c": 5000})

    def test_sum_always_exact_with_remainder(self):
        # 除不尽时最大余数法补齐，合计必须分毫不差（容量充足、只验比例取整）
        big = 10**12
        for amount in (1, 2, 3, 7, 100, 9999, 123456789):
            r = proportional_split(amount, [src("a", 1, big), src("b", 1, big),
                                            src("c", 1, big)])
            self.assertEqual(sum(r.values()), amount)
            self.assertTrue(all(v >= 0 for v in r.values()))

    def test_caps_respected_and_redistributed(self):
        # a 只剩 10 元容量，其余流向 b
        r = proportional_split(10000, [src("a", 5000, 1000), src("b", 5000, 100000)])
        self.assertEqual(r["a"], 1000)
        self.assertEqual(r["b"], 9000)
        self.assertEqual(sum(r.values()), 10000)

    def test_sequential_refunds_never_exceed_source(self):
        # 模拟多次退款：容量逐次消耗，任何来源累计不超过原始份额
        sources = [src("a", 70000), src("b", 40000), src("c", 10000)]
        used = {"a": 0, "b": 0, "c": 0}
        for amount in (30000, 50000, 20000, 20000):  # 合计 120000 = 池子上限
            for s in sources:
                s["remaining"] = s["weight"] - used[s["key"]]
            r = proportional_split(amount, sources)
            for k, v in r.items():
                used[k] += v
        self.assertEqual(used, {"a": 70000, "b": 40000, "c": 10000})

    def test_deterministic(self):
        big = 10**9
        s = [src("a", 333, big), src("b", 333, big), src("c", 334, big)]
        r1 = proportional_split(10001, [dict(x) for x in s])
        r2 = proportional_split(10001, [dict(x) for x in s])
        self.assertEqual(r1, r2)

    def test_insufficient_capacity_raises(self):
        with self.assertRaises(SplitError):
            proportional_split(1000, [src("a", 500, 400)])

    def test_zero_amount(self):
        self.assertEqual(proportional_split(0, [src("a", 100)]), {})


if __name__ == "__main__":
    unittest.main()
