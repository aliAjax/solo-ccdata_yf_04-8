/* 退款清算台前端（原生 JS，无外部依赖）。
   金额一律以后端返回的整数“分”渲染；提交冻结使用幂等键，重试/双击不产生重复单。 */
"use strict";

const S = { orders: [], refunds: [], ledger: [], base: "CNY" };
let idemKey = null; // 当前退款表单的幂等键：试算时生成，成功后作废

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const fmt = (minor, cur = "") =>
  (Number(minor) / 100).toLocaleString("zh-CN",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + (cur ? " " + cur : "");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const uuid = () => (crypto.randomUUID
  ? crypto.randomUUID()
  : "k-" + Date.now() + "-" + Math.random().toString(36).slice(2));

function toast(msg, ok = true) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + (ok ? "ok" : "err");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 3800);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = data.error || {};
    throw new Error(`${e.code || res.status}: ${e.message || "请求失败"}`);
  }
  return data;
}

async function refresh() {
  const st = await api("/api/state");
  S.orders = st.orders;
  S.refunds = st.refunds;
  S.ledger = st.ledger;
  S.base = st.base_currency;
  renderAll();
}

/* ---------------- 渲染 ---------------- */
function renderAll() {
  renderOrders();
  renderOrderOptions();
  renderRefunds();
  renderLedger();
  renderRecon();
}

function renderOrders() {
  const tb = $("#ordersTable tbody");
  tb.innerHTML = S.orders.map((o) => `
    <tr data-order="${esc(o.order_no)}" class="${o.available_minor > 0 ? "" : "depleted"}">
      <td class="mono">${esc(o.order_no)}</td><td>${esc(o.currency)}</td>
      <td>${esc(o.fx_rate)}</td>
      <td class="num">${fmt(o.total_minor)}</td>
      <td class="num">${fmt(o.pool_minor)}</td>
      <td class="num warn">${fmt(o.frozen_minor)}</td>
      <td class="num">${fmt(o.confirmed_minor)}</td>
      <td class="num strong">${fmt(o.available_minor)}</td>
      <td class="num muted">${fmt(o.available_base_minor, S.base)}</td>
    </tr>`).join("") || `<tr><td colspan="9" class="empty">暂无订单，请先导入数据</td></tr>`;
  $$("#ordersTable tbody tr[data-order]").forEach((tr) =>
    tr.addEventListener("click", () => {
      $("#refOrder").value = tr.dataset.order;
      onFormChange();
      $("#refAmount").focus();
    }));
}

function renderOrderOptions() {
  const sel = $("#refOrder");
  const keep = sel.value;
  sel.innerHTML = `<option value="">选择订单</option>` + S.orders.map((o) =>
    `<option value="${esc(o.order_no)}" ${o.order_no === keep ? "selected" : ""}>`
    + `${esc(o.order_no)}（可退 ${fmt(o.available_minor, o.currency)}）</option>`).join("");
  const order = S.orders.find((o) => o.order_no === sel.value);
  const curSel = $("#refCurrency");
  const keepCur = curSel.value;
  const opts = order ? [...new Set([order.currency, S.base])] : [];
  curSel.innerHTML = opts.map((c) =>
    `<option ${c === keepCur ? "selected" : ""}>${c}</option>`).join("");
}

const STATUS_META = {
  FROZEN: ["已冻结", "frozen"],
  CONFIRMED: ["已记账", "confirmed"],
  RELEASED: ["已释放", "released"],
  REVERSED: ["已冲正", "reversed"],
};

function renderRefunds() {
  const box = $("#refundList");
  if (!S.refunds.length) {
    box.innerHTML = `<div class="empty">暂无退款单</div>`;
    return;
  }
  box.innerHTML = S.refunds.map((r) => {
    const [label, cls] = STATUS_META[r.status] || [r.status, ""];
    const order = S.orders.find((o) => o.order_no === r.order_no);
    const cur = order ? order.currency : r.req_currency;
    const actions =
      r.status === "FROZEN"
        ? `<button class="btn small primary" data-act="confirm" data-no="${r.refund_no}">确认记账</button>
           <button class="btn small" data-act="release" data-no="${r.refund_no}">撤销释放</button>`
        : r.status === "CONFIRMED"
        ? `<button class="btn small danger" data-act="reverse" data-no="${r.refund_no}">冲正</button>`
        : "";
    const lines = r.lines.map((ln) => `
      <tr>
        <td>${ln.source_type === "PAYMENT" ? "支付" : "优惠"}</td>
        <td>${esc(ln.method)}</td>
        <td class="mono">${esc(ln.source_ref)}</td>
        <td class="num">${fmt(ln.amount_minor, cur)}</td>
        <td class="num muted">${fmt(ln.amount_base_minor, S.base)}</td>
      </tr>`).join("");
    return `
    <div class="card refund-card">
      <div class="refund-head">
        <span class="mono strong">${esc(r.refund_no)}</span>
        <span class="chip ${cls}">${label}</span>
        <span class="muted">订单 ${esc(r.order_no)}</span>
        <span class="grow"></span>
        <span class="strong big">${fmt(r.amount_minor, cur)}</span>
        <span class="muted">≈ ${fmt(r.amount_base_minor, S.base)}</span>
      </div>
      <div class="refund-meta muted">
        ${esc(r.created_at)} · 请求 ${esc(r.req_amount)} ${esc(r.req_currency)}
        ${r.reason ? " · " + esc(r.reason) : ""}${r.note ? " · " + esc(r.note) : ""}
        · 幂等键 <span class="mono">${esc(r.idempotency_key.slice(0, 8))}…</span>
      </div>
      <table class="lines">
        <thead><tr><th>来源</th><th>方式</th><th>来源单号</th>
          <th class="num">回冲金额</th><th class="num">基准币</th></tr></thead>
        <tbody>${lines}</tbody>
      </table>
      <div class="refund-actions">${actions}</div>
    </div>`;
  }).join("");
  $$("#refundList [data-act]").forEach((b) =>
    b.addEventListener("click", () => doRefundAction(b.dataset.act, b.dataset.no)));
}

