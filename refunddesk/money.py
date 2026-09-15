"""金额与汇率工具。

全局约定（精度与舍入规则，全系统唯一入口）：
  * 所有金额在库内一律以「最小货币单位」的整数存储（SCALE=2，即“分”），
    任何中间计算不使用浮点数。
  * 十进制字符串 -> 分：Decimal 解析后按 ROUND_HALF_UP 落到分。
  * 跨币种折算：以订单成交汇率 fx_rate（1 单位订单币种 = fx_rate 单位基准币）
    做乘/除，结果按 ROUND_HALF_UP 落到分，单次换算单次舍入。
  * 订单币种金额是退款的权威金额；基准币金额由其派生。
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

BASE_CURRENCY = "CNY"
SCALE = 2
_FACTOR = Decimal(10) ** SCALE          # 100
_ONE_MINOR = Decimal(1)                 # 1 个最小单位
_DISPLAY_QUANT = Decimal(1) / _FACTOR   # 0.01，用于展示


class MoneyError(ValueError):
    """金额/汇率非法。"""


def _parse_decimal(text, field):
    try:
        d = Decimal(str(text).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise MoneyError(f"{field} 不是合法数字: {text!r}")
    if not d.is_finite():
        raise MoneyError(f"{field} 不是有限数字: {text!r}")
    return d


def to_minor(text, field="amount"):
    """'123.45' -> 12345。超过两位的部分按 HALF_UP 舍入（全局统一规则）。"""
    d = _parse_decimal(text, field)
    return int((d * _FACTOR).quantize(_ONE_MINOR, rounding=ROUND_HALF_UP))


def fmt(minor, currency=""):
    """分 -> 展示字符串 '123.45'。"""
    s = str((Decimal(int(minor)) / _FACTOR).quantize(_DISPLAY_QUANT, rounding=ROUND_HALF_UP))
    return f"{s} {currency}".strip()


def parse_rate(text, field="fx_rate"):
    """校验并规范化汇率（保留原始十进制精度，存字符串）。"""
    d = _parse_decimal(text, field)
    if d <= 0:
        raise MoneyError(f"{field} 必须为正数: {text!r}")
    return format(d, "f")


def fx_to_base(minor, rate):
    """订单币种（分） -> 基准币（分），HALF_UP。"""
    return int((Decimal(int(minor)) * _parse_decimal(rate, "fx_rate")).quantize(
        _ONE_MINOR, rounding=ROUND_HALF_UP))


def fx_from_base(base_minor, rate):
    """基准币（分） -> 订单币种（分），HALF_UP。"""
    return int((Decimal(int(base_minor)) / _parse_decimal(rate, "fx_rate")).quantize(
        _ONE_MINOR, rounding=ROUND_HALF_UP))
