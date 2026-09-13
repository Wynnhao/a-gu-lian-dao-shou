/* ═══ A股镰刀手 · AI交易员看板 前端逻辑（原生 JS，无框架） ═══
   自动刷新：每 30s 刷新轻量接口（overview / signals / pending）；
   其余数据在切换标签页与手动刷新时拉取。所有 fetch 失败行内提示，不白屏。 */
"use strict";

/* ───────────────────────── 工具 ───────────────────────── */
function $(sel) { return document.querySelector(sel); }
function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtMoney(v, noSym) {
  if (v == null || isNaN(Number(v))) return "—";
  var s = Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return noSym ? s : "¥" + s;
}
function fmtPct(v, digits) {
  if (v == null || isNaN(Number(v))) return "—";
  var d = (digits == null ? 2 : digits);
  var n = Number(v);
  return (n > 0 ? "+" : "") + n.toFixed(d) + "%";
}
function fmtNum(v, d) {
  if (v == null || isNaN(Number(v))) return "—";
  return Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d });
}
function fmtTs(s) {
  if (!s) return "—";
  return String(s).replace("T", " ").slice(0, 19);
}
function clsSign(v) { return v > 0 ? "up" : (v < 0 ? "down" : "flat"); } /* A股惯例：红涨绿跌 */

async function fetchJSON(url, opts) {
  var resp;
  try {
    resp = await fetch(url, opts || {});
  } catch (e) {
    throw new Error("网络错误：" + e.message);
  }
  var data = null;
  try { data = await resp.json(); } catch (e) { /* 非 JSON */ }
  if (!resp.ok) {
    throw new Error((data && data.error) ? data.error : ("HTTP " + resp.status));
  }
  return data;
}

function showErr(id, msg) {
  var el = $("#" + id);
  if (!el) return;
  el.textContent = "⚠ " + msg;
  el.classList.remove("hidden");
}
function clearErr(id) {
  var el = $("#" + id);
  if (el) el.classList.add("hidden");
}
function emptyBox(msg) { return '<div class="empty">' + esc(msg) + "</div>"; }

/* 徽章 */
var STATUS_BADGE = {
  executed: ["executed", "已执行"], approved: ["approved", "已批准"],
  proposed: ["proposed", "待风控"], rejected: ["rejected", "已否决"],
  report_only: ["report_only", "仅报告"]
};
function badgeStatus(st) {
  var m = STATUS_BADGE[st] || [String(st || "未知"), String(st || "未知")];
  return '<span class="badge b-' + esc(m[0]) + '">' + esc(m[1]) + "</span>";
}
function badgeSide(side) {
  var buy = String(side).toLowerCase() === "buy";
  return '<span class="badge ' + (buy ? "b-buy" : "b-sell") + '">' + (buy ? "买入" : "卖出") + "</span>";
}

/* ───────────────────────── 全局状态 ───────────────────────── */
var S = {
  countdown: 30,
  overview: null,
  signals: [],
  pending: [],
  activeTab: "equity",
  loaded: {},          /* 各标签页是否已拉取过 */
  charts: {},          /* echarts 实例 */
  sigSelected: null,   /* 已选信号 code */
  newsMode: "market",
  decisions: [],
  sessions: []
};
var REFRESH_SEC = 30;

function chart(id) {
  if (!window.echarts) return null;
  var box = $("#" + id);
  if (!box) return null;
  if (!S.charts[id]) S.charts[id] = echarts.init(box);
  S.charts[id].resize();
  return S.charts[id];
}

/* ───────────────────────── 顶部 & 总览栏 ───────────────────────── */
async function refreshOverview() {
  clearErr("ov-error");
  try {
    var d = await fetchJSON("/api/overview");
    S.overview = d;
    renderOverview(d);
    renderBlacklist(d);
    renderPositions(d);
    return d;
  } catch (e) {
    showErr("ov-error", "总览加载失败：" + e.message);
    return null;
  }
}

function renderOverview(d) {
  $("#as-of").textContent = d.as_of || "—";
  $("#ov-total").textContent = fmtMoney(d.total);
  $("#ov-start").textContent = "期初 " + fmtMoney(d.start_cash);
  $("#ov-cash").textContent = fmtMoney(d.cash);
  var cashPct = d.total > 0 ? (d.cash / d.total * 100) : null;
  $("#ov-cash-pct").textContent = cashPct == null ? "—" : "占比 " + cashPct.toFixed(1) + "%";

  $("#ov-mv").textContent = fmtMoney(d.market_value);
  var npos = (d.positions || []).length;
  $("#ov-mv-count").textContent = npos ? (npos + " 只持仓") : "无持仓（空仓）";

  var ret = $("#ov-ret");
  ret.textContent = fmtPct(d.cum_return_pct);
  ret.className = "v " + clsSign(d.cum_return_pct);
  var ex = $("#ov-excess");
  var exv = d.excess_pct;
  ex.textContent = "vs 沪深300 " + (exv == null ? "—" : (exv > 0 ? "+" : "") + exv.toFixed(2) + "%");
  ex.className = "s " + clsSign(exv);

  var dd = $("#ov-dd");
  var ddp = (d.drawdown || 0) * 100;
  dd.textContent = ddp.toFixed(2) + "%";
  dd.className = "v " + (ddp > 0 ? "down" : "flat");
  $("#ov-kill").textContent = "kill switch: " + (d.kill_switch_active ? "已触发" : "正常");

  /* 状态灯：kill 红 > 健康黄 > 正常绿 */
  var light = $("#light"), lt = $("#light-text");
  light.className = "light";
  if (d.kill_switch_active) {
    light.classList.add("light-red");
    lt.textContent = "风控停机";
    light.title = "kill switch 已触发，禁止交易";
  } else if ((d.health_issues || []).length) {
    light.classList.add("light-yellow");
    lt.textContent = "数据告警 " + d.health_issues.length;
    light.title = d.health_issues.join("\n");
  } else {
    light.classList.add("light-green");
    lt.textContent = "运行正常";
    light.title = "数据与风控状态正常";
  }
}