const ACTION_LABEL = { FREEZE: "冻结", CONFIRM: "记账", RELEASE: "释放", REVERSE: "冲正" };

function renderLedger() {
  const tb = $("#ledgerTable tbody");
  tb.innerHTML = S.ledger.map((e) => `
    <tr>
      <td class="muted">${e.entry_no}</td>
      <td class="muted nowrap">${esc(e.created_at)}</td>
      <td class="mono">${esc(e.refund_no)}</td>
      <td class="mono">${esc(e.order_no)}</td>
      <td><span class="chip act-${e.action.toLowerCase()}">${ACTION_LABEL[e.action] || e.action}</span></td>
      <td>${e.source_type === "PAYMENT" ? "支付" : "优惠"}</td>
      <td>${esc(e.method)}</td>
      <td class="num">${fmt(e.amount_minor)}</td>
      <td class="num muted">${fmt(e.amount_base_minor, S.base)}</td>
      <td class="num">${fmt(e.available_after_minor)}</td>
      <td class="muted">${esc(e.note)}</td>
    </tr>`).join("") || `<tr><td colspan="11" class="empty">暂无台账记录</td></tr>`;
}

function renderRecon() {
  const tb = $("#reconTable tbody");
  tb.innerHTML = S.orders.map((o) => `
    <tr>
      <td class="mono">${esc(o.order_no)}</td>
      <td>${esc(o.currency)}</td><td>${esc(o.fx_rate)}</td>
      <td class="num">${fmt(o.total_minor)}</td>
      <td class="num">${fmt(o.pool_minor)}</td>
      <td class="num warn">${fmt(o.frozen_minor)}</td>
      <td class="num">${fmt(o.confirmed_minor)}</td>
      <td class="num strong">${fmt(o.available_minor)}</td>
      <td class="num muted">${fmt(o.pool_base_minor, S.base)}</td>
      <td class="num muted">${fmt(o.confirmed_base_minor, S.base)}</td>
    </tr>`).join("") || `<tr><td colspan="10" class="empty">暂无数据</td></tr>`;
}

/* ---------------- 交互 ---------------- */
function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
}

function onFormChange() {
  idemKey = null; // 表单变化后必须重新试算、重新生成幂等键
  $("#btnFreeze").disabled = true;
  $("#trialResult").classList.add("hidden");
}

