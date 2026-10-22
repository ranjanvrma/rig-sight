// Reading the Rig - bento frontend. Talks to api.py; no build step, no dependencies.
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pretty = (c) => String(c).replace(/_/g, " ");
const CATS = ["productive", "stuck_pipe", "lost_circulation", "tool_failure", "waiting_on_weather", "waiting_on_equipment", "well_control"];
const color = (c) => getComputedStyle(document.documentElement).getPropertyValue(`--c-${c}`).trim() || "#888";
const ROLE = { ingest_report: "Ingest", orchestrator: "Orchestrator", planner: "Planner", research: "Research", executor: "Executor",
  tools: "ToolNode", critic: "Critic", report_writer: "Writer", human_review: "Engineer" };
const SAMPLE = `WELL W-02  DAY 15  DEPTH 2,700 m  COST USD 431,000
00:00-05:00 Drilled 12-1/4 in hole from 2,610 m to 2,700 m
05:00-07:30 Mud motor failure, POOH to change motor
07:30-12:00 RIH with new motor
12:00-2:00 Circulated and conditioned mud
14:00-24:00 Waited on weather, swell above 4 m
`;

const S = { reports: [], current: null, thread: null, events: [], state: null, busy: false };

// ---------------- helpers ----------------
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 2600);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r;
}
async function stream(path, body) {
  const r = await api(path, { method: "POST", body: JSON.stringify(body) });
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      const ev = /event: (.+)/.exec(raw)?.[1]; const data = /data: ([\s\S]+)/.exec(raw)?.[1];
      if (ev && data) onEvent(ev, JSON.parse(data));
    }
  }
}
function md(text) {   // tiny, safe markdown for the insight report
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  let html = "", list = false;
  for (const line of String(text || "").split("\n")) {
    const close = () => { if (list) { html += "</ul>"; list = false; } };
    if (line.startsWith("### ")) { close(); html += `<h3>${inline(line.slice(4))}</h3>`; }
    else if (/^\*\*[^*]+\*\*/.test(line)) { close(); const m = /^\*\*([^*]+)\*\*(.*)$/.exec(line); html += `<h4>${esc(m[1])}<span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400"> ${esc(m[2])}</span></h4>`; }
    else if (line.startsWith("- ")) { if (!list) { html += "<ul>"; list = true; } html += `<li>${inline(line.slice(2))}</li>`; }
    else if (line.startsWith("> ")) { close(); html += `<blockquote>${inline(line.slice(2))}</blockquote>`; }
    else if (line.trim()) { close(); html += `<p>${inline(line)}</p>`; }
    else close();
  }
  return html + (list ? "</ul>" : "");
}
const fmt = (n, d = 1) => (n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));

// ---------------- model ----------------
async function loadStatus() {
  const s = await (await api("/api/status")).json();
  $("#modelSelect").value = s.backend;
  $("#modelDot").className = "dot " + (s.backend === "ollama" ? "ok" : s.ollama_ok ? "" : "warn");
  const hint = $("#modelHint");
  hint.className = "hint" + (!s.ollama_ok ? " warn" : "");
  hint.textContent = s.backend === "ollama" ? `Lines labelled by ${s.model} on this computer.`
    : s.ollama_ok ? "Mock keyword rules: instant, no model." : `${s.ollama_message}. Using the mock.`;
}
$("#modelSelect").addEventListener("change", async (e) => {
  const s = await (await api("/api/model", { method: "POST", body: JSON.stringify({ name: e.target.value }) })).json();
  if (e.target.value === "ollama" && s.backend !== "ollama") toast(s.ollama_message + ". Using the mock.");
  loadStatus();
});