function renderPositions(d) {
  var wrap = $("#pos-wrap");
  var pos = d.positions || [];
  $("#pos-summary").textContent = pos.length ? (pos.length + " 只") : "";
  if (!pos.length) {
    wrap.innerHTML = emptyBox("暂无持仓 —— position 表为空（P6 执行阶段未开始，当前为干净基线）");
    return;
  }
  var rows = pos.map(function (p) {
    var pnl = p.unrealized_pnl;
    var pnlPct = pnl != null && p.cost > 0 ? (pnl / (p.cost * p.shares) * 100) : null;
    return "<tr>" +
      '<td class="t">' + esc(p.code) + "</td>" +
      '<td class="t">' + esc(p.name) + "</td>" +
      '<td class="num">' + fmtNum(p.shares, 0) + "</td>" +
      '<td class="num">' + fmtNum(p.avail_shares, 0) + "</td>" +
      '<td class="num">' + fmtNum(p.cost) + "</td>" +
      '<td class="num">' + fmtNum(p.latest_price) + "</td>" +
      '<td class="num">' + fmtMoney(p.market_value) + "</td>" +
      '<td class="num ' + clsSign(pnl) + '">' + fmtMoney(pnl, true) + (pnlPct == null ? "" : " (" + fmtPct(pnlPct) + ")") + "</td>" +
      '<td class="num">' + (p.pct_of_total == null ? "—" : p.pct_of_total.toFixed(1) + "%") + "</td>" +
      '<td class="t">' + esc(p.latest_date || "—") + "</td>" +
      "</tr>";
  }).join("");
  wrap.innerHTML =
    '<div class="tbl-wrap"><table class="tbl"><thead><tr>' +
    "<th>代码</th><th>名称</th><th class=\"num\">持股</th><th class=\"num\">可卖</th><th class=\"num\">成本</th>" +
    '<th class="num">现价</th><th class="num">市值</th><th class="num">浮动盈亏</th><th class="num">占比</th><th>现价日期</th>' +
    "</tr></thead><tbody>" + rows + "</tbody></table></div>";
}

function renderBlacklist(d) {
  var bl = d.blacklist || [];
  var hi = d.health_issues || [];
  var blHtml;
  if (!bl.length) {
    blHtml = emptyBox("黑名单：stock_info 为空");
  } else {
    blHtml = '<div class="mini-h">黑名单（上市天数 / ST / 次新规则）</div><div class="tbl-wrap"><table class="tbl"><tbody>' +
      bl.map(function (b) {
        return '<tr><td class="t">' + esc(b.code) + "</td><td class=\"t\">" + esc(b.name) + "</td>" +
          '<td><span class="badge ' + (b.ok ? "b-pass" : "b-block") + '">' + (b.ok ? "PASS" : "BLOCK") + "</span></td>" +
          '<td class="t">' + esc(b.reason || "-") + "</td></tr>";
      }).join("") + "</tbody></table></div>";
  }
  var hiHtml;
  if (!hi.length) {
    hiHtml = '<div class="mini-h">数据健康</div>' + emptyBox("数据健康：OK（无告警）");
  } else {
    hiHtml = '<div class="mini-h">数据健康告警</div><ul class="gate-list warning">' +
      hi.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul>";
  }
  $("#blacklist-wrap").innerHTML = blHtml + "<div>" + hiHtml + "</div>";
}

/* ───────────────────────── 标签页 1：权益曲线 ───────────────────────── */
var EQ_CHART_BASE = {
  backgroundColor: "transparent",
  textStyle: { color: "#7d8fa5" },
  tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
  axisPointer: { link: [{ xAxisIndex: "all" }] }
};

async function refreshEquity() {
  clearErr("equity-err");
  try {
    var d = await fetchJSON("/api/equity_curve");
    renderEquity(d);
  } catch (e) {
    showErr("equity-err", "权益曲线加载失败：" + e.message);
    $("#equity-chart").classList.add("hidden");
    $("#equity-empty").classList.remove("hidden");
  }
}

function renderEquity(d) {
  var has = (d.dates || []).length > 0;
  $("#equity-chart").classList.toggle("hidden", !has);
  $("#equity-empty").classList.toggle("hidden", has);
  if (!has) return;
  var c = chart("equity-chart");
  if (!c) return;
  var ddNeg = (d.drawdown || []).map(function (v) { return -(v || 0); });
  c.setOption({
    backgroundColor: "transparent",
    textStyle: { color: "#7d8fa5" },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    legend: { data: ["组合总权益", "沪深300(归一)", "回撤"], textStyle: { color: "#7d8fa5" }, top: 0 },
    grid: [
      { left: 74, right: 26, top: 34, height: "56%" },
      { left: 74, right: 26, top: "74%", height: "18%" }
    ],
    xAxis: [
      { type: "category", data: d.dates, gridIndex: 0, axisLine: { lineStyle: { color: "#223047" } } },
      { type: "category", data: d.dates, gridIndex: 1, axisLine: { lineStyle: { color: "#223047" } } }
    ],
    yAxis: [
      { type: "value", scale: true, gridIndex: 0, splitLine: { lineStyle: { color: "rgba(34,48,71,.6)" } },
        axisLabel: { formatter: function (v) { return v >= 10000 ? (v / 10000).toFixed(1) + "万" : v; } } },
      { type: "value", gridIndex: 1, splitLine: { show: false },
        axisLabel: { formatter: "{value}%" }, max: 0 }
    ],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }],
    series: [
      { name: "组合总权益", type: "line", data: d.total, xAxisIndex: 0, yAxisIndex: 0,
        showSymbol: d.dates.length <= 2, symbolSize: 6, lineStyle: { width: 2, color: "#4da3ff" },
        itemStyle: { color: "#4da3ff" }, connectNulls: true },
      { name: "沪深300(归一)", type: "line", data: d.benchmark, xAxisIndex: 0, yAxisIndex: 0,
        showSymbol: false, lineStyle: { width: 1.5, type: "dashed", color: "#e6b800" },
        itemStyle: { color: "#e6b800" }, connectNulls: true },
      { name: "回撤", type: "line", data: ddNeg, xAxisIndex: 1, yAxisIndex: 1,
        showSymbol: d.dates.length <= 2, lineStyle: { width: 1, color: "#ef5350" },
        itemStyle: { color: "#ef5350" },
        areaStyle: { color: "rgba(239,83,80,.22)" }, connectNulls: true }
    ]
  }, true);
}

