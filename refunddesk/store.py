"""SQLite 存储与退款清算核心事务。

一致性设计：
  * 所有写操作在 BEGIN IMMEDIATE 事务内执行（库级写串行化），
    进程内再加一把 RLock，杜绝 SQLITE_BUSY 的边界情况。
  * 余额变动全部使用「带守卫条件的 UPDATE」（如
    frozen+confirmed+? <= pool），由数据库保证并发下不超退、不为负；
    另有 CHECK 约束兜底。
  * 退款单状态机：FROZEN(已冻结) -> CONFIRMED(已记账) / RELEASED(已释放)；
    CONFIRMED -> REVERSED(已冲正)。状态迁移用条件 UPDATE 实现，
    并发下只有一个请求能迁移成功，其余拿到幂等的当前态或 409。
  * 幂等：退款单以 idempotency_key 唯一；同键重放返回原单，
    同键不同载荷返回 409，不会重复冻结/重复扣减。
  * 台账 ledger 只增不改：FREEZE/CONFIRM/RELEASE/REVERSE 四类分录，
    每行记录资金来源、金额、基准币折算与操作后可退余额，构成冲正依据。
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import count

from . import money
from .split import proportional_split, SplitError

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  order_no      TEXT PRIMARY KEY,
  currency      TEXT NOT NULL,
  fx_rate       TEXT NOT NULL,
  total_minor   INTEGER NOT NULL CHECK(total_minor >= 0),
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments(
  payment_no    TEXT PRIMARY KEY,
  order_no      TEXT NOT NULL REFERENCES orders(order_no),
  method        TEXT NOT NULL,
  amount_minor  INTEGER NOT NULL CHECK(amount_minor > 0),
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discounts(
  alloc_no      TEXT PRIMARY KEY,
  order_no      TEXT NOT NULL REFERENCES orders(order_no),
  coupon_no     TEXT NOT NULL,
  amount_minor  INTEGER NOT NULL CHECK(amount_minor > 0),
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_balance(
  order_no        TEXT PRIMARY KEY REFERENCES orders(order_no),
  pool_minor      INTEGER NOT NULL CHECK(pool_minor >= 0),
  frozen_minor    INTEGER NOT NULL DEFAULT 0 CHECK(frozen_minor >= 0),
  confirmed_minor INTEGER NOT NULL DEFAULT 0 CHECK(confirmed_minor >= 0),
  CHECK(frozen_minor + confirmed_minor <= pool_minor)
);
CREATE TABLE IF NOT EXISTS counters(
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds(
  refund_no         TEXT PRIMARY KEY,
  order_no          TEXT NOT NULL REFERENCES orders(order_no),
  amount_minor      INTEGER NOT NULL CHECK(amount_minor > 0),
  amount_base_minor INTEGER NOT NULL,
  req_currency      TEXT NOT NULL,
  req_amount        TEXT NOT NULL,
  reason            TEXT NOT NULL DEFAULT '',
  note              TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL CHECK(status IN ('FROZEN','CONFIRMED','RELEASED','REVERSED')),
  idempotency_key   TEXT NOT NULL UNIQUE,
  request_json      TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(order_no);
CREATE TABLE IF NOT EXISTS refund_lines(
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  refund_no         TEXT NOT NULL REFERENCES refunds(refund_no),
  line_seq          INTEGER NOT NULL,
  source_type       TEXT NOT NULL CHECK(source_type IN ('PAYMENT','COUPON')),
  source_ref        TEXT NOT NULL,
  method            TEXT NOT NULL,
  amount_minor      INTEGER NOT NULL CHECK(amount_minor > 0),
  amount_base_minor INTEGER NOT NULL,
  UNIQUE(refund_no, line_seq)
);
CREATE TABLE IF NOT EXISTS ledger(
  entry_no            INTEGER PRIMARY KEY AUTOINCREMENT,
  refund_no           TEXT NOT NULL REFERENCES refunds(refund_no),
  order_no            TEXT NOT NULL,
  action              TEXT NOT NULL CHECK(action IN ('FREEZE','CONFIRM','RELEASE','REVERSE')),
  line_seq            INTEGER NOT NULL,
  source_type         TEXT NOT NULL,
  source_ref          TEXT NOT NULL,
  method              TEXT NOT NULL,
  amount_minor        INTEGER NOT NULL,
  amount_base_minor   INTEGER NOT NULL,
  available_after_minor INTEGER NOT NULL,
  note                TEXT NOT NULL DEFAULT '',
  created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_refund ON ledger(refund_no);
"""


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_MEM_SEQ = count(1)