// ---------------- reports ----------------
async function loadReports() {
  S.reports = await (await api("/api/reports")).json();
  const sel = $("#reportSelect");
  sel.innerHTML = S.reports.map((r) => `<option value="${r.id}">${r.well} · Day ${r.day} — ${esc(r.tag)}</option>`).join("")
    + `<option value="CUSTOM">✍ Custom report — paste your own</option>`;
  sel.value = "W-02_D14";
  selectReport();
}
function selectReport() {
  const id = $("#reportSelect").value;
  S.current = id; S.thread = null; S.state = null; S.events = [];
  const r = S.reports.find((x) => x.id === id);
  $("#reportTag").innerHTML = id === "CUSTOM" ? `<span class="pill">no answer key</span><span class="pill">errors are left in for the Critic</span>`
    : r.planted_error ? `<span class="pill err">planted error</span><span class="pill">the Critic should catch it</span>`
    : `<span class="pill ${r.tag === "clean day" ? "ok" : ""}">${esc(r.tag)}</span>`;
  $("#customText").classList.toggle("hidden", id !== "CUSTOM");
  $("#ddr").classList.toggle("hidden", id === "CUSTOM");
  if (id === "CUSTOM" && !$("#customText").value) $("#customText").value = SAMPLE;
  renderAll();
}
$("#reportSelect").addEventListener("change", selectReport);

// ---------------- running ----------------
$("#runBtn").addEventListener("click", async () => {
  if (S.busy) return;
  const body = { report_id: S.current, critic: $("#criticToggle").checked };
  if (S.current === "CUSTOM") body.report_text = $("#customText").value;
  S.events = []; S.state = null; renderAll();
  await go("/api/runs", body);
});
async function go(path, body) {
  S.busy = true; $("#runBtn").disabled = true; setLive("on", "running");
  try { await stream(path, body); }
  catch (e) { toast(e.message); setLive("", "error"); }
  finally { S.busy = false; $("#runBtn").disabled = false; }
}
function onEvent(ev, data) {
  if (ev === "start") S.thread = data.thread;
  if (ev === "step") { S.events.push(data); renderTrace(); renderPipeline(); }
  if (ev === "error") { toast(data.message); setLive("", "error"); }
  if (ev === "done") {
    S.state = data;
    if (S.current === "CUSTOM") $("#customText").classList.add("hidden"), $("#ddr").classList.remove("hidden");
    setLive(data.paused ? "wait" : "done", data.paused ? "waiting for you" : data.engineer_decision === "approve" ? "approved" : "done");
    renderAll();
  }
}
function setLive(cls, text) { const b = $("#liveBadge"); b.className = "live " + cls; b.textContent = text; }