async function doTrial() {
  const order_no = $("#refOrder").value;
  const amount = $("#refAmount").value.trim();
  const currency = $("#refCurrency").value;
  if (!order_no || !amount) return toast("请选择订单并输入金额", false);
  try {
    const t = await api("/api/trial", {
      method: "POST",
      body: JSON.stringify({ order_no, amount, currency }),
    });
    idemKey = uuid();
    $("#btnFreeze").disabled = false;
    const rows = t.lines.map((ln) => `
      <tr><td>${ln.source_type === "PAYMENT" ? "支付" : "优惠"}</td>
        <td>${esc(ln.method)}</td><td class="mono">${esc(ln.source_ref)}</td>
        <td class="num">${fmt(ln.amount_minor, t.currency)}</td>
        <td class="num muted">${fmt(ln.amount_base_minor, S.base)}</td></tr>`).join("");
    const box = $("#trialResult");
    box.className = "result-box";
    box.innerHTML = `
      <div>试算通过：退款 <b>${fmt(t.amount_minor, t.currency)}</b>
        （≈ ${fmt(t.amount_base_minor, S.base)}，按成交汇率 ${esc(t.fx_rate)} 折算），
        当前可退 ${fmt(t.balance.available_minor, t.currency)}</div>
      <table class="lines">
        <thead><tr><th>来源</th><th>方式</th><th>来源单号</th>
          <th class="num">回冲金额</th><th class="num">基准币</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    const box = $("#trialResult");
    box.className = "result-box err";
    box.textContent = "试算失败：" + e.message;
    $("#btnFreeze").disabled = true;
  }
}

async function doFreeze() {
  if (!idemKey) return toast("请先试算", false);
  const btn = $("#btnFreeze");
  btn.disabled = true; // 防连击；幂等键保证重试/双击/刷新重发不产生重复冻结
  try {
    const r = await api("/api/refunds", {
      method: "POST",
      headers: { "Idempotency-Key": idemKey },
      body: JSON.stringify({
        order_no: $("#refOrder").value,
        amount: $("#refAmount").value.trim(),
        currency: $("#refCurrency").value,
        reason: $("#refReason").value.trim(),
        idempotency_key: idemKey,
      }),
    });
    toast(r.created
      ? `已冻结 ${r.refund.refund_no}，等待确认记账`
      : `重复请求已去重，返回原单 ${r.refund.refund_no}`);
    idemKey = null;
    $("#trialResult").classList.add("hidden");
    $("#refAmount").value = "";
    $("#refReason").value = "";
    await refresh();
    switchTab("refunds");
  } catch (e) {
    toast("提交失败：" + e.message, false);
    btn.disabled = false; // 同一幂等键可安全重试
  }
}

async function doRefundAction(act, refundNo) {
  const label = { confirm: "确认记账", release: "撤销释放", reverse: "冲正" }[act];
  if (act === "reverse" &&
      !confirm(`确认对 ${refundNo} 做冲正？资金将回冲至可退余额。`)) return;
  try {
    const r = await api(`/api/refunds/${refundNo}/${act}`, {
      method: "POST", body: JSON.stringify({}),
    });
    toast(`${label}${r.changed === false ? "（重复操作，状态未变）" : "成功"}`);
    await refresh();
  } catch (e) {
    toast(`${label}失败：` + e.message, false);
  }
}

async function doVerify() {
  try {
    const v = await api("/api/verify");
    const badge = $("#verifyBadge");
    badge.textContent = v.ok ? "账务一致 ✓" : `${v.issues.length} 处不一致`;
    badge.className = "badge " + (v.ok ? "ok" : "err");
    const box = $("#verifyResult");
    box.className = "result-box " + (v.ok ? "" : "err");
    box.innerHTML = v.ok
      ? `核对通过（${esc(v.checked_at)}）：资金池、冻结、已退与台账逐单一致，无超退、无负余额。`
      : "<b>核对不一致：</b><br>" + v.issues.map(esc).join("<br>");
    switchTab("recon");
  } catch (e) {
    toast("核对失败：" + e.message, false);
  }
}

async function doImport(kind) {
  const map = { orders: "#csvOrders", payments: "#csvPayments", discounts: "#csvDiscounts" };
  const csv = $(map[kind]).value.trim();
  if (!csv) return toast("请先粘贴 CSV 内容", false);
  const box = $("#importResult");
  try {
    const r = await api(`/api/import/${kind}`, {
      method: "POST", body: JSON.stringify({ csv }),
    });
    box.className = "result-box";
    box.textContent = `导入成功：${r.imported} 行`;
    await refresh();
  } catch (e) {
    box.className = "result-box err";
    box.textContent = "导入失败：" + e.message;
  }
}

/* ---------------- 启动 ---------------- */
function main() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#btnRefresh").addEventListener("click", () => refresh().then(() => toast("已刷新")));
  $("#btnVerify").addEventListener("click", doVerify);
  $("#btnVerify2").addEventListener("click", doVerify);
  $("#btnTrial").addEventListener("click", doTrial);
  $("#btnFreeze").addEventListener("click", doFreeze);
  ["#refOrder", "#refAmount", "#refCurrency", "#refReason"].forEach((s) =>
    $(s).addEventListener("input", onFormChange));
  $$("[data-import]").forEach((b) =>
    b.addEventListener("click", () => doImport(b.dataset.import)));
  $("#btnSample").addEventListener("click", async () => {
    try {
      await api("/api/import/sample", { method: "POST", body: "{}" });
      toast("示例数据已导入");
      await refresh();
      switchTab("orders");
    } catch (e) { toast(e.message, false); }
  });
  $("#btnReset").addEventListener("click", async () => {
    if (!confirm("确认清空全部数据？此操作不可恢复。")) return;
    await api("/api/reset", { method: "POST", body: "{}" });
    toast("已清空");
    await refresh();
  });
  refresh().catch((e) => toast("加载失败：" + e.message, false));
}

document.addEventListener("DOMContentLoaded", main);
