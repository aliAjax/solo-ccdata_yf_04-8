"""退款金额在「原始资金来源」上的分摊算法。

规则（按原支付方式与券份额回冲）：
  * 每个资金来源（一笔支付流水 / 一条优惠分摊）有原始权重 weight 与
    剩余可退容量 remaining（已扣除此前冻结中/已确认的回冲份额）。
  * 退款额 amount 按权重比例分摊，使用 Fraction 精确取整 + 最大余数法
    补齐到分，保证 sum(分摊结果) == amount，分毫不差。
  * 任何来源分摊额不超过其 remaining（不会把某个支付方式退成负数）；
    触顶的来源退出，余额在剩余来源间继续按比例分配，迭代至分完。
  * 完全确定性：同样的输入永远得到同样的结果（平局按 key 字典序）。
"""
from fractions import Fraction


class SplitError(ValueError):
    """分摊失败（容量不足等）。"""


def proportional_split(amount, sources):
    """amount: int(分)；sources: [{'key', 'weight', 'remaining'}, ...]
    返回 {key: 分摊额(分)}，键覆盖 remaining>0 且被分到的来源。"""
    if amount < 0:
        raise SplitError("分摊金额不能为负")
    pool = [{"key": s["key"], "w": int(s["weight"]), "cap": int(s["remaining"])}
            for s in sources]
    if amount == 0:
        return {}
    if sum(p["cap"] for p in pool) < amount:
        raise SplitError(f"资金来源剩余容量不足：需 {amount}，余 {sum(p['cap'] for p in pool)}")

    result = {}
    left = amount
    while left > 0:
        active = [p for p in pool if p["cap"] > 0]
        if not active:
            raise SplitError("资金来源容量耗尽，无法完成分摊")
        total_w = sum(p["w"] for p in active)
        if total_w <= 0:
            raise SplitError("有效资金来源权重为 0")

        # 精确比例下取整，记录小数余量
        fracs = []
        used = 0
        for p in active:
            raw = Fraction(left * p["w"], total_w)
            floor = raw.numerator // raw.denominator
            p["take"] = min(floor, p["cap"])
            used += p["take"]
            fracs.append((raw - floor, p))

        # 最大余数法把剩余的分补齐（不超过各自容量）
        rem = left - used
        fracs.sort(key=lambda t: (-t[0], -t[1]["cap"], str(t[1]["key"])))
        for _frac, p in fracs:
            if rem <= 0:
                break
            if p["take"] < p["cap"]:
                p["take"] += 1
                rem -= 1

        for p in active:
            if p["take"]:
                result[p["key"]] = result.get(p["key"], 0) + p["take"]
                p["cap"] -= p["take"]
        left = rem

    return result