// ---------------- rendering ----------------
function renderAll() {
  renderKpis(); renderReport(); renderTrace(); renderPipeline(); renderCharts(); renderCritic(); renderInsight(); renderReview(); renderAnswerKey();
  if (!S.events.length && !S.busy) setLive("", "idle");
}
function kpi(id, value, sub, cls = "") {
  const el = $(id); $(".kpi-value", el).textContent = value; const s = $(".kpi-sub", el); s.textContent = sub || " "; s.className = "kpi-sub " + cls;
}
function renderKpis() {
  const v = S.state;
  if (!v) { ["#kpiNpt", "#kpiHours", "#kpiDepth", "#kpiRop", "#kpiCritic"].forEach((id) => kpi(id, "—", id === "#kpiNpt" ? "run a report" : "")); return; }
  const total = v.total_hours ?? 0, ok24 = Math.abs(total - 24) < 0.01;
  kpi("#kpiNpt", `${fmt(v.total_npt)} h`, `${fmt((v.total_npt / 24) * 100, 0)}% of the day lost`);
  kpi("#kpiHours", `${fmt(total, 2)} h`, ok24 ? "complete" : "incomplete", ok24 ? "good" : "bad");
  const d = v.depth_vs_plan_m;
  kpi("#kpiDepth", d == null ? "n/a" : `${d > 0 ? "+" : ""}${fmt(d, 0)} m`, d == null ? "no plan for this day" : d > 0 ? "ahead of plan" : d < 0 ? "behind plan" : "on plan", d < 0 ? "bad" : d > 0 ? "good" : "");
  kpi("#kpiRop", v.rop?.rop_m_per_h ? `${fmt(v.rop.rop_m_per_h)}` : "—", v.rop?.drilled_m ? `m/h · ${fmt(v.rop.drilled_m, 0)} m drilled` : "no drilling");
  const fails = (v.attempts || []).slice(0, -1).filter((a) => a.verdict === "FAIL").length;
  kpi("#kpiCritic", v.critic_verdict || (v.critic_on ? "—" : "OFF"), v.critic_on ? `${fails} retr${fails === 1 ? "y" : "ies"} · ${(v.attempts || []).length} check(s)` : "self-check skipped",
    v.critic_verdict === "PASS" ? "good" : v.critic_verdict === "FAIL" ? "bad" : "");
  $("#costBadge").textContent = v.cost_status && v.cost_status !== "unknown" ? `Day cost ${v.cost_status}` : "";
}
function renderReport() {
  const r = S.reports.find((x) => x.id === S.current);
  const text = S.state?.report_text ?? (S.current === "CUSTOM" ? $("#customText").value : r?.text ?? "");
  const acts = Object.fromEntries((S.state?.activities || []).map((a) => [a.source_line, a]));
  const bad = Object.fromEntries((S.state?.parse_issues || []).map((i) => [i.line, i]));
  $("#ddr").innerHTML = text.split("\n").filter((l, i, arr) => l || i < arr.length - 1).map((line, i) => {
    const n = i + 1, a = acts[n]; let style = "", chip = "", note = "";
    if (bad[n]) { style = "border-left-color:#ef4444;background:#ef444418"; chip = `<span class="chip" style="background:#ef4444">source error · not used</span>`;
      note = `<span class="note" style="color:#fca5a5">↳ ${esc(bad[n].detail)}</span>`; }
    else if (a) {
      if (a.category !== "productive") { const c = color(a.category); style = `border-left-color:${c};background:${c}18`;
        chip = `<span class="chip" style="background:${c}">${pretty(a.category)} · ${fmt(a.hours, 2)} h · ${a.confidence.toFixed(2)}</span>`; }
      if (a.note) note = `<span class="note">↳ ${esc(a.note)}</span>`;
    }
    return `<div class="row${n === 1 ? " hdr" : ""}" style="${style}"><span class="ln">${n}</span><span class="tx">${esc(line)}${note}</span>${chip}</div>`;
  }).join("");
  const used = new Set((S.state?.activities || []).map((a) => a.category));
  $("#legend").innerHTML = S.state ? CATS.filter((c) => used.has(c)).map((c) => `<span><i style="background:${color(c)}"></i>${pretty(c)}</span>`).join("") : "";
}
function renderTrace() {
  const ol = $("#trace");
  if (!S.events.length) { ol.innerHTML = `<li class="empty">Agent steps stream here as they happen.</li>`; return; }
  ol.innerHTML = S.events.map((e, i) => {
    let s = e.summary;
    if (e.node === "orchestrator") s = `route() → ${ROLE[S.events[i + 1]?.node] || "…"}`;
    const minor = e.node === "orchestrator" || e.node === "tools";
    const cls = /^FAIL|REJECTED/.test(s) || s.includes("REJECTED") ? "fail" : /^PASS|APPROVED/.test(s) || s.includes("APPROVED") ? "pass" : "";
    return `<li class="${minor ? "minor" : ""}"><span class="who">${ROLE[e.node] || e.node}</span><span class="${cls}">${esc(s)}</span></li>`;
  }).join("");
  ol.scrollTop = ol.scrollHeight;
}
function renderPipeline() {
  const seen = new Set(S.events.map((e) => e.node)); const last = S.events[S.events.length - 1]?.node;
  const failed = S.events.some((e) => e.node === "critic" && e.summary.startsWith("FAIL"));
  const names = ["ingest_report", "planner", "research", "executor", "tools", "critic", "report_writer", "human_review"];
  $("#pipeline").innerHTML = names.map((n) => {
    let cls = seen.has(n) ? "done" : ""; if (n === last && S.busy) cls = "active";
    if (n === "human_review" && S.state?.paused) cls = "active";
    if (n === "critic" && failed && !S.busy && S.state?.critic_verdict === "FAIL") cls = "done fail";
    return `<div class="stage ${cls}">${ROLE[n]}</div>`;
  }).join("");
}