/* ───────────────────────── 标签页 2：信号看板 ───────────────────────── */
async function refreshSignals() {
  clearErr("signals-err");
  try {
    var d = await fetchJSON("/api/signals");
    S.signals = d || [];
    renderSignalCards();
    if (S.sigSelected) {
      var still = S.signals.some(function (s) { return s.code === S.sigSelected; });
      if (!still) { S.sigSelected = null; $("#candle-panel").classList.add("hidden"); }
    }
  } catch (e) {
    showErr("signals-err", "信号加载失败：" + e.message);
    $("#signal-cards").innerHTML = "";
    $("#signals-empty").classList.remove("hidden");
  }
}

function badgeTrend(t) {
  if (t === "up") return '<span class="badge b-trend-up">多头 ↑</span>';
  if (t === "down") return '<span class="badge b-trend-down">空头 ↓</span>';
  return '<span class="badge b-trend-flat">震荡 →</span>';
}

function renderSignalCards() {
  var wrap = $("#signal-cards");
  var list = S.signals;
  if (!list.length) {
    $("#signals-empty").classList.remove("hidden");
    wrap.innerHTML = "";
    return;
  }
  $("#signals-empty").classList.add("hidden");
  wrap.innerHTML = list.map(function (s) {
    var g = s.signals || {};
    var score = (s.score || 0) * 100;
    var rsi = g.rsi_14;
    var rsiCls = rsi == null ? "flat" : (rsi >= 70 ? "up" : (rsi <= 30 ? "down" : "flat"));
    var rsiTag = rsi == null ? "" : (rsi >= 70 ? " 超买" : (rsi <= 30 ? " 超卖" : " 中性"));
    var mom = g.mom_20d;
    return '<div class="sig-card' + (S.sigSelected === s.code ? " sel-on" : "") + '" data-code="' + esc(s.code) + '">' +
      '<div class="hd"><span class="nm">' + esc(s.name) + '</span><span class="cd">' + esc(s.code) + ' · ' + esc(s.as_of || "") + "</span></div>" +
      '<div class="score-bar"><i style="width:' + Math.max(0, Math.min(100, score)).toFixed(1) + '%"></i></div>' +
      '<div class="sig-metrics">' +
        badgeTrend(g.ma_trend) +
        '<span class="badge b-trend-flat">score ' + fmtNum(s.score, 3) + "</span>" +
        '<span class="badge ' + (rsiCls === "up" ? "b-trend-up" : rsiCls === "down" ? "b-trend-down" : "b-trend-flat") + '">RSI ' + (rsi == null ? "—" : rsi.toFixed(1)) + esc(rsiTag) + "</span>" +
        '<span class="badge ' + (mom == null ? "b-trend-flat" : mom >= 0 ? "b-trend-up" : "b-trend-down") + '">20日动量 ' + (mom == null ? "—" : fmtPct(mom * 100)) + "</span>" +
        '<span class="badge b-trend-flat">MA60' + (g.above_ma60 ? "上 ✓" : "下 ✗") + "</span>" +
        '<span class="badge b-trend-flat">换手 ' + (g.turnover_pct == null ? "—" : g.turnover_pct.toFixed(2) + "%") + "</span>" +
        '<span class="badge b-trend-flat">收 ' + fmtNum(g.close) + " (" + (g.pct_chg == null ? "—" : fmtPct(g.pct_chg)) + ")</span>" +
      "</div></div>";
  }).join("");
  $all("#signal-cards .sig-card").forEach(function (card) {
    card.addEventListener("click", function () { selectSignal(card.getAttribute("data-code")); });
  });
}

function selectSignal(code) {
  S.sigSelected = code;
  $all("#signal-cards .sig-card").forEach(function (c) {
    c.classList.toggle("sel-on", c.getAttribute("data-code") === code);
  });
  loadCandles(code);
}

async function loadCandles(code) {
  clearErr("candle-err");
  var s = S.signals.filter(function (x) { return x.code === code; })[0] || {};
  $("#candle-panel").classList.remove("hidden");
  $("#candle-title").textContent = "K线 · " + (s.name || code) + " (" + code + ")";
  try {
    var d = await fetchJSON("/api/candles?code=" + encodeURIComponent(code) + "&days=120");
    renderCandles(d);
  } catch (e) {
    showErr("candle-err", "K线加载失败：" + e.message);
    $("#candle-chart").classList.add("hidden");
    return;
  }
  $("#candle-chart").classList.remove("hidden");
}

