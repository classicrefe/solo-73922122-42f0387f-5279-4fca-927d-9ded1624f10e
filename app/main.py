"""FastAPI 入口:操作页(来源核对 / 运单回补)+ 接口 + 样例下载。"""
from __future__ import annotations

import json

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .core import (
    EvidenceError,
    InputError,
    load_receipt_csv,
    load_receipt_csv_allow_empty,
    load_waybill_csv,
    load_waybill_csv_allow_empty,
    parse_evidence,
    verify,
)
from .backfill import (
    CannotProduce,
    ScopeInsufficient,
    csv_style,
    parse_approved,
    parse_target_commit,
    run_backfill,
)
from .backfill_sample import build_backfill_sample
from .sample import build_sample

app = FastAPI(title="货运数仓来源核对与运单回补")

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>货运数仓来源核对</title>
<style>
  :root { color-scheme: light; }
  body { font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
         margin: 0; background: #f4f6f8; color: #1f2933; }
  .wrap { max-width: 920px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 22px; margin: 8px 0 4px; }
  .sub { color: #6b7280; font-size: 13px; margin-bottom: 20px; }
  .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
          padding: 18px 20px; margin-bottom: 16px; }
  .card h2 { font-size: 15px; margin: 0 0 12px; }
  label.f { display: block; margin: 10px 0 4px; font-size: 13px; font-weight: 600; }
  input[type=file] { font-size: 13px; }
  button { background: #2563eb; color: #fff; border: 0; border-radius: 8px;
           padding: 10px 26px; font-size: 14px; cursor: pointer; margin-top: 14px; }
  button:disabled { background: #93c5fd; cursor: wait; }
  .banner { border-radius: 8px; padding: 12px 16px; font-size: 14px; font-weight: 600; }
  .ok    { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
  .warn  { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
  .err   { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }
  th, td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }
  th { background: #f9fafb; }
  code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  pre { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px;
        padding: 12px; font-size: 12px; overflow: auto; }
  details { font-size: 13px; }
  details summary { cursor: pointer; font-weight: 600; margin-bottom: 6px; }
  .muted { color: #6b7280; font-size: 12px; }
  .iv { display: inline-block; background: #eff6ff; color: #1d4ed8; border-radius: 6px;
        padding: 2px 8px; margin: 2px 4px 2px 0; font-family: ui-monospace, monospace; }
  #result { display: none; }
  #bf-result { display: none; }
  a { color: #2563eb; }
  .tabs { display: flex; gap: 8px; margin: 0 0 16px; }
  .tabs button { margin: 0; background: #e5e7eb; color: #374151; padding: 8px 18px; }
  .tabs button.on { background: #2563eb; color: #fff; }
  .hidden { display: none !important; }
  input[type=text], input[type=number] { font-size: 13px; padding: 8px 10px; width: 100%;
        box-sizing: border-box; border: 1px solid #d1d5db; border-radius: 8px; }
  .row2 { display: flex; gap: 14px; flex-wrap: wrap; }
  .row2 > div { flex: 1 1 260px; }
  .chip { display: inline-block; background: #f3f4f6; border: 1px solid #e5e7eb;
          border-radius: 6px; padding: 1px 7px; margin: 2px 4px 2px 0;
          font-family: ui-monospace, monospace; font-size: 12px; }
  .chip.bad { background: #fef2f2; border-color: #fecaca; color: #b91c1c; }
  .stat { display: flex; gap: 18px; flex-wrap: wrap; margin: 10px 0; font-size: 13px; }
  .stat b { font-size: 18px; display: block; }
  a.dl { display: inline-block; background: #059669; color: #fff; text-decoration: none;
         border-radius: 8px; padding: 10px 24px; font-size: 14px; margin-top: 10px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>货运数仓来源核对与运单回补</h1>
  <div class="sub">核对两份未标版本的导出 CSV 是否来自同一次提交;或按获批单号,把下游导出中被覆盖的运单及其签收明细整体回补到目标提交的版本。</div>

  <div class="tabs">
    <button type="button" id="tab-verify" class="on" onclick="switchTab('verify')">来源核对</button>
    <button type="button" id="tab-backfill" onclick="switchTab('backfill')">运单回补</button>
  </div>

  <div id="pane-verify">
  <div class="card">
    <h2>1. 上传文件</h2>
    <form id="form">
      <label class="f">运单导出 CSV(列:单号,状态,件数;仅已签收运单)</label>
      <input type="file" name="waybill_csv" accept=".csv" required>
      <label class="f">签收导出 CSV(列:签收号,单号,实收件数;仅已签收运单的签收明细)</label>
      <input type="file" name="receipt_csv" accept=".csv" required>
      <label class="f">证据包 JSON(起点快照 + 起止提交号 + 逐事务行变更前后值)</label>
      <input type="file" name="evidence" accept=".json,application/json" required>
      <br>
      <button id="go" type="submit">开始核查</button>
      <span class="muted" id="hint"></span>
    </form>
  </div>

  <div id="result">
    <div class="card">
      <h2>2. 核查结论</h2>
      <div class="banner" id="banner"></div>
      <div id="detail"></div>
    </div>
  </div>

  <div class="card">
    <h2>格式说明与样例</h2>
    <details>
      <summary>证据包 JSON 结构(点击展开)</summary>
      <pre>{
  "start_commit": 100,            // 起始提交号(快照所处提交)
  "end_commit": 110,              // 终止提交号
  "snapshots": {                  // 起点快照:两张表的全量行
    "waybills": [{"单号": "WB0001", "状态": "已签收", "件数": 3, ...}],
    "receipts": [{"签收号": "RC0001", "单号": "WB0001", "实收件数": 3, ...}]
  },
  "transactions": [               // 逐事务记录,提交号必须连续无缺号
    {"commit": 101, "changes": [
      {"table": "waybills", "op": "update", "key": "WB0001",
       "before": {...}, "after": {...}},     // insert: before=null; delete: after=null
      {"table": "receipts", "op": "insert", "key": "RC0009",
       "before": null, "after": {...}}
    ]}
  ]
}</pre>
      <div class="muted">
        表名支持 waybills/运单表、receipts/签收表。比对只在起点和每笔事务整体应用之后进行;
        日志缺号或变更前值与重放状态对不上时,判定证据不足并停止。变更条数上限 50000。
      </div>
    </details>
    <p class="muted" style="margin-top:10px">
      样例文件:
      <a href="/api/sample/waybills.csv" download>运单 CSV</a> ·
      <a href="/api/sample/receipts.csv" download>签收 CSV</a> ·
      <a href="/api/sample/evidence.json" download>证据包 JSON</a>
      (三者可直接上传试跑,预期交集:100、102、106、108、110)
    </p>
  </div>
  </div><!-- /pane-verify -->

  <div id="pane-backfill" class="hidden">
    <div class="card">
      <h2>回补输入</h2>
      <form id="bf-form">
        <label class="f">下游运单导出 CSV(列:单号,状态,件数)</label>
        <input type="file" name="waybill_csv" accept=".csv" required>
        <label class="f">下游签收导出 CSV(列:签收号,单号,实收件数)</label>
        <input type="file" name="receipt_csv" accept=".csv" required>
        <label class="f">JSON 证据包(完整校验:起点快照、连续提交日志、逐事务前后值)</label>
        <input type="file" name="evidence" accept=".json,application/json" required>
        <div class="row2">
          <div>
            <label class="f">目标提交号(取该提交完成后的导出数据;可填起点快照提交号)</label>
            <input type="number" name="target_commit" id="bf-target" required
                   placeholder="例如 505">
          </div>
          <div>
            <label class="f">获准回补的单号(逗号/空格分隔)</label>
            <input type="text" name="approved" id="bf-approved" required
                   placeholder="例如 WB100, WB101">
          </div>
        </div>
        <br>
        <button id="bf-go" type="submit">校验并生成回补包</button>
        <span class="muted" id="bf-hint"></span>
      </form>
    </div>

    <div id="bf-result">
      <div class="card">
        <h2>回补结论</h2>
        <div class="banner" id="bf-banner"></div>
        <div id="bf-detail"></div>
      </div>
    </div>

    <div class="card">
      <h2>回补样例</h2>
      <p class="muted" style="margin-top:0">
        样例:下游导出停在提交 500;目标提交 505 中 WB101 及其签收 RC1001 已删除,
        RC1002 由 WB101 改挂到 WB100,WB102 已退出导出。只选 WB100 会提示还需补选
        WB101(涉及签收号 RC1002);选 WB100、WB101 即可出包(范围外 WB102、WB106 保留原值)。
      </p>
      <p class="muted">
        下载后直接上传,或
        <button type="button" onclick="fillSample()"
                style="padding:5px 14px;margin:0 6px 0 0;font-size:12px;vertical-align:middle">
          填入样例参数</button>
        <a href="/api/backfill/sample/waybills.csv" download>下游运单 CSV</a> ·
        <a href="/api/backfill/sample/receipts.csv" download>下游签收 CSV</a> ·
        <a href="/api/backfill/sample/evidence.json" download>证据包 JSON</a>
        (目标提交 505,获准单号 WB100, WB101)
      </p>
    </div>
  </div>
</div>

<script>
const fmtIv = ([a, b]) => a === b ? `${a}` : `${a}–${b}`;
const ivsHtml = ivs => ivs.length
  ? ivs.map(iv => `<span class="iv">${fmtIv(iv)}</span>`).join("")
  : '<span class="muted">(无)</span>';
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => (
  {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const chips = (xs, cls = "") => (xs && xs.length)
  ? xs.map(x => `<span class="chip ${cls}">${esc(x)}</span>`).join("")
  : '<span class="muted">(无)</span>';

function switchTab(name) {
  const verify = name === "verify";
  document.getElementById("pane-verify").classList.toggle("hidden", !verify);
  document.getElementById("pane-backfill").classList.toggle("hidden", verify);
  document.getElementById("tab-verify").classList.toggle("on", verify);
  document.getElementById("tab-backfill").classList.toggle("on", !verify);
}

function fillSample() {
  document.getElementById("bf-target").value = "505";
  document.getElementById("bf-approved").value = "WB100, WB101";
}

document.getElementById("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("go");
  const hint = document.getElementById("hint");
  btn.disabled = true; hint.textContent = "核查中…";
  try {
    const fd = new FormData(e.target);
    const resp = await fetch("/api/verify", { method: "POST", body: fd });
    const data = await resp.json();
    render(data, resp.status);
  } catch (err) {
    render({ status: "invalid_input", message: "请求失败: " + err }, 0);
  } finally {
    btn.disabled = false; hint.textContent = "";
  }
});

function render(data, httpStatus) {
  const banner = document.getElementById("banner");
  const detail = document.getElementById("detail");
  document.getElementById("result").style.display = "block";
  let html = "";
  if (data.status === "ok") {
    banner.className = "banner ok";
    banner.textContent = "两份文件在所查范围内存在共同来源提交区间。";
  } else if (data.status === "no_common_source") {
    banner.className = "banner warn";
    banner.textContent = "所查范围内没有共同来源:两份文件的吻合提交区间交集为空。";
  } else if (data.status === "insufficient_evidence") {
    banner.className = "banner err";
    banner.textContent = "证据不足,已停止核查:" + data.message;
  } else {
    banner.className = "banner err";
    banner.textContent = "输入有误:" + (data.message || ("HTTP " + httpStatus));
  }
  const r = data.result;
  if (r) {
    html += `<table>
      <tr><th>文件</th><th>完全吻合的提交区间(按编号和值)</th><th>吻合提交数</th></tr>
      <tr><td>运单 CSV</td><td>${ivsHtml(r.waybill_csv.intervals)}</td>
          <td>${r.waybill_csv.match_commits}</td></tr>
      <tr><td>签收 CSV</td><td>${ivsHtml(r.receipt_csv.intervals)}</td>
          <td>${r.receipt_csv.match_commits}</td></tr>
      <tr><td><b>交集</b></td><td><b>${ivsHtml(r.intersection)}</b></td><td>—</td></tr>
    </table>
    <p class="muted">比对范围:提交 ${r.start_commit}–${r.end_commit}(共 ${r.compare_points} 个比对点);
    已应用变更 ${r.changes_applied} 条;起点快照:运单 ${r.snapshot_rows.waybills} 行、
    签收 ${r.snapshot_rows.receipts} 行。</p>`;
  }
  detail.innerHTML = html;
  document.getElementById("result").scrollIntoView({ behavior: "smooth" });
}

// ---- 回补 ----

document.getElementById("bf-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("bf-go");
  const hint = document.getElementById("bf-hint");
  btn.disabled = true; hint.textContent = "校验中…";
  try {
    const fd = new FormData(e.target);
    const resp = await fetch("/api/backfill", { method: "POST", body: fd });
    const data = await resp.json();
    renderBackfill(data, resp.status);
  } catch (err) {
    renderBackfill({ status: "invalid_input", message: "请求失败: " + err }, 0);
  } finally {
    btn.disabled = false; hint.textContent = "";
  }
});

function renderBackfill(data, httpStatus) {
  const banner = document.getElementById("bf-banner");
  const detail = document.getElementById("bf-detail");
  document.getElementById("bf-result").style.display = "block";
  const s = data.scope;
  let html = "";

  if (data.status === "ok") {
    banner.className = "banner ok";
    banner.textContent = "回补包已生成:" + data.message;
  } else if (data.status === "insufficient_scope") {
    banner.className = "banner err";
    banner.textContent = "获准范围不足,暂不出包。";
  } else if (data.status === "cannot_produce") {
    banner.className = "banner err";
    banner.textContent = "回补结果不完整,暂不出包:" + data.message;
  } else if (data.status === "insufficient_evidence") {
    banner.className = "banner err";
    banner.textContent = "证据不足,已停止回补:" + data.message;
  } else {
    banner.className = "banner err";
    banner.textContent = "输入有误:" + (data.message || ("HTTP " + httpStatus));
  }

  if (s) {
    html += `<div class="stat">
      <div><b>${esc(data.target_commit)}</b>目标提交</div>
      <div><b>${s.approved.length}</b>获准单号</div>
      <div><b>${s.required.length}</b>闭包内需回补单号</div>
      <div><b>${data.input_rows.waybills}</b>下游运单行</div>
      <div><b>${data.input_rows.receipts}</b>下游签收行</div>
    </div>
    <p class="muted">证据范围:提交 ${data.evidence.start_commit}–${data.evidence.end_commit},
    已应用变更 ${data.evidence.changes_applied} 条;起点快照 运单 ${data.evidence.snapshot_rows.waybills} 行、
    签收 ${data.evidence.snapshot_rows.receipts} 行。目标提交后导出:运单 ${data.target_rows.waybills} 行、
    签收 ${data.target_rows.receipts} 行。</p>
    <p class="muted">获准单号:${chips(s.approved)}</p>`;
    if (s.approved_no_rows && s.approved_no_rows.length) {
      html += `<p class="muted">以下获准单号在两版导出中都没有任何行(可能此前已回补),
        不产生变更:${chips(s.approved_no_rows)}</p>`;
    }

    if (data.status === "insufficient_scope") {
      html += `<h3 style="font-size:14px">至少还需补选 ${s.missing.length} 票</h3>
        <p>${chips(s.missing, "bad")}</p>`;
      if (s.uncovered.length) {
        html += `<table><tr><th>需补选运单</th><th>涉及签收号</th><th>关联明细(原运单 → 目标运单)</th></tr>`;
        for (const u of s.uncovered) {
          const links = u.关联明细.length
            ? u.关联明细.map(l => `${esc(l.签收号)}:${esc(l.原运单 ?? "(无)")} → ${esc(l.目标运单 ?? "(无)")}`
                + `(与 ${l.关联单号.map(esc).join("、")} 同票)`).join("<br>")
            : '<span class="muted">(无)</span>';
          html += `<tr><td><b>${esc(u.单号)}</b></td>
            <td>${chips(u.涉及签收号, "bad")}</td><td>${links}</td></tr>`;
        }
        html += `</table>`;
      }
      html += `<p class="muted" style="margin-top:10px">这些运单与已获准运单通过同一签收号(改挂)连在一起,
        必须整体回补,否则签收明细会只换一半。按上面的清单补选后重新提交即可。</p>`;
    }

    if (data.status === "cannot_produce" && data.violations) {
      html += `<h3 style="font-size:14px">回补后无对应运单的签收明细</h3>
        <table><tr><th>签收号</th><th>单号</th><th>说明</th></tr>`;
      for (const v of data.violations) {
        html += `<tr><td>${esc(v.签收号)}</td><td>${esc(v.单号)}</td><td>${esc(v.说明)}</td></tr>`;
      }
      html += `</table><p class="muted" style="margin-top:10px">建议扩大获准范围,
        把上述签收号对应的运单一起补入后再试。</p>`;
    }

    if (data.status === "ok" && data.stats) {
      const st = data.stats;
      html += `<table>
        <tr><th>文件</th><th>新增</th><th>删除</th><th>改值</th><th>变更合计</th></tr>
        <tr><td>运单 CSV</td><td>${st.waybills.added.length}</td><td>${st.waybills.removed.length}</td>
            <td>${st.waybills.changed.length}</td><td>${st.waybills.total_changed}</td></tr>
        <tr><td>签收 CSV</td><td>${st.receipts.added.length}</td><td>${st.receipts.removed.length}</td>
            <td>${st.receipts.changed.length}</td><td>${st.receipts.total_changed}</td></tr>
      </table>`;
      const p = data.package;
      const bytes = Uint8Array.from(atob(p.content_base64), c => c.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: "application/zip" }));
      html += `<a class="dl" id="bf-dl" href="${url}" download="${esc(p.filename)}">
        下载 ${esc(p.filename)}(${p.files.join(" + ")},${p.size_bytes} 字节)</a>
        <p class="muted">ZIP 内含 ${p.files.map(esc).join("、")};范围外的行保留原值,
        范围内的运单行及其名下签收明细整体取自目标提交版本。</p>`;
    }
  }
  detail.innerHTML = html;
  document.getElementById("bf-result").scrollIntoView({ behavior: "smooth" });
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/verify")
async def api_verify(
    waybill_csv: UploadFile = File(...),
    receipt_csv: UploadFile = File(...),
    evidence: UploadFile = File(...),
):
    try:
        tgt_a = load_waybill_csv(await waybill_csv.read())
        tgt_b = load_receipt_csv(await receipt_csv.read())
    except InputError as e:
        return JSONResponse({"status": "invalid_input", "message": str(e)}, status_code=400)

    raw = await evidence.read()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return JSONResponse(
            {"status": "invalid_input", "message": f"证据包不是合法 JSON: {e}"},
            status_code=400)
    try:
        ev = parse_evidence(data)
    except (InputError, EvidenceError) as e:
        return JSONResponse({"status": "invalid_input", "message": str(e)}, status_code=400)

    try:
        result = verify(ev, tgt_a, tgt_b)
    except EvidenceError as e:
        return {"status": "insufficient_evidence", "message": str(e)}

    if result["intersection"]:
        status, message = "ok", "两份文件在所查范围内存在共同来源提交区间"
    else:
        status, message = "no_common_source", "所查范围内没有共同来源"
    return {"status": status, "message": message, "result": result}


@app.get("/api/sample/waybills.csv", response_class=PlainTextResponse)
async def sample_waybills():
    _, wb_csv, _, _ = build_sample()
    return PlainTextResponse(wb_csv, media_type="text/csv; charset=utf-8")


@app.get("/api/sample/receipts.csv", response_class=PlainTextResponse)
async def sample_receipts():
    _, _, rc_csv, _ = build_sample()
    return PlainTextResponse(rc_csv, media_type="text/csv; charset=utf-8")


@app.get("/api/sample/evidence.json", response_class=PlainTextResponse)
async def sample_evidence():
    ev, _, _, _ = build_sample()
    return PlainTextResponse(
        json.dumps(ev, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8")


# ---- 回补 ----

async def _read_backfill_inputs(waybill_csv, receipt_csv, evidence,
                               target_commit, approved):
    """读取并校验回补请求的四类输入;校验失败抛 InputError。"""
    wb_raw = await waybill_csv.read()
    rc_raw = await receipt_csv.read()
    cur_a = load_waybill_csv_allow_empty(wb_raw)
    cur_b = load_receipt_csv_allow_empty(rc_raw)
    raw = await evidence.read()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InputError(f"证据包不是合法 JSON: {e}")
    try:
        ev = parse_evidence(data)
    except (InputError, EvidenceError) as e:
        raise InputError(str(e))
    try:
        commit = parse_target_commit(target_commit)
    except InputError as e:
        raise InputError(str(e))
    if commit < ev.start_commit or commit > ev.end_commit:
        raise InputError(
            f"目标提交号 {commit} 不在证据范围内 {ev.start_commit}..{ev.end_commit}")
    style = csv_style(wb_raw)
    style["crlf"] = style["crlf"] or csv_style(rc_raw)["crlf"]
    return ev, cur_a, cur_b, commit, parse_approved(approved), style


@app.post("/api/backfill")
async def api_backfill(
    waybill_csv: UploadFile = File(...),
    receipt_csv: UploadFile = File(...),
    evidence: UploadFile = File(...),
    target_commit: str = Form(...),
    approved: str = Form(""),
):
    try:
        ev, cur_a, cur_b, commit, approved_ids, style = await _read_backfill_inputs(
            waybill_csv, receipt_csv, evidence, target_commit, approved)
    except InputError as e:
        return JSONResponse({"status": "invalid_input", "message": str(e)},
                            status_code=400)
    if not approved_ids:
        return JSONResponse(
            {"status": "invalid_input", "message": "请填写获准回补的单号"},
            status_code=400)

    try:
        result = run_backfill(ev, cur_a, cur_b, commit, approved_ids, style)
    except EvidenceError as e:
        return {"status": "insufficient_evidence", "message": str(e)}
    except InputError as e:
        return JSONResponse({"status": "invalid_input", "message": str(e)},
                            status_code=400)
    except ScopeInsufficient as e:
        return JSONResponse(e.scope, status_code=409)
    except CannotProduce as e:
        payload = dict(e.result, status="cannot_produce",
                       message="回补后存在没有对应运单的签收明细",
                       violations=e.violations)
        return JSONResponse(payload, status_code=409)
    return result


@app.get("/api/backfill/sample/waybills.csv", response_class=PlainTextResponse)
async def backfill_sample_waybills():
    _, wb_csv, _, _ = build_backfill_sample()
    return PlainTextResponse(wb_csv, media_type="text/csv; charset=utf-8")


@app.get("/api/backfill/sample/receipts.csv", response_class=PlainTextResponse)
async def backfill_sample_receipts():
    _, _, rc_csv, _ = build_backfill_sample()
    return PlainTextResponse(rc_csv, media_type="text/csv; charset=utf-8")


@app.get("/api/backfill/sample/evidence.json", response_class=PlainTextResponse)
async def backfill_sample_evidence():
    ev, _, _, _ = build_backfill_sample()
    return PlainTextResponse(
        json.dumps(ev, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8")