// charts (hand-drawn SVG)
const tip = Object.assign(document.createElement("div"), { className: "tooltip" }); document.body.appendChild(tip);
function bindTips(root) {
  root.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mousemove", (e) => { tip.style.display = "block"; tip.innerHTML = el.dataset.tip; tip.style.left = e.clientX + 14 + "px"; tip.style.top = e.clientY + 14 + "px"; });
    el.addEventListener("mouseleave", () => (tip.style.display = "none"));
  });
}
function renderCharts() {
  const acts = S.state?.activities || [];
  if (!acts.length) { $("#timeline").innerHTML = `<p class="muted">The 24-hour timeline appears after a run.</p>`; $("#bars").innerHTML = ""; return; }
  const W = 800, H = 70, x = (m) => (m / 1440) * W;
  let svg = `<svg viewBox="0 -4 ${W} ${H + 22}" role="img" aria-label="24 hour timeline">`;
  for (const a of acts) {
    const w = Math.max(1, x(a.end_min) - x(a.start_min) - 2);
    svg += `<rect x="${x(a.start_min) + 1}" y="0" width="${w}" height="${H - 14}" rx="5" fill="${color(a.category)}"
      data-tip="<b>${a.start}–${a.end}</b> · ${fmt(a.hours, 2)} h<br>${esc(a.text)}<br><span style='color:#aaa'>${pretty(a.category)} · line ${a.source_line}</span>"></rect>`;
  }
  for (let h = 0; h <= 24; h += 3) svg += `<text x="${x(h * 60)}" y="${H + 12}" text-anchor="${h === 0 ? "start" : h === 24 ? "end" : "middle"}">${h}:00</text>`;
  $("#timeline").innerHTML = svg + "</svg>";
  const npt = Object.entries(S.state.npt_hours || {}).sort((a, b) => b[1] - a[1]);
  if (!npt.length) { $("#bars").innerHTML = `<p class="muted" style="color:var(--ok)">Clean day: no non-productive time.</p>`; bindTips($("#timeline")); return; }
  const max = Math.max(...npt.map((d) => d[1])), rowH = 30, LW = 170, BW = W - LW - 60;
  let b = `<svg viewBox="0 0 ${W} ${npt.length * rowH}" role="img" aria-label="NPT hours by category">`;
  npt.forEach(([c, h], i) => {
    const y = i * rowH, w = Math.max(4, (h / max) * BW);
    b += `<text class="cat" x="${LW - 10}" y="${y + 19}" text-anchor="end">${pretty(c)}</text>
      <rect x="${LW}" y="${y + 6}" width="${w}" height="18" rx="4" fill="${color(c)}" data-tip="${pretty(c)}: <b>${fmt(h, 2)} h</b>"></rect>
      <text class="val" x="${LW + w + 8}" y="${y + 19}">${fmt(h, 2)} h</text>`;
  });
  $("#bars").innerHTML = b + "</svg>";
  bindTips($("#timeline")); bindTips($("#bars"));
}
function renderCritic() {
  const v = S.state, el = $("#critic");
  if (!v) { el.innerHTML = `<p class="muted">No checks yet.</p>`; return; }
  if (!v.critic_on) { el.innerHTML = `<p class="muted">The Critic is switched off for this run: nothing was checked.</p>`; return; }
  el.innerHTML = (v.attempts || []).map((a, i) => `<div class="check ${a.verdict === "PASS" ? "pass" : "fail"}">
      <div class="check-head"><span>Check ${i + 1} · ${fmt(a.total_hours, 2)} h logged · ${fmt(a.total_npt, 2)} h NPT</span><span class="v">${a.verdict}</span></div>
      ${a.problems.length ? `<ul>${a.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}</div>`).join("")
    + (v.critic_warnings || []).map((w) => `<div class="warnline">⚠ ${esc(w)}</div>`).join("")
    + (v.reference_warnings || []).map((w) => `<div class="warnline">⚠ ${esc(w)}</div>`).join("");
}
function renderInsight() {
  $("#insight").innerHTML = S.state?.draft_report ? md(S.state.draft_report) : `<p class="muted">The draft appears here when the agents finish.</p>`;
}
function renderReview() {
  const v = S.state, el = $("#review");
  if (!v) { el.innerHTML = `<p class="muted">Nothing to review yet.</p>`; return; }
  if (v.engineer_decision === "approve" && !v.paused) {
    el.innerHTML = `<div class="stamp"><b>APPROVED</b>${v.engineer_comment ? esc(v.engineer_comment) + "<br>" : ""}Computed · Sourced · Approved</div>`; return;
  }
  if (!v.paused) { el.innerHTML = `<p class="muted">Not waiting for a review.</p>`; return; }
  el.innerHTML = `<p class="muted" style="margin-bottom:10px">The graph is paused at <code>interrupt()</code>. Nothing goes out until you decide.</p>
    <textarea id="comment" placeholder="e.g. line 7 is planned, not NPT"></textarea>
    <div class="review-actions"><button class="btn primary" id="approveBtn">Approve</button><button class="btn" id="rejectBtn">Reject &amp; re-run</button></div>
    <p class="muted" style="margin-top:8px;font-size:12px">Reject with “line N is &lt;category&gt;” (or “planned”) to correct a line on the re-run.</p>`;
  const send = (decision) => go(`/api/runs/${S.thread}/review`, { decision, comment: $("#comment").value });
  $("#approveBtn").onclick = () => send("approve");
  $("#rejectBtn").onclick = () => send("reject");
}
function renderAnswerKey() {
  const v = S.state, k = v?.answer_key, el = $("#answerKey");
  if (!v) { el.innerHTML = `<p class="muted">Synthetic reports have a known answer.</p>`; return; }
  if (!k) { el.innerHTML = `<p class="muted">Custom report: no answer key to compare with.</p>`; return; }
  const rows = [["Hours logged", 24, v.total_hours], ["Total NPT (h)", k.total_npt, v.total_npt], ["Depth vs plan (m)", k.depth_vs_plan_m, v.depth_vs_plan_m],
    ["ROP (m/h)", k.rop_m_per_h, v.rop?.rop_m_per_h],
    ...[...new Set([...Object.keys(k.npt_hours), ...Object.keys(v.npt_hours || {})])].map((c) => [`${pretty(c)} (h)`, k.npt_hours[c] || 0, v.npt_hours?.[c] || 0])];
  el.innerHTML = `<table><tr><th></th><th>Expected</th><th>Agents</th><th></th></tr>${rows.map(([n, e, a]) => {
    const ok = a != null && Math.abs(Number(e ?? 0) - Number(a ?? 0)) < 0.06;
    return `<tr><td>${n}</td><td>${fmt(e, 2)}</td><td>${fmt(a, 2)}</td><td class="${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"}</td></tr>`;
  }).join("")}</table>`;
}

// ---------------- evaluation ----------------
async function loadEval() {
  const m = await (await api("/api/metrics")).json(), g = $("#evalGrid");
  const o = m.ollama?.runs; if (!o) { g.innerHTML = `<div class="tile span-12"><p class="muted">No results yet. Run python evaluate.py.</p></div>`; return; }
  const G = o["Graph with Critic"], N = o["Graph without Critic"], C = o["Single-prompt chatbot"];
  const bars = [["Graph with Critic", G.npt_hours_mae, "var(--orange)"], ["Graph without Critic", N.npt_hours_mae, "#9a4a30"], ["Single-prompt chatbot", C.npt_hours_mae, "#6b665c"]];
  const max = Math.max(...bars.map((b) => b[1])), W = 760;
  const svg = `<svg viewBox="0 0 ${W} ${bars.length * 46}" role="img" aria-label="NPT error by configuration">${bars.map(([n, v, c], i) => {
    const w = Math.max(6, (v / max) * (W - 260));
    return `<text class="cat" x="180" y="${i * 46 + 26}" text-anchor="end">${n}</text><rect x="192" y="${i * 46 + 10}" width="${w}" height="24" rx="5" fill="${c}"></rect>
      <text class="val" x="${200 + w}" y="${i * 46 + 27}">${v.toFixed(2)} h</text>`;
  }).join("")}</svg>`;
  const table = (runs) => `<table class="data"><tr><th>Run</th><th>NPT error</th><th>Category acc.</th><th>Critic catch</th><th>Evidence</th><th>24 h logs</th><th>Exact NPT</th></tr>${Object.entries(runs).map(([n, r]) =>
    r.note && r.npt_hours_mae == null ? `<tr><td>${n}</td><td colspan="6" class="muted">${esc(r.note)}</td></tr>` :
    `<tr><td>${n}</td><td class="${n === "Graph with Critic" ? "hl" : ""}">${r.npt_hours_mae.toFixed(2)} h</td><td>${(r.category_accuracy * 100).toFixed(1)}%</td><td>${r.critic_catch_rate == null ? "n/a" : (r.critic_catch_rate * 2).toFixed(0) + " / 2"}</td><td>${(r.evidence_rate * 100).toFixed(0)}%</td><td>${esc(String(r.logs_adding_to_24h).split(" / ")[0])}</td><td>${esc(String(r.reports_exact_npt).split(" / ")[0])}</td></tr>`).join("")}</table>`;
  g.innerHTML = `
    <div class="tile kpi accent span-4"><div class="tile-label">Our graph · NPT error per report</div><div class="stat-big orange">${G.npt_hours_mae.toFixed(2)} h</div><div class="kpi-sub">${m.ollama.meta.model}, 15 reports</div></div>
    <div class="tile kpi span-4"><div class="tile-label">Single-prompt chatbot · same model</div><div class="stat-big">${C.npt_hours_mae.toFixed(2)} h</div><div class="kpi-sub">about ${Math.round(C.npt_hours_mae / G.npt_hours_mae)}× more error</div></div>
    <div class="tile kpi span-4"><div class="tile-label">Planted errors caught by the Critic</div><div class="stat-big">${(G.critic_catch_rate * 2).toFixed(0)} / 2</div><div class="kpi-sub">before any human saw them</div></div>
    <div class="tile span-7"><div class="tile-label">Mean NPT hours error · lower is better</div><div class="chart">${svg}</div></div>
    <div class="tile span-5"><div class="tile-label">How to read it</div><p>“Without Critic” looks slightly better only because two mistakes cancel on W-02 Day 14. Only ${esc(N.logs_adding_to_24h)} of its logs add up to 24 h. The chatbot cannot add up the log at all: ${esc(String(C.logs_adding_to_24h).split(" / ")[0])}.</p></div>
    <div class="tile span-12"><div class="tile-label">${esc(m.ollama.meta.model)} · generated ${esc(m.ollama.meta.generated)}</div><div class="scroll">${table(o)}</div></div>
    ${m.mock ? `<div class="tile span-12"><div class="tile-label">Mock keyword classifier (fallback) · optimistic: its keywords match the synthetic data</div><div class="scroll">${table(m.mock.runs)}</div></div>` : ""}`;
}

// ---------------- memory ----------------
async function loadMemory() {
  const m = await (await api("/api/memory")).json(), g = $("#memGrid");
  const t = (rows, cols) => rows.length ? `<table class="data"><tr>${cols.map((c) => `<th>${c}</th>`).join("")}</tr>${rows.map((r) => `<tr>${cols.map((c) => `<td>${esc(r[c])}</td>`).join("")}</tr>`).join("")}</table>` : `<p class="muted">Empty</p>`;
  g.innerHTML = `
    <div class="tile kpi span-4"><div class="tile-label">Past incidents</div><div class="stat-big">${m.incidents.length}</div><div class="kpi-sub">cause → fix, read by Research</div></div>
    <div class="tile kpi span-4"><div class="tile-label">Failed cases</div><div class="stat-big">${m.failed_cases.length}</div><div class="kpi-sub">written by the Critic</div></div>
    <div class="tile kpi span-4"><div class="tile-label">Engineer decisions</div><div class="stat-big">${m.engineer_feedback.length}</div><div class="kpi-sub"><button class="btn" id="resetMem" style="padding:4px 10px;font-size:12px">Reset to seed data</button></div></div>
    <div class="tile span-12"><div class="tile-label">Past NPT incidents</div><div class="scroll">${t(m.incidents, ["well_id", "day", "category", "cause", "fix", "hours_lost"])}</div></div>
    <div class="tile span-7"><div class="tile-label">Failed cases from the Critic</div><div class="scroll">${t(m.failed_cases, ["report_id", "attempt", "problems"])}</div></div>
    <div class="tile span-5"><div class="tile-label">Engineer decisions</div><div class="scroll">${t(m.engineer_feedback, ["report_id", "decision", "comment"])}</div></div>`;
  $("#resetMem").onclick = async () => { await api("/api/memory/reset", { method: "POST" }); toast("Memory reset to the seed data"); loadMemory(); };
}

// ---------------- tabs ----------------
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${b.dataset.view}`));
  if (b.dataset.view === "evaluation") loadEval().catch((e) => toast(e.message));
  if (b.dataset.view === "memory") loadMemory().catch((e) => toast(e.message));
}));

loadStatus().catch(() => {});
loadReports().catch((e) => toast("Could not reach the API: " + e.message));
