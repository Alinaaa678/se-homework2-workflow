"""Offline HTML replay: turn a run log + flow definition into a web page.

Self-contained (no CDN, no server): the flow diagram is drawn with inline
SVG, and all data is embedded as JSON. Open the generated file in any
browser — works offline, safe for classroom demos.

    python main.py --replay runs/run-XXXX.jsonl
"""

from __future__ import annotations

import html
import json
import os
from typing import Any, Dict, List

TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Workflow 运行回放 — __RUN_ID__</title>
<style>
  body { font-family: "Microsoft YaHei", system-ui, sans-serif; margin: 0; background: #f6f7f9; color: #222; }
  header { background: #1f2937; color: #fff; padding: 18px 28px; }
  header h1 { font-size: 20px; margin: 0 0 6px; }
  header .meta { font-size: 13px; opacity: .85; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 13px; margin-left: 8px; }
  .success { background: #16a34a; } .failed { background: #dc2626; }
  main { max-width: 1080px; margin: 0 auto; padding: 20px; }
  section { background: #fff; border-radius: 10px; padding: 18px 22px; margin-bottom: 18px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  h2 { font-size: 16px; margin: 0 0 12px; border-left: 4px solid #2563eb; padding-left: 8px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }
  th { background: #f3f4f6; }
  tr.err td { background: #fef2f2; }
  code, pre { font-family: Consolas, monospace; font-size: 12.5px; }
  pre { background: #0f172a; color: #e2e8f0; padding: 12px; border-radius: 8px; overflow: auto; max-height: 320px; }
  details { margin-bottom: 10px; }
  summary { cursor: pointer; font-weight: 600; padding: 4px 0; }
  .node-box { fill: #fff; stroke: #2563eb; stroke-width: 1.5; rx: 8; }
  .node-parallel { stroke: #9333ea; }
  .node-text { font-size: 12px; font-family: inherit; }
  .edge { stroke: #9ca3af; stroke-width: 1.5; fill: none; marker-end: url(#arrow); }
  .edge-back { stroke-dasharray: 5 4; }
  .edge-label { font-size: 10.5px; fill: #6b7280; }
</style>
</head>
<body>
<header>
  <h1>Workflow 运行回放 <span class="badge __STATUS_CLASS__">__STATUS__</span></h1>
  <div class="meta">流程：__FLOW_NAME__ ｜ 运行 ID：__RUN_ID__ ｜ 步数：__STEP_COUNT__ ｜ 消息数：__MSG_COUNT__</div>
</header>
<main>
  <section><h2>流程图（节点上标注执行次数）</h2><div id="diagram"></div></section>
  <section><h2>步骤时间线</h2><div id="steps"></div></section>
  <section><h2>Agent 间消息记录</h2><div id="messages"></div></section>
  <section><h2>工件版本记录（完整内容与哈希）</h2><div id="artifact-history"></div></section>
  <section><h2>运行指标与验收证据</h2><div id="evidence"></div></section>
  <section><h2>最终产物</h2><div id="artifacts"></div></section>
</main>
<script>
const FLOW = __FLOW_JSON__;
const EVENTS = __EVENTS_JSON__;
const ARTIFACTS = __ARTIFACTS_JSON__;

// ---- layered layout: BFS from start, back-edges drawn dashed ----
const nodes = FLOW.nodes, edges = FLOW.edges || [];
const depth = {[FLOW.start]: 0};
let frontier = [FLOW.start];
while (frontier.length) {
  const next = [];
  for (const id of frontier) {
    for (const e of edges) {
      if (e.from !== id || e.to === "END") continue;
      if (depth[e.to] === undefined) { depth[e.to] = depth[id] + 1; next.push(e.to); }
    }
  }
  frontier = next;
}
nodes.forEach(n => { if (depth[n.id] === undefined) depth[n.id] = 0; });
const layers = {};
nodes.forEach(n => { (layers[depth[n.id]] = layers[depth[n.id]] || []).push(n.id); });
const maxDepth = Math.max(...Object.values(depth));
const pos = {};
Object.entries(layers).forEach(([d, ids]) => ids.forEach((id, i) => pos[id] = { x: 80 + d * 260, y: 60 + i * 110 }));
if (edges.some(e => e.to === "END")) pos.END = { x: 80 + (maxDepth + 1) * 260, y: 60 };
const visits = {};
EVENTS.filter(e => e.event === "step").forEach(e => visits[e.node] = Math.max(visits[e.node] || 0, e.visit));

const W = 200, H = 54;
function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
let svg = `<svg width="${80 + (maxDepth + 2) * 260}" height="${Math.max(1, ...Object.keys(layers).map(d => layers[d].length)) * 110 + 80}">
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#9ca3af"/></marker></defs>`;
for (const e of edges) {
  const a = pos[e.from], b = pos[e.to];
  if (!a || !b) continue;
  const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + (e.to === "END" ? 15 : H / 2);
  const back = e.to !== "END" && depth[e.to] <= depth[e.from];
  const path = back
    ? `M${a.x + W / 2},${a.y + H} C${a.x + W / 2},${a.y + H + 45} ${b.x + W / 2},${b.y + H + 45} ${b.x + W / 2},${b.y + H}`
    : `M${x1},${y1} C${x1 + 60},${y1} ${x2 - 60},${y2} ${x2},${y2}`;
  svg += `<path class="edge ${back ? "edge-back" : ""}" d="${path}"/>`;
  if (e.when) svg += `<text class="edge-label" x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 6}">${esc(e.when)}</text>`;
}
for (const n of nodes) {
  const p = pos[n.id], isPar = n.type === "parallel";
  const sub = isPar ? "并行: " + n.tasks.map(t => t.agent).join(" + ") : (n.agent || "");
  const v = visits[n.id] ? ` ×${visits[n.id]}` : "";
  svg += `<rect class="node-box ${isPar ? "node-parallel" : ""}" x="${p.x}" y="${p.y}" width="${W}" height="${H}" rx="8"/>
  <text class="node-text" x="${p.x + W / 2}" y="${p.y + 24}" text-anchor="middle" font-weight="700">${esc(n.id)}${v}</text>
  <text class="node-text" x="${p.x + W / 2}" y="${p.y + 42}" text-anchor="middle" fill="#6b7280">${esc(sub)}</text>`;
}
if (pos.END) svg += `<circle cx="${pos.END.x + 15}" cy="${pos.END.y + 15}" r="16" fill="#16a34a"/><text x="${pos.END.x + 15}" y="${pos.END.y + 19}" text-anchor="middle" fill="#fff" font-size="10" font-weight="700">END</text>`;
svg += "</svg>";
document.getElementById("diagram").innerHTML = svg;

// ---- steps timeline ----
const steps = EVENTS.filter(e => e.event === "step");
let rows = steps.map(s => {
  const taskDetail = (s.tasks || []).map(t => `${esc(t.task)}(${esc(t.agent)}) ${t.duration_ms}ms`).join("<br>");
  return `<tr class="${s.status === "ok" ? "" : "err"}"><td>${s.node}</td><td>${s.visit}</td><td>${s.agent}</td>
    <td>${s.duration_ms ?? "-"}ms${taskDetail ? "<br>" + taskDetail : ""}</td>
    <td>${s.status === "ok" ? "✅ " + esc(String(s.output_key)) : "❌ " + esc(s.error || "")}</td></tr>`;
}).join("");
document.getElementById("steps").innerHTML =
  `<table><tr><th>节点</th><th>第几次执行</th><th>Agent</th><th>耗时</th><th>结果</th></tr>${rows}</table>`;

// ---- messages ----
const msgs = EVENTS.filter(e => e.event === "message");
document.getElementById("messages").innerHTML =
  `<table><tr><th>#</th><th>从</th><th>到</th><th>类型</th><th>内容摘要</th></tr>` +
  msgs.map((m, i) => `<tr><td>${i + 1}</td><td>${esc(m.from)}</td><td>${esc(m.to)}</td><td>${esc(m.kind)}</td><td><code>${esc(m.payload)}</code></td></tr>`).join("") +
  "</table>";

// ---- complete artifact versions and content addresses ----
const artifactEvents = EVENTS.filter(e => e.event === "artifact");
document.getElementById("artifact-history").innerHTML = artifactEvents.map((a, i) => {
  const value = typeof a.value === "string" ? a.value : JSON.stringify(a.value, null, 2);
  const version = a.version ? ` · v${a.version}` : "";
  const hash = a.hash ? `<br><small>${esc(a.hash)}</small>` : "";
  return `<details><summary>#${i + 1} ${esc(a.node)} · 第${a.visit}次执行 · ${esc(a.key)}${version}${hash}</summary><pre>${esc(value)}</pre></details>`;
}).join("") || "<p>（该日志没有完整工件事件；请使用新版流程重新运行）</p>";

// ---- metrics and independently derived verdict ----
const endEvent = [...EVENTS].reverse().find(e => e.event === "run_end") || {};
const metrics = endEvent.metrics || {};
let evidence = `<table><tr><th>模型调用</th><th>重试</th><th>总 Token</th><th>节点耗时</th></tr>
  <tr><td>${metrics.llm_calls || 0}</td><td>${metrics.llm_retries || 0}</td><td>${metrics.total_tokens || 0}</td><td>${metrics.node_duration_ms || 0}ms</td></tr></table>`;
if (ARTIFACTS.verdict) {
  const v = ARTIFACTS.verdict;
  evidence += `<p><strong>最终状态：${esc(v.status || "unknown")}</strong>；代码哈希：<code>${esc(v.target_code_hash || "")}</code></p>`;
  evidence += "<table><tr><th>检查</th><th>结果</th></tr>" + Object.entries(v.checks || {}).map(
    ([key, ok]) => `<tr><td>${esc(key)}</td><td>${ok ? "✅" : "❌"}</td></tr>`
  ).join("") + "</table>";
}
if (ARTIFACTS.acceptance) {
  const a = ARTIFACTS.acceptance;
  evidence += `<details><summary>独立验收：${a.passed ? "通过" : "失败"}</summary><pre>${esc((a.run_result || {}).output || "")}</pre></details>`;
}
document.getElementById("evidence").innerHTML = evidence;

// ---- final artifacts ----
let art = "";
if (ARTIFACTS.review) {
  const r = ARTIFACTS.review;
  art += `<details open><summary>审查结论：${r.passed ? "✅ 通过" : "❌ 未通过"}</summary>
    <p>${esc(r.summary || "")}</p>
    <table><tr><th>级别</th><th>来源</th><th>问题</th></tr>` +
    (r.issues || []).map(i => `<tr><td>${esc(i.severity || "")}</td><td>${esc(i.source || "")}</td><td>${esc(i.desc || "")}</td></tr>`).join("") +
    "</table></details>";
}
if (ARTIFACTS.code) art += `<details><summary>最终代码（点击展开）</summary><pre>${esc(ARTIFACTS.code)}</pre></details>`;
document.getElementById("artifacts").innerHTML = art || "<p>（无产物，运行未成功）</p>";
</script>
</body>
</html>
"""


def artifacts_from_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reconstruct the latest value of every artifact from a run log."""
    artifacts: Dict[str, Any] = {}
    for event in events:
        if event.get("event") == "artifact":
            artifacts[event["key"]] = event.get("value")
    return artifacts


def render_replay(
    flow: Dict[str, Any],
    events: List[Dict[str, Any]],
    artifacts: Dict[str, Any],
    out_path: str,
) -> str:
    """Render one run as a self-contained HTML file; returns out_path."""
    run_id = next((e.get("run_id") for e in events if e.get("run_id")), "unknown")
    end = next((e for e in reversed(events) if e.get("event") == "run_end"), {})
    status = end.get("status", "unknown")
    steps = [e for e in events if e.get("event") == "step"]
    msgs = [e for e in events if e.get("event") == "message"]

    page = (
        TEMPLATE
        .replace("__RUN_ID__", html.escape(str(run_id)))
        .replace("__FLOW_NAME__", html.escape(str(flow.get("name", ""))))
        .replace("__STATUS__", html.escape(status))
        .replace("__STATUS_CLASS__", "success" if status == "success" else "failed")
        .replace("__STEP_COUNT__", str(len(steps)))
        .replace("__MSG_COUNT__", str(end.get("message_count", len(msgs))))
        .replace("__FLOW_JSON__", json.dumps(flow, ensure_ascii=False))
        .replace("__EVENTS_JSON__", json.dumps(events, ensure_ascii=False))
        .replace("__ARTIFACTS_JSON__", json.dumps(artifacts, ensure_ascii=False))
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return out_path


def replay_path_for(log_path: str) -> str:
    """runs/run-X.jsonl -> runs/run-X.html"""
    return os.path.splitext(log_path)[0] + ".html"