function renderCandles(d) {
  if (!(d.dates || []).length) {
    $("#candle-chart").classList.add("hidden");
    $("#candle-panel").querySelector(".panel-head .hint").textContent = "暂无数据";
    return;
  }
  $("#candle-chart").classList.remove("hidden");
  var c = chart("candle-chart");
  if (!c) return;
  var k = d.kline;
  var kl = d.dates.map(function (_, i) {
    return [k.open[i], k.close[i], k.low[i], k.high[i]];
  });
  function maSeries(name, arr, color) {
    return { name: name, type: "line", data: arr, showSymbol: false, smooth: true,
             lineStyle: { width: 1.2, color: color }, itemStyle: { color: color }, connectNulls: true };
  }
  c.setOption({
    backgroundColor: "transparent",
    textStyle: { color: "#7d8fa5" },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    legend: { data: ["K线", "MA5", "MA20", "MA60"], textStyle: { color: "#7d8fa5" }, top: 0 },
    grid: { left: 64, right: 26, top: 32, height: "58%" },
    xAxis: { type: "category", data: d.dates, axisLine: { lineStyle: { color: "#223047" } } },
    yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "rgba(34,48,71,.6)" } } },
    dataZoom: [
      { type: "inside", start: 50, end: 100 },
      { type: "slider", bottom: 6, height: 22, borderColor: "#223047" }
    ],
    series: [
      { name: "K线", type: "candlestick", data: kl,
        itemStyle: { color: "#ef5350", color0: "#26a69a", borderColor: "#ef5350", borderColor0: "#26a69a" } },
      maSeries("MA5", d.ma5, "#e6b800"),
      maSeries("MA20", d.ma20, "#4da3ff"),
      maSeries("MA60", d.ma60, "#b06ce6")
    ]
  }, true);
}

/* ───────────────────────── 标签页 3：决策流水 ───────────────────────── */
async function refreshDecisions() {
  clearErr("decisions-err");
  try {
    var d = await fetchJSON("/api/decisions?limit=50");
    S.decisions = d || [];
    renderDecisions();
  } catch (e) {
    showErr("decisions-err", "决策加载失败：" + e.message);
    $("#decisions-wrap").innerHTML = "";
  }
}

