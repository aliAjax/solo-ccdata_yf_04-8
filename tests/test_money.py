import unittest

from refunddesk import money
from refunddesk.money import MoneyError


class TestMoney(unittest.TestCase):
    def test_to_minor_basic(self):
        self.assertEqual(money.to_minor("123.45"), 12345)
        self.assertEqual(money.to_minor("0.01"), 1)
        self.assertEqual(money.to_minor("100"), 10000)
        self.assertEqual(money.to_minor(99.99), 9999)

    def test_to_minor_half_up(self):
        # 全局统一 ROUND_HALF_UP
        self.assertEqual(money.to_minor("0.005"), 1)
        self.assertEqual(money.to_minor("0.004"), 0)
        self.assertEqual(money.to_minor("99.999"), 10000)
        self.assertEqual(money.to_minor("2.675"), 268)  # Decimal 精确，无浮点误差

    def test_to_minor_invalid(self):
        with self.assertRaises(MoneyError):
            money.to_minor("abc")
        with self.assertRaises(MoneyError):
            money.to_minor("")

    def test_fmt(self):
        self.assertEqual(money.fmt(12345), "123.45")
        self.assertEqual(money.fmt(5), "0.05")
        self.assertEqual(money.fmt(10000, "CNY"), "100.00 CNY")

    def test_fx_half_up_consistent(self):
        # 1 分 JPY 按 0.048 折算 -> 0.048 分 -> HALF_UP 落 0；11 分 -> 0.528 -> 1
        self.assertEqual(money.fx_to_base(1, "0.048"), 0)
        self.assertEqual(money.fx_to_base(11, "0.048"), 1)
        # 100.00 USD @7.10 -> 710.00 CNY
        self.assertEqual(money.fx_to_base(10000, "7.10"), 71000)
        # 反向：710.00 CNY -> 100.00 USD
        self.assertEqual(money.fx_from_base(71000, "7.10"), 10000)

    def test_fx_rate_validation(self):
        with self.assertRaises(MoneyError):
            money.parse_rate("0")
        with self.assertRaises(MoneyError):
            money.parse_rate("-1.5")
        self.assertEqual(money.parse_rate("7.10"), "7.10")


if __name__ == "__main__":
    unittest.main()
