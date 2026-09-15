# 退款清算台（离线版）

单机离线运行的退款清算系统：导入订单、支付流水与优惠分摊，计算每笔订单的可退余额；
支持整单 / 部分 / 多次退款，按**原支付方式与券份额**比例回冲；提交先**冻结额度**，
确认后**记账**，失败或撤销**原样释放**，已记账可**冲正**。全程保留资金明细与台账，
刷新/重启后状态完整恢复，可导出对账 CSV。

零第三方依赖（Python 3.8+ 标准库 + 原生前端），无需联网、无需安装任何包。

## 运行

```bash
python3 server.py            # 默认 http://127.0.0.1:8000 ，数据库 data/refund_desk.db
python3 server.py --port 9000 --db /path/to.db
```

浏览器打开后按页签走主流程：

1. **数据导入** — 粘贴三类 CSV 分别导入，或点「一键导入示例数据」。
2. **订单与退款** — 查看每单资金池 / 冻结中 / 已退 / 可退余额；选订单、输金额、
   选币种（订单币种或基准币 CNY）→ **试算**（预览回冲分摊，不落库）→ **提交冻结**。
3. **退款单** — 对已冻结单 **确认记账** 或 **撤销释放**；对已记账单 **冲正**。
4. **资金台账** — 只增不改的分录流水（冻结/记账/释放/冲正），含操作后可退余额。
5. **对账与导出** — 「核对账务」重算校验全部不变量；导出对账单 / 退款明细 / 台账 CSV。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：金额与汇率舍入、分摊精确性与容量上限、生命周期与非法迁移、幂等重放、
20 线程并发冻结不超退、同键并发只落一单、确认/撤销竞争唯一胜者、混合风暴后
账务核对通过、HTTP 端到端（含导出与重启持久化）。

## 关键设计

### 金额与汇率（`refunddesk/money.py`）
- 一切金额以**最小货币单位整数（分）**存储与计算，全程无浮点。
- 文本→分、跨币种折算统一 `ROUND_HALF_UP`，单次换算单次舍入，规则全局唯一。
- 订单币种金额是退款权威金额；基准币金额由其按**订单成交汇率**（导入时锁定）派生。
- 明细行基准币各自折算后，**舍入差并入金额最大行**，保证 `Σ明细 = 单据合计`。

### 回冲分摊（`refunddesk/split.py`）
- 资金来源 = 各支付流水 + 各优惠分摊；按原始金额占比分摊退款额。
- `Fraction` 精确取整 + **最大余数法**补到分：`Σ分摊 = 退款额`，分毫不差。
- 每个来源有剩余容量（原始份额 − 已冻结/已退的累计回冲），分摊**不超过容量**，
  触顶来源退出后余额在其余来源间继续按比例分配 —— 来源级永不超退。
- 完全确定性：同输入必同输出。

### 并发与幂等（`refunddesk/store.py`）
- 写操作：`BEGIN IMMEDIATE` 事务 + 进程内写锁，余额变动用**带守卫条件的 UPDATE**
  （`frozen + confirmed + ? <= pool`）由数据库保证不超退、不负值；CHECK 约束兜底。
- 状态机 `FROZEN → CONFIRMED / RELEASED`，`CONFIRMED → REVERSED`，迁移用条件
  UPDATE，并发下只有一个请求生效；重复操作幂等返回当前态。
- 退款单以**幂等键**唯一：同键同参重放返回原单（不重复冻结），同键不同参返回 409。
- 已存在退款的订单禁止变更资金流水与币种/汇率，防止底账漂移。

### 台账与核对
- `ledger` 只增不改：每次冻结/记账/释放/冲正按资金明细逐行落分录，记录来源、
  金额、基准币与操作后可退余额 —— 即冲正依据。
- `GET /api/verify` 由明细与台账**重算**每单余额并与物化余额比对，同时校验
  资金池守恒、来源级不超退、台账分录合计与单据一致。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/import/{orders,payments,discounts}` | 导入 CSV（`{"csv": "..."}`）或 `{"rows": [...]}` |
| POST | `/api/import/sample` | 导入 `samples/` 示例数据 |
| POST | `/api/reset` | 清空全部数据 |
| GET  | `/api/state` | 订单余额、退款单（含明细）、台账 |
| POST | `/api/trial` | 试算 `{order_no, amount, currency?}`，不落库 |
| POST | `/api/refunds` | 提交冻结，需 `Idempotency-Key` 头或 `idempotency_key` |
| POST | `/api/refunds/{no}/confirm` | 确认记账 |
| POST | `/api/refunds/{no}/release` | 撤销/失败，原样释放 |
| POST | `/api/refunds/{no}/reverse` | 冲正已记账退款 |
| GET  | `/api/verify` | 账务核对 |
| GET  | `/api/export/{reconciliation,refunds,ledger}.csv` | 导出对账文件 |

错误统一为 `{"error": {"code", "message"}}`，如 `INSUFFICIENT_BALANCE`(409)、
`IDEMPOTENCY_KEY_REUSE`(409)、`STATE_CONFLICT`(409)、`CURRENCY_UNSUPPORTED`(400)。

## CSV 格式

```csv
# orders.csv —— fx_rate：1 单位订单币种 = fx_rate 基准币(CNY)，导入时锁定
order_no,currency,fx_rate,total
ORD1001,CNY,1,1200.00

# payments.csv
payment_no,order_no,method,amount
PAY9001,ORD1001,银行卡,700.00

# discounts.csv
alloc_no,order_no,coupon_no,amount
DIS001,ORD1001,CPN-NEWUSER,100.00
```

约定：资金池 = 支付流水合计 + 优惠分摊合计；导入按单号幂等（重复导入不产生重复数据）。

## 目录

```
server.py            # HTTP 服务（路由/静态/CSV 解析），仅标准库
refunddesk/
  money.py           # 金额、汇率、舍入规则
  split.py           # 按比例带上限的回冲分摊
  store.py           # SQLite 模式、事务、状态机、台账、核对
static/              # 单页前端（原生 JS，无外部依赖）
samples/             # 示例 CSV（多币种、多支付方式、含优惠券）
tests/               # 单元 / 并发 / 端到端测试
data/                # SQLite 数据库（运行时生成）
```