class Store:
    def __init__(self, db_path):
        self._lock = threading.RLock()
        self._uri = db_path.startswith("file:")
        if db_path == ":memory:":
            # 共享缓存内存库：同一 Store 的连接共用一库（keeper 保活），
            # 不同 Store 用序号隔离，互不可见
            db_path = (f"file:refunddesk_{next(_MEM_SEQ)}"
                       "?mode=memory&cache=shared")
            self._uri = True
        self.db_path = db_path
        if not self._uri:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._keeper = self._connect() if "mode=memory" in db_path else None
        with self._connect() as c:
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO counters(name, value) VALUES('refund', 0)")

    # ---------- 连接与事务 ----------
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None,
                               uri=self._uri)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _tx(self):
        """写事务：进程锁 + BEGIN IMMEDIATE，双保险串行化。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def _read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ---------- 导入 ----------
    def reset(self):
        with self._tx() as c:
            for t in ("ledger", "refund_lines", "refunds", "order_balance",
                      "discounts", "payments", "orders"):
                c.execute(f"DELETE FROM {t}")
            c.execute("UPDATE counters SET value=0 WHERE name='refund'")

    def import_orders(self, rows):
        """rows: [{order_no, currency, fx_rate, total}]，全量校验后单事务写入。"""
        parsed = []
        for i, r in enumerate(rows, 1):
            try:
                order_no = str(r["order_no"]).strip()
                currency = str(r["currency"]).strip().upper()
                if not order_no or len(currency) != 3:
                    raise money.MoneyError("order_no 不能为空，currency 须为 3 位代码")
                parsed.append({
                    "order_no": order_no,
                    "currency": currency,
                    "fx_rate": money.parse_rate(r["fx_rate"]),
                    "total_minor": money.to_minor(r["total"], "total"),
                })
            except (KeyError, money.MoneyError) as e:
                raise ApiError(400, "IMPORT_INVALID", f"orders 第 {i} 行: {e}")
        with self._tx() as c:
            for p in parsed:
                existing = c.execute("SELECT * FROM orders WHERE order_no=?",
                                     (p["order_no"],)).fetchone()
                if existing and (existing["currency"] != p["currency"]
                                 or existing["fx_rate"] != p["fx_rate"]):
                    if c.execute(
                            """SELECT 1 FROM refunds WHERE order_no=? AND
                               status IN ('FROZEN','CONFIRMED') LIMIT 1""",
                            (p["order_no"],)).fetchone():
                        raise ApiError(409, "ORDER_HAS_REFUNDS",
                                       f"订单 {p['order_no']} 已存在退款，禁止变更币种/汇率")
                c.execute(
                    """INSERT INTO orders(order_no, currency, fx_rate, total_minor, created_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(order_no) DO UPDATE SET
                         currency=excluded.currency, fx_rate=excluded.fx_rate,
                         total_minor=excluded.total_minor""",
                    (p["order_no"], p["currency"], p["fx_rate"], p["total_minor"], _now()))
                c.execute("INSERT OR IGNORE INTO order_balance(order_no, pool_minor) VALUES(?,0)",
                          (p["order_no"],))
            self._recompute_pools(c)
        return {"imported": len(parsed)}

    def import_payments(self, rows):
        return self._import_sources(rows, kind="PAYMENT")

    def import_discounts(self, rows):
        return self._import_sources(rows, kind="COUPON")

    def _import_sources(self, rows, kind):
        table = "payments" if kind == "PAYMENT" else "discounts"
        parsed = []
        for i, r in enumerate(rows, 1):
            try:
                if kind == "PAYMENT":
                    rec = {
                        "no": str(r["payment_no"]).strip(),
                        "order_no": str(r["order_no"]).strip(),
                        "method": str(r["method"]).strip(),
                        "amount_minor": money.to_minor(r["amount"], "amount"),
                    }
                else:
                    rec = {
                        "no": str(r["alloc_no"]).strip(),
                        "order_no": str(r["order_no"]).strip(),
                        "coupon_no": str(r["coupon_no"]).strip(),
                        "amount_minor": money.to_minor(r["amount"], "amount"),
                    }
                if not rec["no"] or not rec["order_no"]:
                    raise money.MoneyError("单号/订单号不能为空")
                if rec["amount_minor"] <= 0:
                    raise money.MoneyError("金额必须为正")
                parsed.append(rec)
            except (KeyError, money.MoneyError) as e:
                raise ApiError(400, "IMPORT_INVALID", f"{table} 第 {i} 行: {e}")
        with self._tx() as c:
            for p in parsed:
                if not c.execute("SELECT 1 FROM orders WHERE order_no=?",
                                 (p["order_no"],)).fetchone():
                    raise ApiError(400, "IMPORT_INVALID",
                                   f"{table} 引用了不存在的订单 {p['order_no']}")
                if c.execute(
                        """SELECT 1 FROM refunds WHERE order_no=? AND
                           status IN ('FROZEN','CONFIRMED') LIMIT 1""",
                        (p["order_no"],)).fetchone():
                    raise ApiError(409, "ORDER_HAS_REFUNDS",
                                   f"订单 {p['order_no']} 已存在退款，禁止变更资金流水")
                if kind == "PAYMENT":
                    c.execute(
                        """INSERT INTO payments(payment_no, order_no, method, amount_minor, created_at)
                           VALUES(?,?,?,?,?)
                           ON CONFLICT(payment_no) DO UPDATE SET
                             order_no=excluded.order_no, method=excluded.method,
                             amount_minor=excluded.amount_minor""",
                        (p["no"], p["order_no"], p["method"], p["amount_minor"], _now()))
                else:
                    c.execute(
                        """INSERT INTO discounts(alloc_no, order_no, coupon_no, amount_minor, created_at)
                           VALUES(?,?,?,?,?)
                           ON CONFLICT(alloc_no) DO UPDATE SET
                             order_no=excluded.order_no, coupon_no=excluded.coupon_no,
                             amount_minor=excluded.amount_minor""",
                        (p["no"], p["order_no"], p["coupon_no"], p["amount_minor"], _now()))
            self._recompute_pools(c)
        return {"imported": len(parsed)}

    def _recompute_pools(self, c):
        """资金池 = 支付流水合计 + 优惠分摊合计（冻结/已退额保持不变）。"""
        c.execute(
            """UPDATE order_balance SET pool_minor = COALESCE((
                   SELECT SUM(amount_minor) FROM payments p
                   WHERE p.order_no = order_balance.order_no), 0)
                 + COALESCE((
                   SELECT SUM(amount_minor) FROM discounts d
                   WHERE d.order_no = order_balance.order_no), 0)""")

    # ---------- 试算与分摊 ----------
    def _sources(self, c, order_no):
        """资金来源及剩余可退容量（扣除冻结中与已确认的回冲份额）。"""
        src = []
        for r in c.execute(
                "SELECT payment_no AS no, method, amount_minor FROM payments WHERE order_no=?",
                (order_no,)):
            src.append({"key": ("PAYMENT", r["no"]), "source_type": "PAYMENT",
                        "source_ref": r["no"], "method": r["method"],
                        "weight": r["amount_minor"]})
        for r in c.execute(
                "SELECT alloc_no AS no, coupon_no, amount_minor FROM discounts WHERE order_no=?",
                (order_no,)):
            src.append({"key": ("COUPON", r["no"]), "source_type": "COUPON",
                        "source_ref": r["no"], "method": f"优惠券({r['coupon_no']})",
                        "weight": r["amount_minor"]})
        used = {}
        for r in c.execute(
                """SELECT rl.source_type, rl.source_ref, SUM(rl.amount_minor) AS used
                   FROM refund_lines rl
                   JOIN refunds r ON r.refund_no = rl.refund_no
                   WHERE r.order_no=? AND r.status IN ('FROZEN','CONFIRMED')
                   GROUP BY rl.source_type, rl.source_ref""", (order_no,)):
            used[(r["source_type"], r["source_ref"])] = r["used"]
        for s in src:
            s["remaining"] = s["weight"] - used.get(s["key"], 0)
        return src

    def _build_lines(self, order, amount_minor, amount_base_minor, split_map, sources):
        """由分摊结果生成资金明细；单行基准币折算的舍入差并入金额最大行，
        保证 明细合计 == 单据合计（舍入规则全局一致）。"""
        by_key = {s["key"]: s for s in sources}
        picked = [by_key[k] for k in split_map]
        picked.sort(key=lambda s: (s["source_type"], s["source_ref"]))
        lines = []
        for seq, s in enumerate(picked, 1):
            amt = split_map[s["key"]]
            lines.append({
                "line_seq": seq,
                "source_type": s["source_type"],
                "source_ref": s["source_ref"],
                "method": s["method"],
                "amount_minor": amt,
                "amount_base_minor": money.fx_to_base(amt, order["fx_rate"]),
            })
        diff = amount_base_minor - sum(l["amount_base_minor"] for l in lines)
        if diff and lines:
            biggest = max(lines, key=lambda l: (l["amount_minor"], -l["line_seq"]))
            biggest["amount_base_minor"] += diff
        return lines

    def _resolve_amount(self, order, amount_text, currency):
        """把请求金额规范化为订单币种分（权威金额），并派生基准币金额。"""
        currency = (currency or order["currency"]).upper()
        if currency == order["currency"]:
            amount_minor = money.to_minor(amount_text, "amount")
        elif currency == money.BASE_CURRENCY and order["currency"] != money.BASE_CURRENCY:
            amount_minor = money.fx_from_base(money.to_minor(amount_text, "amount"),
                                              order["fx_rate"])
        else:
            raise ApiError(400, "CURRENCY_UNSUPPORTED",
                           f"仅支持以订单币种 {order['currency']} 或基准币 "
                           f"{money.BASE_CURRENCY} 发起退款")
        if amount_minor <= 0:
            raise ApiError(400, "AMOUNT_INVALID", "退款金额必须为正")
        return amount_minor, money.fx_to_base(amount_minor, order["fx_rate"]), currency

    def trial(self, order_no, amount_text, currency=None):
        """试算：只读，不落库。实际提交时在事务内重算，可能与试算结果不同。"""
        with self._read() as c:
            order = self._get_order(c, order_no)
            amount_minor, amount_base, req_cur = self._resolve_amount(
                order, amount_text, currency)
            bal = self._get_balance(c, order_no)
            if amount_minor > bal["available_minor"]:
                raise ApiError(409, "INSUFFICIENT_BALANCE",
                               f"可退余额不足：可退 {money.fmt(bal['available_minor'], order['currency'])}，"
                               f"请求 {money.fmt(amount_minor, order['currency'])}")
            sources = self._sources(c, order_no)
            try:
                split_map = proportional_split(amount_minor, sources)
            except SplitError as e:
                raise ApiError(409, "SPLIT_FAILED", str(e))
            lines = self._build_lines(order, amount_minor, amount_base, split_map, sources)
            return {
                "order_no": order_no, "currency": order["currency"],
                "req_currency": req_cur, "amount_minor": amount_minor,
                "amount_base_minor": amount_base,
                "fx_rate": order["fx_rate"], "balance": bal, "lines": lines,
            }

    # ---------- 退款生命周期 ----------
    def freeze(self, order_no, amount_text, currency, reason, idem_key):
        """提交退款：冻结额度 + 生成资金明细 + FREEZE 台账，单事务完成。"""
        if not idem_key:
            raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "缺少幂等键")
        with self._tx() as c:
            dup = c.execute("SELECT * FROM refunds WHERE idempotency_key=?",
                            (idem_key,)).fetchone()
            if dup:  # 幂等重放：同载荷返回原单，不同载荷报冲突，绝不重复冻结
                req_fingerprint = json.dumps(
                    {"order_no": dup["order_no"], "amount_minor": dup["amount_minor"],
                     "req_currency": dup["req_currency"], "reason": dup["reason"]},
                    sort_keys=True)
                order = self._get_order(c, order_no)
                amount_minor, _, req_cur = self._resolve_amount(order, amount_text, currency)
                new_fingerprint = json.dumps(
                    {"order_no": order_no, "amount_minor": amount_minor,
                     "req_currency": req_cur, "reason": reason or ""},
                    sort_keys=True)
                if req_fingerprint != new_fingerprint:
                    raise ApiError(409, "IDEMPOTENCY_KEY_REUSE",
                                   "幂等键已被不同参数的退款占用")
                return {"refund": self._refund_dict(c, dup["refund_no"]), "created": False}

            order = self._get_order(c, order_no)
            amount_minor, amount_base, req_cur = self._resolve_amount(
                order, amount_text, currency)
            # 守卫式冻结：并发下也不会让 frozen+confirmed 越过资金池
            cur = c.execute(
                """UPDATE order_balance SET frozen_minor = frozen_minor + ?
                   WHERE order_no=? AND frozen_minor + confirmed_minor + ? <= pool_minor""",
                (amount_minor, order_no, amount_minor))
            if cur.rowcount == 0:
                bal = self._get_balance(c, order_no)
                raise ApiError(409, "INSUFFICIENT_BALANCE",
                               f"可退余额不足：可退 {money.fmt(bal['available_minor'], order['currency'])}，"
                               f"请求 {money.fmt(amount_minor, order['currency'])}")

            sources = self._sources(c, order_no)
            try:
                split_map = proportional_split(amount_minor, sources)
            except SplitError as e:
                raise ApiError(409, "SPLIT_FAILED", str(e))
            lines = self._build_lines(order, amount_minor, amount_base, split_map, sources)

            refund_no = self._next_refund_no(c)
            now = _now()
            request_json = json.dumps(
                {"order_no": order_no, "amount": str(amount_text),
                 "currency": req_cur, "reason": reason or ""},
                ensure_ascii=False, sort_keys=True)
            c.execute(
                """INSERT INTO refunds(refund_no, order_no, amount_minor, amount_base_minor,
                     req_currency, req_amount, reason, status, idempotency_key,
                     request_json, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (refund_no, order_no, amount_minor, amount_base, req_cur,
                 str(amount_text), reason or "", "FROZEN", idem_key,
                 request_json, now, now))
            for ln in lines:
                c.execute(
                    """INSERT INTO refund_lines(refund_no, line_seq, source_type, source_ref,
                         method, amount_minor, amount_base_minor)
                       VALUES(?,?,?,?,?,?,?)""",
                    (refund_no, ln["line_seq"], ln["source_type"], ln["source_ref"],
                     ln["method"], ln["amount_minor"], ln["amount_base_minor"]))
            self._add_ledger(c, refund_no, order_no, "FREEZE", lines, "冻结退款额度")
            return {"refund": self._refund_dict(c, refund_no), "created": True}

    def confirm(self, refund_no):
        """确认记账：FROZEN -> CONFIRMED，冻结额转为已退额，写 CONFIRM 台账。"""
        with self._tx() as c:
            row = self._get_refund(c, refund_no)
            cur = c.execute(
                "UPDATE refunds SET status='CONFIRMED', updated_at=? "
                "WHERE refund_no=? AND status='FROZEN'", (_now(), refund_no))
            if cur.rowcount == 0:
                if row["status"] == "CONFIRMED":
                    return {"refund": self._refund_dict(c, refund_no), "changed": False}
                raise ApiError(409, "STATE_CONFLICT",
                               f"退款单当前状态为 {row['status']}，不能确认记账")
            c.execute(
                """UPDATE order_balance SET frozen_minor = frozen_minor - ?,
                     confirmed_minor = confirmed_minor + ? WHERE order_no=?""",
                (row["amount_minor"], row["amount_minor"], row["order_no"]))
            self._add_ledger(c, refund_no, row["order_no"], "CONFIRM",
                             self._lines_of(c, refund_no), "确认记账，资金出账")
            return {"refund": self._refund_dict(c, refund_no), "changed": True}

    def release(self, refund_no, note=""):
        """撤销/失败：FROZEN -> RELEASED，按冻结时的明细原样释放额度。"""
        with self._tx() as c:
            row = self._get_refund(c, refund_no)
            cur = c.execute(
                "UPDATE refunds SET status='RELEASED', note=?, updated_at=? "
                "WHERE refund_no=? AND status='FROZEN'",
                (note or "撤销冻结", _now(), refund_no))
            if cur.rowcount == 0:
                if row["status"] == "RELEASED":
                    return {"refund": self._refund_dict(c, refund_no), "changed": False}
                raise ApiError(409, "STATE_CONFLICT",
                               f"退款单当前状态为 {row['status']}，不能撤销")
            c.execute(
                "UPDATE order_balance SET frozen_minor = frozen_minor - ? WHERE order_no=?",
                (row["amount_minor"], row["order_no"]))
            self._add_ledger(c, refund_no, row["order_no"], "RELEASE",
                             self._lines_of(c, refund_no),
                             note or "撤销冻结，额度原样释放")
            return {"refund": self._refund_dict(c, refund_no), "changed": True}

    def reverse(self, refund_no, note=""):
        """冲正：CONFIRMED -> REVERSED，已退额回冲，写 REVERSE 台账（冲正依据）。"""
        with self._tx() as c:
            row = self._get_refund(c, refund_no)
            cur = c.execute(
                "UPDATE refunds SET status='REVERSED', note=?, updated_at=? "
                "WHERE refund_no=? AND status='CONFIRMED'",
                (note or "冲正", _now(), refund_no))
            if cur.rowcount == 0:
                if row["status"] == "REVERSED":
                    return {"refund": self._refund_dict(c, refund_no), "changed": False}
                raise ApiError(409, "STATE_CONFLICT",
                               f"退款单当前状态为 {row['status']}，不能冲正")
            c.execute(
                "UPDATE order_balance SET confirmed_minor = confirmed_minor - ? "
                "WHERE order_no=?", (row["amount_minor"], row["order_no"]))
            self._add_ledger(c, refund_no, row["order_no"], "REVERSE",
                             self._lines_of(c, refund_no),
                             note or f"冲正退款单 {refund_no}，资金回冲")
            return {"refund": self._refund_dict(c, refund_no), "changed": True}

    # ---------- 查询 / 核对 ----------
    def state(self):
        with self._read() as c:
            orders = []
            for o in c.execute(
                    """SELECT o.*, b.pool_minor, b.frozen_minor, b.confirmed_minor
                       FROM orders o JOIN order_balance b ON b.order_no=o.order_no
                       ORDER BY o.order_no"""):
                d = dict(o)
                d["available_minor"] = d["pool_minor"] - d["frozen_minor"] - d["confirmed_minor"]
                d["pool_base_minor"] = money.fx_to_base(d["pool_minor"], d["fx_rate"])
                d["confirmed_base_minor"] = money.fx_to_base(d["confirmed_minor"], d["fx_rate"])
                d["available_base_minor"] = money.fx_to_base(d["available_minor"], d["fx_rate"])
                d["total_base_minor"] = money.fx_to_base(d["total_minor"], d["fx_rate"])
                orders.append(d)
            refunds = [self._refund_dict(c, r["refund_no"]) for r in c.execute(
                "SELECT refund_no FROM refunds ORDER BY created_at DESC, refund_no DESC")]
            ledger = [dict(r) for r in c.execute(
                "SELECT * FROM ledger ORDER BY entry_no DESC LIMIT 500")]
            return {"base_currency": money.BASE_CURRENCY, "orders": orders,
                    "refunds": refunds, "ledger": ledger}

    def verify(self):
        """核对：由台账/明细重算余额，与物化余额逐单比对，并校验资金池。"""
        issues = []
        with self._read() as c:
            for o in c.execute(
                    """SELECT o.order_no, o.currency, b.pool_minor, b.frozen_minor,
                              b.confirmed_minor
                       FROM orders o JOIN order_balance b ON b.order_no=o.order_no"""):
                order_no = o["order_no"]
                pool = o["pool_minor"]
                src_sum = c.execute(
                    """SELECT (SELECT COALESCE(SUM(amount_minor),0) FROM payments WHERE order_no=?)
                            + (SELECT COALESCE(SUM(amount_minor),0) FROM discounts WHERE order_no=?)
                       AS s""", (order_no, order_no)).fetchone()["s"]
                if src_sum != pool:
                    issues.append(f"{order_no}: 资金池 {pool} ≠ 流水合计 {src_sum}")
                frozen = self._sum_by_status(c, order_no, "FROZEN")
                confirmed = self._sum_by_status(c, order_no, "CONFIRMED")
                if frozen != o["frozen_minor"]:
                    issues.append(f"{order_no}: 冻结额 {o['frozen_minor']} ≠ 明细重算 {frozen}")
                if confirmed != o["confirmed_minor"]:
                    issues.append(f"{order_no}: 已退额 {o['confirmed_minor']} ≠ 明细重算 {confirmed}")
                if o["frozen_minor"] + o["confirmed_minor"] > pool:
                    issues.append(f"{order_no}: 冻结+已退超过资金池（超退）")
                if o["frozen_minor"] < 0 or o["confirmed_minor"] < 0:
                    issues.append(f"{order_no}: 出现负余额")
                # 台账完整性：每个退款单在其状态对应动作上的分录合计须等于单据金额
                expected_actions = {
                    "FROZEN": ("FREEZE",), "CONFIRMED": ("FREEZE", "CONFIRM"),
                    "RELEASED": ("FREEZE", "RELEASE"),
                    "REVERSED": ("FREEZE", "CONFIRM", "REVERSE"),
                }
                for r in c.execute(
                        "SELECT refund_no, status, amount_minor FROM refunds WHERE order_no=?",
                        (order_no,)):
                    for action in expected_actions.get(r["status"], ()):
                        s = c.execute(
                            """SELECT COALESCE(SUM(amount_minor),0) AS s FROM ledger
                               WHERE refund_no=? AND action=?""",
                            (r["refund_no"], action)).fetchone()["s"]
                        if s != r["amount_minor"]:
                            issues.append(
                                f"{r['refund_no']}: {action} 台账合计 {s} ≠ 单据 "
                                f"{r['amount_minor']}")
                # 来源级防超退：任一支付/优惠来源的累计回冲不得超过其原始份额
                for src in c.execute(
                        """SELECT rl.source_type, rl.source_ref, SUM(rl.amount_minor) AS used
                           FROM refund_lines rl JOIN refunds r ON r.refund_no=rl.refund_no
                           WHERE r.order_no=? AND r.status IN ('FROZEN','CONFIRMED')
                           GROUP BY rl.source_type, rl.source_ref""", (order_no,)):
                    if src["source_type"] == "PAYMENT":
                        w = c.execute("SELECT amount_minor FROM payments WHERE payment_no=?",
                                      (src["source_ref"],)).fetchone()
                    else:
                        w = c.execute("SELECT amount_minor FROM discounts WHERE alloc_no=?",
                                      (src["source_ref"],)).fetchone()
                    if w and src["used"] > w["amount_minor"]:
                        issues.append(
                            f"{order_no}: 来源 {src['source_ref']} 累计回冲 {src['used']} "
                            f"超过原始份额 {w['amount_minor']}")
            return {"ok": not issues, "issues": issues,
                    "checked_at": _now()}

    def _sum_by_status(self, c, order_no, status):
        return c.execute(
            """SELECT COALESCE(SUM(rl.amount_minor),0) AS s
               FROM refund_lines rl JOIN refunds r ON r.refund_no=rl.refund_no
               WHERE r.order_no=? AND r.status=?""", (order_no, status)).fetchone()["s"]

    # ---------- 导出 ----------
    def reconciliation_rows(self):
        with self._read() as c:
            rows = []
            for o in c.execute(
                    """SELECT o.*, b.pool_minor, b.frozen_minor, b.confirmed_minor
                       FROM orders o JOIN order_balance b ON b.order_no=o.order_no
                       ORDER BY o.order_no"""):
                reversed_sum = c.execute(
                    """SELECT COALESCE(SUM(amount_minor),0) AS s FROM refunds
                       WHERE order_no=? AND status='REVERSED'""",
                    (o["order_no"],)).fetchone()["s"]
                avail = o["pool_minor"] - o["frozen_minor"] - o["confirmed_minor"]
                rows.append({
                    "order_no": o["order_no"], "currency": o["currency"],
                    "fx_rate": o["fx_rate"],
                    "order_total": money.fmt(o["total_minor"]),
                    "pool": money.fmt(o["pool_minor"]),
                    "frozen": money.fmt(o["frozen_minor"]),
                    "confirmed": money.fmt(o["confirmed_minor"]),
                    "reversed": money.fmt(reversed_sum),
                    "available": money.fmt(avail),
                    "pool_base": money.fmt(money.fx_to_base(o["pool_minor"], o["fx_rate"]),
                                           money.BASE_CURRENCY),
                    "confirmed_base": money.fmt(
                        money.fx_to_base(o["confirmed_minor"], o["fx_rate"]),
                        money.BASE_CURRENCY),
                    "available_base": money.fmt(
                        money.fx_to_base(avail, o["fx_rate"]), money.BASE_CURRENCY),
                })
            return rows

    def refund_export_rows(self):
        with self._read() as c:
            rows = []
            for r in c.execute(
                    """SELECT rl.*, r.order_no, r.status, r.reason, r.created_at AS r_created
                       FROM refund_lines rl JOIN refunds r ON r.refund_no=rl.refund_no
                       ORDER BY rl.refund_no, rl.line_seq"""):
                rows.append({
                    "refund_no": r["refund_no"], "order_no": r["order_no"],
                    "status": r["status"], "line_seq": r["line_seq"],
                    "source_type": r["source_type"], "source_ref": r["source_ref"],
                    "method": r["method"], "amount": money.fmt(r["amount_minor"]),
                    "amount_base": money.fmt(r["amount_base_minor"], money.BASE_CURRENCY),
                    "reason": r["reason"], "created_at": r["r_created"],
                })
            return rows

    def ledger_rows(self):
        with self._read() as c:
            return [{
                "entry_no": r["entry_no"], "refund_no": r["refund_no"],
                "order_no": r["order_no"], "action": r["action"],
                "line_seq": r["line_seq"], "source_type": r["source_type"],
                "source_ref": r["source_ref"], "method": r["method"],
                "amount": money.fmt(r["amount_minor"]),
                "amount_base": money.fmt(r["amount_base_minor"], money.BASE_CURRENCY),
                "available_after": money.fmt(r["available_after_minor"]),
                "note": r["note"], "created_at": r["created_at"],
            } for r in c.execute("SELECT * FROM ledger ORDER BY entry_no")]

    # ---------- 内部工具 ----------
    def _get_order(self, c, order_no):
        row = c.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        if not row:
            raise ApiError(404, "ORDER_NOT_FOUND", f"订单不存在: {order_no}")
        return row

    def _get_refund(self, c, refund_no):
        row = c.execute("SELECT * FROM refunds WHERE refund_no=?", (refund_no,)).fetchone()
        if not row:
            raise ApiError(404, "REFUND_NOT_FOUND", f"退款单不存在: {refund_no}")
        return row

    def _get_balance(self, c, order_no):
        row = c.execute("SELECT * FROM order_balance WHERE order_no=?",
                        (order_no,)).fetchone()
        if not row:
            raise ApiError(404, "ORDER_NOT_FOUND", f"订单不存在: {order_no}")
        return {
            "pool_minor": row["pool_minor"], "frozen_minor": row["frozen_minor"],
            "confirmed_minor": row["confirmed_minor"],
            "available_minor": row["pool_minor"] - row["frozen_minor"] - row["confirmed_minor"],
        }

    def _next_refund_no(self, c):
        c.execute("UPDATE counters SET value=value+1 WHERE name='refund'")
        v = c.execute("SELECT value FROM counters WHERE name='refund'").fetchone()["value"]
        return f"R{v:06d}"

    def _lines_of(self, c, refund_no):
        return [dict(r) for r in c.execute(
            "SELECT * FROM refund_lines WHERE refund_no=? ORDER BY line_seq",
            (refund_no,))]

    def _add_ledger(self, c, refund_no, order_no, action, lines, note):
        bal = self._get_balance(c, order_no)
        now = _now()
        for ln in lines:
            c.execute(
                """INSERT INTO ledger(refund_no, order_no, action, line_seq, source_type,
                     source_ref, method, amount_minor, amount_base_minor,
                     available_after_minor, note, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (refund_no, order_no, action, ln["line_seq"], ln["source_type"],
                 ln["source_ref"], ln["method"], ln["amount_minor"],
                 ln["amount_base_minor"], bal["available_minor"], note, now))

    def _refund_dict(self, c, refund_no):
        r = self._get_refund(c, refund_no)
        d = dict(r)
        d.pop("request_json", None)
        d["lines"] = self._lines_of(c, refund_no)
        return d