function renderDecisions() {
  var wrap = $("#decisions-wrap");
  var list = S.decisions;
  if (!list.length) {
    wrap.innerHTML = emptyBox("暂无决策 —— decision 表为空（P5 决策阶段未开始）");
    return;
  }
  var rows = list.map(function (x, i) {
    var detail =
      (x.reasons && x.reasons.length ? '<div class="dt">决策理由 reasons</div><ul>' +
        x.reasons.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") + "</ul>" : '<div class="dt">决策理由：无</div>') +
      (x.risk_notes && x.risk_notes.length ? '<div class="dt">风险提示 risk_notes</div><ul>' +
        x.risk_notes.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") + "</ul>" : '<div class="dt">风险提示：无</div>');
    return '<tr class="clickable" data-i="' + i + '">' +
      '<td><span class="caret">▶</span>#' + esc(x.id) + "</td>" +
      '<td class="t">' + esc(x.run_date) + "</td>" +
      '<td class="t">' + esc(x.code) + "</td>" +
      '<td class="t">' + esc(x.name) + "</td>" +
      '<td>' + (x.action === "buy" ? badgeSide("buy") : x.action === "sell" ? badgeSide("sell") : '<span class="badge b-trend-flat">' + esc(x.action) + "</span>") + "</td>" +
      '<td class="num">' + (x.target_weight == null ? "—" : (x.target_weight * 100).toFixed(1) + "%") + "</td>" +
      '<td class="num">' + (x.confidence == null ? "—" : x.confidence.toFixed(2)) + "</td>" +
      "<td>" + badgeStatus(x.status) + "</td>" +
      '<td class="t">' + esc(fmtTs(x.created_at)) + "</td></tr>" +
      '<tr class="detail-row hidden" data-d="' + i + '"><td colspan="9">' + detail + "</td></tr>";
  }).join("");
  wrap.innerHTML =
    '<div class="tbl-wrap"><table class="tbl"><thead><tr>' +
    "<th>ID</th><th>决策日</th><th>代码</th><th>名称</th><th>动作</th><th class=\"num\">目标权重</th>" +
    '<th class="num">置信度</th><th>状态</th><th>创建时间</th>' +
    "</tr></thead><tbody>" + rows + "</tbody></table></div>";
  $all("#decisions-wrap tr.clickable").forEach(function (tr) {
    tr.addEventListener("click", function () {
      var i = tr.getAttribute("data-i");
      var dr = document.querySelector('.detail-row[data-d="' + i + '"]');
      if (dr) dr.classList.toggle("hidden");
      var caret = tr.querySelector(".caret");
      if (caret) caret.textContent = dr && dr.classList.contains("hidden") ? "▶" : "▼";
    });
  });
}

async function refreshSessions() {
  clearErr("session-err");
  try {
    var d = await fetchJSON("/api/sessions");
    S.sessions = d || [];
    var sel = $("#session-select");
    if (!S.sessions.length) {
      sel.innerHTML = '<option value="">暂无输入包</option>';
      $("#btn-session").disabled = true;
      $("#session-view").classList.add("empty-view");
      $("#session-view").innerHTML = "暂无数据 —— logs/session 为空（P5 决策阶段未开始）";
      return;
    }
    sel.innerHTML = S.sessions.map(function (s) {
      return '<option value="' + esc(s.date) + '">' + esc(s.date) +
        (s.has_bundle ? " · bundle" : "") + (s.has_decision ? " · decision" : "") + "</option>";
    }).join("");
    $("#btn-session").disabled = false;
    loadSession();
  } catch (e) {
    showErr("session-err", "输入包列表加载失败：" + e.message);
  }
}

async function loadSession() {
  var date = $("#session-select").value;
  if (!date) return;
  clearErr("session-err");
  try {
    var d = await fetchJSON("/api/session?date=" + encodeURIComponent(date) + "&kind=bundle_md");
    var view = $("#session-view");
    view.classList.remove("empty-view");
    view.innerHTML = window.marked
      ? marked.parse(d.content || "")
      : "<pre>" + esc(d.content || "") + "</pre>";
  } catch (e) {
    showErr("session-err", "输入包加载失败：" + e.message);
  }
}

/* ───────────────────────── 标签页 4：成交与风控 ───────────────────────── */
async function refreshPending() {
  clearErr("pending-err");
  try {
    var d = await fetchJSON("/api/pending");
    S.pending = d || [];
    renderPending();
  } catch (e) {
    showErr("pending-err", "待确认单加载失败：" + e.message);
    $("#pending-wrap").innerHTML = "";
  }
}

function renderPending() {
  var wrap = $("#pending-wrap");
  var list = S.pending;
  if (!list.length) {
    wrap.innerHTML = emptyBox("暂无待确认单 —— 人工闸门空闲（无 pending 文件）");
    return;
  }
  wrap.innerHTML = list.map(function (p, i) {
    var dec = p.decision || {};
    var v = p.verdict || {};
    var adj = v.adjusted_order;
    var violations = v.violations || [];
    var warnings = v.warnings || [];
    return '<div class="gate-card" data-did="' + esc(p.decision_id) + '">' +
      '<div class="hd"><span class="tt">决策 #' + esc(p.decision_id) + " · " +
        (dec.action === "buy" ? "买入" : dec.action === "sell" ? "卖出" : esc(dec.action)) + " " +
        esc(dec.name || dec.code || "") + " <span class='hint'>(" + esc(dec.code || "") + ")</span></span>" +
        '<span><span class="badge ' + (v.approved ? "b-approved" : "b-rejected") + '">' +
        (v.approved ? "风控通过" : "风控未过") + "</span> " +
        '<span class="hint">' + esc(fmtTs(p.created_at)) + "</span></span></div>" +
      '<div class="kv-line"><span class="k2">目标权重</span>' +
        (dec.target_weight == null ? "—" : (dec.target_weight * 100).toFixed(1) + "%") +
        '　<span class="k2">置信度</span>' + (dec.confidence == null ? "—" : dec.confidence.toFixed(2)) +
        '　<span class="k2">建议数量</span>' + esc(p.decision && p.decision.order ? p.decision.order.shares : "—") +
        '　<span class="k2">委托价</span>' + esc(p.decision && p.decision.order ? p.decision.order.price : "—") +
        (adj ? '　<span class="badge b-proposed">风控调整 → ' + esc(adj.shares) + " 股</span>" : "") +
      "</div>" +
      (dec.reasons && dec.reasons.length ? '<div class="kv-line"><span class="k2">理由</span></div><ul class="gate-list">' +
        dec.reasons.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") + "</ul>" : "") +
      (dec.risk_notes && dec.risk_notes.length ? '<div class="kv-line"><span class="k2">风险</span></div><ul class="gate-list warning">' +
        dec.risk_notes.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") + "</ul>" : "") +
      (violations.length ? '<div class="kv-line"><span class="k2">违规项</span></div><ul class="gate-list violation">' +
        violations.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") + "</ul>" : "") +
      (warnings.length ? '<div class="kv-line"><span class="k2">警告项</span></div><ul class="gate-list warning">' +
        warnings.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") + "</ul>" : "") +
      '<div class="kv-line"><span class="k2">确认命令</span><code>' + esc(p.confirm_hint) + "</code></div>" +
      '<div class="gate-actions">' +
        '<span>操作人 <input type="text" class="gate-by" style="width:110px" value="人工"></span>' +
        '<button class="btn btn-primary gate-confirm" data-did="' + esc(p.decision_id) + '">✓ 确认执行</button>' +
        '<span class="rej"><input type="text" class="gate-reason" placeholder="否决理由（必填）">' +
        '<button class="btn btn-danger gate-reject" data-did="' + esc(p.decision_id) + '">✗ 否决</button></span>' +
      "</div>" +
      '<div class="cli-out hidden"><div class="cap">CLI 输出（execution/runner.py）</div><pre class="log-view"></pre></div>' +
      '<div class="hint" data-idx="' + i + '" style="display:none"></div>' +
    "</div>";
  }).join("");

  $all(".gate-confirm").forEach(function (btn) {
    btn.addEventListener("click", function () { gateAct(btn, "confirm"); });
  });
  $all(".gate-reject").forEach(function (btn) {
    btn.addEventListener("click", function () { gateAct(btn, "reject"); });
  });
}

async function gateAct(btn, kind) {
  var did = parseInt(btn.getAttribute("data-did"), 10);
  var card = btn.closest(".gate-card");
  var by = (card.querySelector(".gate-by") || {}).value || "人工";
  var reason = kind === "reject" ? (card.querySelector(".gate-reason") || {}).value || "" : "";
  if (kind === "reject" && !reason.trim()) {
    alert("否决必须填写理由");
    return;
  }
  btn.disabled = true;
  btn.textContent = kind === "confirm" ? "执行中…" : "否决中…";
  var outBox = $("#gate-out");
  var outPre = $("#gate-out-pre");
  outBox.classList.remove("hidden");
  outPre.textContent = "执行中…（调用 execution/runner.py " + kind + "）";
  try {
    var body = kind === "confirm"
      ? { decision_id: did, by: by }
      : { decision_id: did, reason: reason, by: by };
    var d = await fetchJSON("/api/" + kind, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    outPre.textContent = "[decision #" + did + " · " + kind + " · " +
      (d.ok ? "CLI 正常返回" : "CLI 返回非零") + "]\n" + (d.output || "(空)");
  } catch (e) {
    outPre.textContent = "[请求失败] " + e.message;
  }
  btn.disabled = false;
  btn.textContent = kind === "confirm" ? "✓ 确认执行" : "✗ 否决";
  /* 操作后刷新相关数据 */
  refreshPending(); refreshOverview();
  if (S.loaded.decisions) refreshDecisions();
  if (S.loaded.trades) refreshTrades();
}

async function refreshTrades() {
  clearErr("trades-err");
  try {
    var d = await fetchJSON("/api/trades?limit=50");
    var list = d || [];
    var wrap = $("#trades-wrap");
    if (!list.length) {
      wrap.innerHTML = emptyBox("暂无成交 —— trade 表为空（P6 执行阶段未开始）");
      return;
    }
    var rows = list.map(function (t) {
      return "<tr>" +
        '<td>#' + esc(t.id) + "</td>" +
        '<td class="t">' + esc(t.trade_date) + "</td>" +
        '<td class="t">' + esc(t.code) + "</td>" +
        '<td class="t">' + esc(t.name || "") + "</td>" +
        "<td>" + badgeSide(t.side) + "</td>" +
        '<td class="num">' + fmtNum(t.price) + "</td>" +
        '<td class="num">' + fmtNum(t.shares, 0) + "</td>" +
        '<td class="num">' + fmtMoney(t.amount, true) + "</td>" +
        '<td><span class="badge ' + (t.status === "filled" ? "b-filled" : "b-trend-flat") + '">' + esc(t.status) + "</span></td>" +
        '<td>#' + esc(t.decision_id == null ? "—" : t.decision_id) + "</td>" +
        '<td class="t">' + esc(t.confirmed_by || "—") + "</td>" +
        '<td class="t">' + esc(fmtTs(t.created_at)) + "</td></tr>";
    }).join("");
    wrap.innerHTML = '<div class="tbl-wrap"><table class="tbl"><thead><tr>' +
      "<th>ID</th><th>日期</th><th>代码</th><th>名称</th><th>方向</th><th class=\"num\">价格</th>" +
      '<th class="num">数量</th><th class="num">金额(净流)</th><th>状态</th><th>决策</th><th>确认人</th><th>时间</th>' +
      "</tr></thead><tbody>" + rows + "</tbody></table></div>";
  } catch (e) {
    showErr("trades-err", "成交加载失败：" + e.message);
  }
}

async function refreshRiskEvents() {
  clearErr("risk-err");
  try {
    var d = await fetchJSON("/api/risk_events?limit=50");
    var list = d || [];
    var wrap = $("#risk-wrap");
    if (!list.length) {
      wrap.innerHTML = emptyBox("暂无风控事件 —— risk_event 表为空");
      return;
    }
    var rows = list.map(function (r) {
      return "<tr>" +
        '<td>#' + esc(r.id) + "</td>" +
        '<td class="t">' + esc(fmtTs(r.ts)) + "</td>" +
        '<td class="t">' + esc(r.rule) + "</td>" +
        '<td class="t">' + esc(r.detail) + "</td>" +
        '<td>#' + esc(r.decision_id == null ? "—" : r.decision_id) + "</td></tr>";
    }).join("");
    wrap.innerHTML = '<div class="tbl-wrap"><table class="tbl"><thead><tr>' +
      "<th>ID</th><th>时间</th><th>规则</th><th>详情</th><th>决策</th>" +
      "</tr></thead><tbody>" + rows + "</tbody></table></div>";
  } catch (e) {
    showErr("risk-err", "风控事件加载失败：" + e.message);
  }
}

/* ───────────────────────── 标签页 5：新闻与宏观 ───────────────────────── */
async function refreshMacro() {
  clearErr("macro-err");
  try {
    var d = await fetchJSON("/api/macro");
    renderMacroCards((d || {}).indices || []);
  } catch (e) {
    showErr("macro-err", "估值加载失败：" + e.message);
    $("#macro-cards").innerHTML = "";
  }
}

function pctBar(label, pct, v) {
  var p = (pct == null ? 0 : pct * 100);
  var color = p >= 70 ? "#ef5350" : (p <= 30 ? "#26a69a" : "#e6b800");
  return '<div class="pct-row"><span class="lab">' + esc(label) + '</span>' +
    '<span class="pct-bar"><i style="width:' + Math.max(0, Math.min(100, p)).toFixed(1) + "%;background:" + color + '"></i></span>' +
    '<span class="pct-val">' + (pct == null ? "—" : p.toFixed(0) + "%") + "</span>" +
    '<span>' + (v == null ? "—" : fmtNum(v, 2)) + "</span></div>";
}

function renderMacroCards(list) {
  var wrap = $("#macro-cards");
  $("#macro-empty").classList.toggle("hidden", list.length > 0);
  if (!list.length) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = list.map(function (m) {
    return '<div class="val-card">' +
      '<div class="nm">' + esc(m.name) + "<span>" + esc(m.trade_date) + " · 收 " + fmtNum(m.close) + "</span></div>" +
      '<div class="pe">' + fmtNum(m.pe, 2) + "<small>PE(TTM)</small></div>" +
      pctBar("PE 分位", m.pe_pct, m.pe) +
      pctBar("PB 分位", m.pb_pct, m.pb) +
      "</div>";
  }).join("");
}

async function refreshMacroHistory() {
  clearErr("macro-history-err");
  var idx = $("#macro-index-sel").value;
  var years = $("#macro-years-sel").value;
  try {
    var d = await fetchJSON("/api/macro_history?index=" + encodeURIComponent(idx) + "&years=" + encodeURIComponent(years));
    renderMacroHistory(d);
  } catch (e) {
    showErr("macro-history-err", "PE 历史加载失败：" + e.message);
    $("#macro-chart").classList.add("hidden");
  }
}

function renderMacroHistory(d) {
  var has = (d.dates || []).length > 0;
  $("#macro-chart").classList.toggle("hidden", !has);
  if (!has) return;
  var c = chart("macro-chart");
  if (!c) return;
  var pct = (d.pe_pct || []).map(function (v) { return v == null ? null : v * 100; });
  c.setOption({
    backgroundColor: "transparent",
    textStyle: { color: "#7d8fa5" },
    tooltip: { trigger: "axis",
      formatter: function (params) {
        var i = params[0].dataIndex;
        var html = esc(d.dates[i]);
        params.forEach(function (p) {
          html += "<br>" + p.marker + " " + esc(p.seriesName) + ": " +
            (p.seriesName === "PE 分位" ? (p.value == null ? "—" : Number(p.value).toFixed(1) + "%") : fmtNum(p.value, 2));
        });
        return html;
      } },
    legend: { data: ["PE(TTM)", "PE 分位"], textStyle: { color: "#7d8fa5" }, top: 0 },
    grid: { left: 58, right: 54, top: 32, bottom: 46 },
    xAxis: { type: "category", data: d.dates, axisLine: { lineStyle: { color: "#223047" } } },
    yAxis: [
      { type: "value", scale: true, name: "PE", splitLine: { lineStyle: { color: "rgba(34,48,71,.6)" } },
        nameTextStyle: { color: "#7d8fa5" } },
      { type: "value", min: 0, max: 100, name: "分位%", splitLine: { show: false },
        axisLabel: { formatter: "{value}%" }, nameTextStyle: { color: "#7d8fa5" } }
    ],
    dataZoom: [{ type: "inside" }, { type: "slider", bottom: 4, height: 20, borderColor: "#223047" }],
    series: [
      { name: "PE 分位", type: "line", data: pct, xAxisIndex: 0, yAxisIndex: 1,
        showSymbol: false, lineStyle: { width: 0 }, connectNulls: true,
        areaStyle: { color: "rgba(77,163,255,.13)" },
        markLine: { silent: true, symbol: "none", lineStyle: { type: "dashed", color: "rgba(230,184,0,.55)" },
          label: { color: "#e6b800", formatter: "{b}" },
          data: [{ name: "70%", yAxis: 70 }, { name: "30%", yAxis: 30 }] } },
      { name: "PE(TTM)", type: "line", data: d.pe, showSymbol: false,
        lineStyle: { width: 2, color: "#4da3ff" }, itemStyle: { color: "#4da3ff" }, connectNulls: true }
    ]
  }, true);
}

async function refreshNews() {
  clearErr("news-err");
  try {
    var d = await fetchJSON("/api/news?limit=30");
    S.news = d || { market: [], by_code: {} };
    renderNews(S.news);
  } catch (e) {
    showErr("news-err", "新闻加载失败：" + e.message);
    $("#news-wrap").innerHTML = "";
  }
}

function newsItemHtml(n) {
  return '<div class="news-item">' +
    '<div class="t">' + (n.url ? '<a href="' + esc(n.url) + '" target="_blank" rel="noopener">' + esc(n.title) + "</a>" : esc(n.title)) + "</div>" +
    '<div class="m">' + esc(n.source || "") + " · " + esc(fmtTs(n.published_at)) + "</div>" +
    (n.content ? '<div class="x">' + esc(n.content) + "</div>" : "") +
    "</div>";
}

function renderNews(d) {
  var wrap = $("#news-wrap");
  var names = {};
  (S.overview && S.overview.blacklist || []).forEach(function (b) { names[b.code] = b.name; });
  (S.signals || []).forEach(function (s) { names[s.code] = s.name; });
  if (S.newsMode === "market") {
    if (!(d.market || []).length) {
      wrap.innerHTML = emptyBox("暂无市场级新闻（news.code=''）");
      return;
    }
    wrap.innerHTML = d.market.map(newsItemHtml).join("");
  } else {
    var codes = Object.keys(d.by_code || {}).sort();
    if (!codes.length) {
      wrap.innerHTML = emptyBox("暂无个股新闻");
      return;
    }
    wrap.innerHTML = codes.map(function (code) {
      return '<div class="news-group-h">' + esc(names[code] || "") + " (" + esc(code) + ")</div>" +
        d.by_code[code].map(newsItemHtml).join("");
    }).join("");
  }
}

/* ───────────────────────── 标签页 6：报告与日志 ───────────────────────── */
async function refreshReports() {
  clearErr("reports-err");
  try {
    var d = await fetchJSON("/api/reports");
    var list = d || [];
    var ul = $("#report-list");
    $("#reports-empty").classList.toggle("hidden", list.length > 0);
    $("#report-view").classList.toggle("hidden", list.length === 0);
    if (!list.length) { ul.innerHTML = ""; return; }
    ul.innerHTML = list.map(function (r) {
      return '<li data-f="' + esc(r.file) + '">' + esc(r.file) +
        '<div class="meta">' + fmtNum(r.size / 1024, 1) + " KB · " + esc(new Date(r.mtime * 1000).toLocaleString("zh-CN")) + "</div></li>";
    }).join("");
    $all("#report-list li").forEach(function (li) {
      li.addEventListener("click", function () { loadReport(li.getAttribute("data-f")); });
    });
  } catch (e) {
    showErr("reports-err", "报告列表加载失败：" + e.message);
  }
}

async function loadReport(file) {
  clearErr("reports-err");
  $all("#report-list li").forEach(function (li) {
    li.classList.toggle("on", li.getAttribute("data-f") === file);
  });
  try {
    var d = await fetchJSON("/api/report?file=" + encodeURIComponent(file));
    var view = $("#report-view");
    view.classList.remove("hidden", "empty-view");
    view.innerHTML = window.marked
      ? marked.parse(d.markdown || "")
      : "<pre>" + esc(d.markdown || "") + "</pre>";
  } catch (e) {
    showErr("reports-err", "报告加载失败：" + e.message);
  }
}

async function refreshLogs() {
  clearErr("log-err");
  var name = $("#log-name-sel").value;
  var lines = $("#log-lines-sel").value;
  try {
    var d = await fetchJSON("/api/logs?name=" + encodeURIComponent(name) + "&lines=" + encodeURIComponent(lines));
    var pre = $("#log-view");
    var arr = d.lines || [];
    pre.textContent = arr.length ? arr.join("\n") : "（" + name + ".log 为空或不存在）";
    if ($("#log-autoscroll").checked) pre.scrollTop = pre.scrollHeight;
  } catch (e) {
    showErr("log-err", "日志加载失败：" + e.message);
  }
}

async function refreshBacktest() {
  clearErr("backtest-err");
  try {
    var d = await fetchJSON("/api/backtest");
    var wrap = $("#backtest-wrap");
    if (d && d.missing) {
      wrap.innerHTML = emptyBox("暂无回测结果 —— logs/backtest_result.json 不存在（P7 回测阶段未开始）");
      return;
    }
    wrap.innerHTML = '<pre class="log-view">' + esc(JSON.stringify(d, null, 2)) + "</pre>";
  } catch (e) {
    showErr("backtest-err", "回测结果加载失败：" + e.message);
  }
}

/* ───────────────────────── 标签页调度 ───────────────────────── */
var TAB_LOADER = {
  equity: function () { refreshOverview(); refreshEquity(); },
  signals: function () { refreshSignals(); },
  decisions: function () { refreshDecisions(); refreshSessions(); },
  risk: function () { refreshPending(); refreshTrades(); refreshRiskEvents(); },
  macro: function () { refreshMacro(); refreshMacroHistory(); refreshNews(); },
  reports: function () { refreshReports(); refreshBacktest(); refreshLogs(); }
};

function switchTab(name) {
  S.activeTab = name;
  $all("#tabs .tab").forEach(function (t) {
    t.classList.toggle("active", t.getAttribute("data-tab") === name);
  });
  $all(".tab-page").forEach(function (p) {
    p.classList.toggle("active", p.id === "tab-" + name);
  });
  Object.keys(S.charts).forEach(function (k) { S.charts[k].resize(); });
  if (!S.loaded[name]) {
    S.loaded[name] = true;
    (TAB_LOADER[name] || function () {})();
  }
}

function refreshActiveTab() {
  (TAB_LOADER[S.activeTab] || function () {})();
}

/* ───────────────────────── 自动刷新 ───────────────────────── */
function tick() {
  S.countdown -= 1;
  if (S.countdown <= 0) {
    S.countdown = REFRESH_SEC;
    /* 轻量接口：overview / signals / pending + 当前页 */
    refreshOverview();
    if (S.loaded.signals) refreshSignals();
    if (S.loaded.risk) refreshPending();
    if (S.activeTab === "equity") refreshEquity();
  }
  $("#countdown").textContent = String(S.countdown);
}

/* ───────────────────────── 启动 ───────────────────────── */
function init() {
  $all("#tabs .tab").forEach(function (t) {
    t.addEventListener("click", function () { switchTab(t.getAttribute("data-tab")); });
  });
  $all(".refresh-btn").forEach(function (btn) {
    btn.addEventListener("click", refreshActiveTab);
  });
  $("#btn-refresh-all").addEventListener("click", function () {
    refreshOverview(); refreshActiveTab();
  });
  $("#btn-session").addEventListener("click", loadSession);
  $("#session-select").addEventListener("change", loadSession);
  $("#macro-index-sel").addEventListener("change", refreshMacroHistory);
  $("#macro-years-sel").addEventListener("change", refreshMacroHistory);
  $("#news-mkt-btn").addEventListener("click", function () {
    S.newsMode = "market";
    $("#news-mkt-btn").classList.add("seg-on"); $("#news-code-btn").classList.remove("seg-on");
    if (S.news) renderNews(S.news);
  });
  $("#news-code-btn").addEventListener("click", function () {
    S.newsMode = "code";
    $("#news-code-btn").classList.add("seg-on"); $("#news-mkt-btn").classList.remove("seg-on");
    if (S.news) renderNews(S.news);
  });
  $("#btn-log").addEventListener("click", refreshLogs);
  $("#log-name-sel").addEventListener("change", refreshLogs);
  $("#log-lines-sel").addEventListener("change", refreshLogs);
  window.addEventListener("resize", function () {
    Object.keys(S.charts).forEach(function (k) { S.charts[k].resize(); });
  });

  /* 首屏 */
  S.loaded.equity = true;
  refreshOverview().then(function (d) {
    if (d && S.activeTab === "equity") refreshEquity();
  });
  refreshEquity();
  setInterval(tick, 1000);
}

document.addEventListener("DOMContentLoaded", init);
