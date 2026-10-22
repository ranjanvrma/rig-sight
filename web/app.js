// Reading the Rig - frontend. Talks to api.py; plain ES modules, no build step, no dependencies.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const icon = (id) => `<svg aria-hidden="true"><use href="#i-${id}"/></svg>`;
const pretty = (c) => String(c).replace(/_/g, " ");
const fmt = (n, d = 1) => (n == null || Number.isNaN(Number(n)) ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const color = (c) => getComputedStyle(document.documentElement).getPropertyValue(`--c-${c}`).trim() || "#888";
const CATS = ["productive", "stuck_pipe", "lost_circulation", "tool_failure", "waiting_on_weather", "waiting_on_equipment", "well_control"];
const ROLE = { ingest_report: "Ingest", orchestrator: "Orchestrator", planner: "Planner", research: "Research", executor: "Executor",
  tools: "ToolNode", critic: "Critic", report_writer: "Writer", human_review: "Engineer" };
const STAGES = ["ingest_report", "planner", "research", "executor", "tools", "critic", "report_writer", "human_review"];
const MAX_CHARS = 20000;
const SAMPLE = `WELL W-02  DAY 15  DEPTH 2,700 m  COST USD 431,000
00:00-05:00 Drilled 12-1/4 in hole from 2,610 m to 2,700 m
05:00-07:30 Mud motor failure, POOH to change motor
07:30-12:00 RIH with new motor
12:00-2:00 Circulated and conditioned mud
14:00-24:00 Waited on weather, swell above 4 m
`;
const ROUTES = {
  "/": ["analyse", "Reading the Rig · Agentic AI for Daily Drilling Reports"],
  "/evaluation": ["evaluation", "Evaluation · Reading the Rig"],
  "/memory": ["memory", "Incident memory · Reading the Rig"],
  "/architecture": ["architecture", "Architecture · Reading the Rig"],
};
const S = { reports: [], current: null, thread: null, events: [], state: null, busy: false };

// ---------------- feedback ----------------
function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.className = "toast"), 3000);
}
function banner(msg, retry) {
  const b = $("#banner");
  if (!msg) { b.classList.add("hidden"); return; }
  $("#bannerText").textContent = msg; b.classList.remove("hidden");
  $("#bannerRetry").onclick = () => { b.classList.add("hidden"); retry(); };
}
function confirmDialog(title, text, okLabel) {
  const d = $("#confirm");
  $("#confirmTitle").textContent = title; $("#confirmText").textContent = text; $("#confirmOk").textContent = okLabel;
  if (!d.showModal) return Promise.resolve(window.confirm(text));   // very old browsers
  d.showModal();
  return new Promise((res) => d.addEventListener("close", () => res(d.returnValue === "ok"), { once: true }));
}

// ---------------- network ----------------
async function api(path, opts = {}) {
  let r;
  try { r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts }); }
  catch { throw new Error("Can't reach the server. Check your connection and try again."); }
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${r.status})`);
  }
  return r;
}
const getJSON = async (p) => (await api(p)).json();
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

// ---------------- theme + menu ----------------
function applyThemeButton() {
  const t = document.documentElement.dataset.theme, b = $("#themeBtn");
  b.setAttribute("aria-label", t === "dark" ? "Switch to light theme" : "Switch to dark theme");
  b.innerHTML = icon(t === "dark" ? "sun" : "moon");
}
$("#themeBtn").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("rig-theme", next); } catch { /* storage blocked: theme lasts for this visit */ }
  applyThemeButton(); renderCharts(); renderReport();
});
$("#menuBtn").addEventListener("click", () => {
  const open = !$("#nav").classList.contains("open");
  $("#nav").classList.toggle("open", open); $("#menuBtn").setAttribute("aria-expanded", String(open));
  $("#menuBtn").setAttribute("aria-label", open ? "Close menu" : "Open menu");
});

// ---------------- routing (real URLs, server renders the same pages) ----------------
function route(path, push = true) {
  const [view, title] = ROUTES[path] || ["notfound", "Page not found · Reading the Rig"];
  if (push && location.pathname !== path) history.pushState({}, "", path);
  document.documentElement.dataset.view = view;
  document.title = title;
  $$("#nav a").forEach((a) => (a.dataset.view === view ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  $("#nav").classList.remove("open"); $("#menuBtn").setAttribute("aria-expanded", "false");
  if (view === "evaluation") loadEval();
  if (view === "memory") loadMemory();
  if (push) { window.scrollTo(0, 0); $("#main").focus({ preventScroll: true }); }
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  e.preventDefault(); route(new URL(a.href).pathname);
});
window.addEventListener("popstate", () => route(location.pathname, false));

// ---------------- model ----------------
// Ollama runs on a laptop; Groq is a free hosted model (what the online demo uses); mock is keyword rules.
async function loadStatus() {
  try {
    const s = await getJSON("/api/status");
    const sel = $("#modelSelect"), opt = (v) => sel.querySelector(`option[value="${v}"]`);
    opt("ollama").disabled = !s.ollama_ok;
    opt("ollama").textContent = `Ollama · ${s.ollama_model}` + (s.ollama_ok ? "" : " (local only)");
    opt("groq").disabled = !s.groq_ok;
    opt("groq").textContent = `Groq · ${s.groq_model}` + (s.groq_ok ? "" : " (no key)");
    sel.value = s.backend;
    $("#modelDot").className = "dot " + (s.backend === "mock" ? "warn" : "ok");
    const hint = $("#modelHint");
    hint.className = "hint" + (s.backend === "mock" && !s.ollama_ok && !s.groq_ok ? " warn" : "");
    hint.textContent = s.backend === "ollama" ? `Lines are labelled by ${s.model}, running on this machine.`
      : s.backend === "groq" ? `Lines are labelled by ${s.model}, a free hosted model.`
      : s.ollama_ok || s.groq_ok ? "Mock keyword rules: instant, no model."
      : "No model on this server, so lines are labelled by keyword rules (mock).";
  } catch { $("#modelHint").textContent = "Model status unavailable."; }
}
$("#modelSelect").addEventListener("change", async (e) => {
  try {
    const s = await (await api("/api/model", { method: "POST", body: JSON.stringify({ name: e.target.value }) })).json();
    if (e.target.value !== s.backend) toast("That model is not available here. Using the mock.", true);
    else toast(`Model: ${s.backend === "mock" ? "mock keyword rules" : s.model}`);
  } catch (err) { toast(err.message, true); }
  loadStatus();
});

// ---------------- reports ----------------
async function loadReports() {
  try {
    S.reports = await getJSON("/api/reports");
    banner(null);
  } catch (e) { banner(e.message, loadReports); return; }
  const sel = $("#reportSelect");
  sel.innerHTML = S.reports.map((r) => `<option value="${esc(r.id)}">${esc(r.well)} · Day ${r.day} — ${esc(r.tag)}</option>`).join("")
    + `<option value="CUSTOM">Custom report — paste your own</option>`;
  sel.disabled = false; $("#runBtn").disabled = false;
  sel.value = "W-02_D14";
  selectReport();
}
function selectReport() {
  const id = $("#reportSelect").value;
  Object.assign(S, { current: id, thread: null, state: null, events: [] });
  const r = S.reports.find((x) => x.id === id);
  $("#reportTag").innerHTML = id === "CUSTOM" ? `<span class="pill">No answer key</span><span class="pill">Errors are left in for the Critic</span>`
    : r.planted_error ? `<span class="pill err">Planted error</span><span class="pill">The Critic should catch it</span>`
    : `<span class="pill ${r.tag === "clean day" ? "ok" : ""}">${esc(r.tag)}</span>`;
  const custom = id === "CUSTOM";
  $("#customBox").classList.toggle("hidden", !custom); $("#ddr").classList.toggle("hidden", custom);
  if (custom && !$("#customText").value) $("#customText").value = SAMPLE;
  validateCustom(false);
  setLive("", "Idle");
  renderAll();
}
$("#reportSelect").addEventListener("change", selectReport);
function validateCustom(show = true) {
  const t = $("#customText"), v = t.value, err = $("#customError");
  $("#customCount").textContent = v.length.toLocaleString();
  let msg = "";
  if (!v.trim()) msg = "Paste a report before running.";
  else if (v.length > MAX_CHARS) msg = `The report is too long (${v.length.toLocaleString()} of ${MAX_CHARS.toLocaleString()} characters).`;
  else if (!/\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}/.test(v)) msg = "No time-log lines found. Lines should start like 06:00-08:30.";
  const bad = Boolean(msg) && show;
  t.setAttribute("aria-invalid", String(bad));
  err.innerHTML = bad ? `${icon("alert")}${esc(msg)}` : ""; err.classList.toggle("hidden", !bad);
  return !msg;
}
$("#customText").addEventListener("input", () => validateCustom($("#customText").getAttribute("aria-invalid") === "true"));

// ---------------- running ----------------
$("#runBtn").addEventListener("click", async () => {
  if (S.busy) return;
  const body = { report_id: S.current, critic: $("#criticToggle").checked };
  if (S.current === "CUSTOM") {
    if (!validateCustom(true)) { $("#customText").focus(); return; }
    body.report_text = $("#customText").value;
  }
  Object.assign(S, { events: [], state: null });
  renderAll(true);
  await go("/api/runs", body, "Running the agents…");
});
async function go(path, body, label) {
  S.busy = true; setBusy(true); setLive("on", label || "Running");
  try { await stream(path, body); }
  catch (e) { toast(e.message, true); setLive("error", "Failed"); if (!S.state) renderAll(); }
  finally { S.busy = false; setBusy(false); renderPipeline(); }
}
function setBusy(on) {
  const b = $("#runBtn"); b.disabled = on; b.setAttribute("aria-busy", String(on));
  $(".run-label", b).textContent = on ? "Running…" : "Run agents";
  $("#reportSelect").disabled = on;
  $$("#review button").forEach((x) => (x.disabled = on));
}
function onEvent(ev, data) {
  if (ev === "start") S.thread = data.thread;
  if (ev === "step") { S.events.push(data); renderTrace(); renderPipeline(); }
  if (ev === "error") { toast(data.message, true); setLive("error", "Failed"); }
  if (ev === "done") {
    S.state = data;
    if (S.current === "CUSTOM") { $("#customBox").classList.add("hidden"); $("#ddr").classList.remove("hidden"); }
    const approved = data.engineer_decision === "approve" && !data.paused;
    setLive(data.paused ? "wait" : "done", data.paused ? "Waiting for you" : approved ? "Approved" : "Done");
    if (approved) toast("Report approved");
    renderAll();
  }
}
function setLive(cls, text) { const b = $("#liveBadge"); b.className = "live " + cls; b.textContent = text; }

// ---------------- rendering ----------------
function renderAll(loading = false) {
  renderKpis(loading); renderReport(); renderTrace(); renderPipeline(); renderCharts(loading); renderCritic(loading);
  renderInsight(loading); renderReview(); renderAnswerKey();
}
function kpi(id, value, sub, cls = "") {
  const el = $(id); $(".kpi-value", el).textContent = value;
  const s = $(".kpi-sub", el); s.textContent = sub || " "; s.className = "kpi-sub " + cls;
  el.removeAttribute("aria-busy");
}
function renderKpis(loading) {
  const v = S.state, ids = ["#kpiNpt", "#kpiHours", "#kpiDepth", "#kpiRop", "#kpiCritic"];
  if (!v) {
    ids.forEach((id) => { kpi(id, loading ? "…" : "—", id === "#kpiNpt" && !loading ? (S.current === "CUSTOM" ? "Run your report" : "Run a report") : ""); if (loading) $(id).setAttribute("aria-busy", "true"); });
    return;
  }
  const total = v.total_hours ?? 0, ok24 = Math.abs(total - 24) < 0.01;
  kpi("#kpiNpt", `${fmt(v.total_npt)} h`, `${fmt(((v.total_npt || 0) / 24) * 100, 0)}% of the day lost`);
  kpi("#kpiHours", `${fmt(total, 2)} h`, ok24 ? "Complete" : "Incomplete", ok24 ? "good" : "bad");
  const d = v.depth_vs_plan_m;
  kpi("#kpiDepth", d == null ? "n/a" : `${d > 0 ? "+" : ""}${fmt(d, 0)} m`, d == null ? "No plan for this day" : d > 0 ? "Ahead of plan" : d < 0 ? "Behind plan" : "On plan", d < 0 ? "bad" : d > 0 ? "good" : "");
  kpi("#kpiRop", v.rop?.rop_m_per_h ? `${fmt(v.rop.rop_m_per_h)}` : "—", v.rop?.drilled_m ? `m/h · ${fmt(v.rop.drilled_m, 0)} m drilled` : "No drilling today");
  const fails = (v.attempts || []).slice(0, -1).filter((a) => a.verdict === "FAIL").length;
  kpi("#kpiCritic", v.critic_verdict || (v.critic_on ? "—" : "Off"), v.critic_on ? `${fails} retr${fails === 1 ? "y" : "ies"} · ${(v.attempts || []).length} check(s)` : "Self-check skipped",
    v.critic_verdict === "PASS" ? "good" : v.critic_verdict === "FAIL" ? "bad" : "");
  $("#costBadge").textContent = v.cost_status && v.cost_status !== "unknown" ? `Day cost ${v.cost_status}` : "";
}
function renderReport() {
  if (!S.reports.length) return;
  const r = S.reports.find((x) => x.id === S.current);
  const text = S.state?.report_text ?? (S.current === "CUSTOM" ? $("#customText").value : r?.text ?? "");
  const acts = Object.fromEntries((S.state?.activities || []).map((a) => [a.source_line, a]));
  const bad = Object.fromEntries((S.state?.parse_issues || []).map((i) => [i.line, i]));
  const lines = text.replace(/\n+$/, "").split("\n");
  $("#ddr").innerHTML = lines.map((line, i) => {
    const n = i + 1, a = acts[n]; let style = "", chip = "", note = "";
    if (bad[n]) { style = "border-left-color:var(--bad);background:var(--bad-bg)"; chip = `<span class="chip" style="background:var(--bad);color:#fff">Source error · not used</span>`;
      note = `<span class="note bad">${esc(bad[n].detail)}</span>`; }
    else if (a) {
      if (a.category !== "productive") { const c = color(a.category); style = `border-left-color:${c};background:color-mix(in srgb, ${c} 10%, transparent)`;
        chip = `<span class="chip" style="background:${c}" title="category · hours · model confidence">${esc(pretty(a.category))} · ${fmt(a.hours, 2)} h · ${Number(a.confidence).toFixed(2)}</span>`; }
      if (a.note) note = `<span class="note">${esc(a.note)}</span>`;
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
    const cls = /FAIL|REJECTED/.test(s) ? "fail" : /^PASS|APPROVED/.test(s) ? "pass" : "";
    return `<li class="${minor ? "minor" : ""}"><span class="who">${ROLE[e.node] || esc(e.node)}</span><span class="${cls}">${esc(s)}</span></li>`;
  }).join("");
  ol.scrollTop = ol.scrollHeight;
}
function renderPipeline() {
  const seen = new Set(S.events.map((e) => e.node)), last = S.events[S.events.length - 1]?.node;
  const failedFinal = S.state?.critic_verdict === "FAIL";
  $("#pipeline").innerHTML = STAGES.map((n) => {
    let cls = seen.has(n) ? "done" : "", state = seen.has(n) ? "done" : "not started";
    if (n === last && S.busy) { cls = "active"; state = "working"; }
    if (n === "human_review" && S.state?.paused && !S.busy) { cls = "active"; state = "waiting for you"; }
    if (n === "critic" && failedFinal && !S.busy) { cls = "done fail"; state = "failed"; }
    return `<li class="stage ${cls}" aria-label="${ROLE[n]}: ${state}">${ROLE[n]}</li>`;
  }).join("");
}

// charts (hand-drawn SVG; tooltips on hover and keyboard focus)
const tip = $("#tooltip");
function bindTips(root) {
  root.querySelectorAll("[data-tip]").forEach((el) => {
    const show = (x, y) => { tip.style.display = "block"; tip.innerHTML = el.dataset.tip;
      tip.style.left = Math.min(x + 14, innerWidth - tip.offsetWidth - 8) + "px"; tip.style.top = y + 14 + "px"; };
    el.addEventListener("mousemove", (e) => show(e.clientX, e.clientY));
    el.addEventListener("focus", () => { const b = el.getBoundingClientRect(); show(b.left, b.bottom); });
    el.addEventListener("mouseleave", () => (tip.style.display = "none"));
    el.addEventListener("blur", () => (tip.style.display = "none"));
  });
}
function renderCharts(loading) {
  const acts = S.state?.activities || [];
  if (!acts.length) {
    $("#timeline").innerHTML = loading ? `<div class="skeleton" style="height:64px"></div>` : `<p class="empty-state">The 24-hour timeline appears after a run.</p>`;
    $("#bars").innerHTML = ""; return;
  }
  const W = 800, H = 70, x = (m) => (m / 1440) * W;
  let svg = `<svg viewBox="0 -4 ${W} ${H + 22}" role="img" aria-label="Timeline of the 24 hours by category">`;
  for (const a of acts) {
    const w = Math.max(1, x(a.end_min) - x(a.start_min) - 2);
    svg += `<rect x="${x(a.start_min) + 1}" y="0" width="${w}" height="${H - 14}" rx="4" fill="${color(a.category)}" tabindex="0"
      aria-label="${esc(a.start)} to ${esc(a.end)}, ${esc(pretty(a.category))}, ${fmt(a.hours, 2)} hours"
      data-tip="${esc(`<b>${esc(a.start)}–${esc(a.end)}</b> · ${fmt(a.hours, 2)} h<br>${esc(a.text)}<br>${esc(pretty(a.category))} · line ${a.source_line}`)}"></rect>`;   // escaped twice: the attribute decodes once
  }
  for (let h = 0; h <= 24; h += 3) svg += `<text x="${x(h * 60)}" y="${H + 12}" text-anchor="${h === 0 ? "start" : h === 24 ? "end" : "middle"}">${h}:00</text>`;
  $("#timeline").innerHTML = svg + "</svg>";
  bindTips($("#timeline"));
  const npt = Object.entries(S.state.npt_hours || {}).sort((a, b) => b[1] - a[1]);
  if (!npt.length) { $("#bars").innerHTML = `<p class="empty-state" style="color:var(--ok)">${icon("check")}Clean day: no non-productive time.</p>`; return; }
  const max = Math.max(...npt.map((d) => d[1])), rowH = 30, LW = 170, BW = W - LW - 60;
  let b = `<svg viewBox="0 0 ${W} ${npt.length * rowH}" role="img" aria-label="Non-productive hours by category">`;
  npt.forEach(([c, h], i) => {
    const y = i * rowH, w = Math.max(4, (h / max) * BW);
    b += `<text class="cat" x="${LW - 10}" y="${y + 19}" text-anchor="end">${esc(pretty(c))}</text>
      <rect x="${LW}" y="${y + 6}" width="${w}" height="18" rx="3" fill="${color(c)}"></rect>
      <text class="val" x="${LW + w + 8}" y="${y + 19}">${fmt(h, 2)} h</text>`;
  });
  $("#bars").innerHTML = b + "</svg>";
}
function renderCritic(loading) {
  const v = S.state, el = $("#critic");
  if (!v) { el.innerHTML = loading ? `<div class="skeleton skel-tile"></div>` : `<p class="empty-state">No checks yet. Run a report to see what the Critic finds.</p>`; return; }
  if (!v.critic_on) { el.innerHTML = `<p class="empty-state">${icon("alert")}The Critic is switched off for this run, so nothing was checked.</p>`; return; }
  el.innerHTML = (v.attempts || []).map((a, i) => `<div class="check ${a.verdict === "PASS" ? "pass" : "fail"}">
      <div class="check-head"><span>Check ${i + 1} · ${fmt(a.total_hours, 2)} h logged · ${fmt(a.total_npt, 2)} h NPT</span><span class="v">${icon(a.verdict === "PASS" ? "check" : "x")}${a.verdict}</span></div>
      ${a.problems.length ? `<ul>${a.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}</div>`).join("")
    + [...(v.critic_warnings || []), ...(v.reference_warnings || [])].map((w) => `<p class="note-line">${icon("alert")}<span>${esc(w)}</span></p>`).join("");
}
function md(text) {   // tiny, safe markdown for the insight report: escape first, then format
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/\*([^*]+)\*/g, "<em>$1</em>");
  let out = "", list = false;
  const close = () => { if (list) { out += "</ul>"; list = false; } };
  for (const line of String(text || "").split("\n")) {
    if (line.startsWith("### ")) { close(); out += `<h3>${inline(line.slice(4))}</h3>`; }
    else if (/^\*\*[^*]+\*\*/.test(line)) { close(); const m = /^\*\*([^*]+)\*\*(.*)$/.exec(line); out += `<h4>${esc(m[1])}<span> ${esc(m[2])}</span></h4>`; }
    else if (line.startsWith("- ")) { if (!list) { out += "<ul>"; list = true; } out += `<li>${inline(line.slice(2))}</li>`; }
    else if (line.startsWith("> ")) { close(); out += `<blockquote>${inline(line.slice(2))}</blockquote>`; }
    else if (line.trim()) { close(); out += `<p>${inline(line)}</p>`; }
    else close();
  }
  return out + (list ? "</ul>" : "");
}
function renderInsight(loading) {
  $("#insight").innerHTML = S.state?.draft_report ? md(S.state.draft_report)
    : loading ? `<div class="skeleton-lines"><i></i><i></i><i></i><i></i></div>` : `<p class="empty-state">The draft appears here when the agents finish.</p>`;
}
function renderReview() {
  const v = S.state, el = $("#review");
  if (!v) { el.innerHTML = `<p class="empty-state">Nothing to review yet.</p>`; return; }
  if (v.engineer_decision === "approve" && !v.paused) {
    el.innerHTML = `<div class="stamp" role="status"><b>${icon("check")}Approved</b>${v.engineer_comment ? esc(v.engineer_comment) + "<br>" : ""}Computed · Sourced · Approved</div>`; return;
  }
  if (!v.paused) { el.innerHTML = `<p class="empty-state">This run is not waiting for a review.</p>`; return; }
  el.innerHTML = `<p class="muted" style="margin-bottom:10px">The graph is paused at <code>interrupt()</code>. Nothing goes out until you decide.</p>
    <label class="field-label" for="comment">Comment (optional for approve)</label>
    <textarea id="comment" class="input" maxlength="1000" placeholder="e.g. line 7 is planned, not NPT" aria-describedby="commentHelp commentError"></textarea>
    <p class="field-help" id="commentHelp">Reject with “line N is &lt;category&gt;” (or “planned”) to correct a line on the re-run.</p>
    <p class="field-error hidden" id="commentError" role="alert"></p>
    <div class="review-actions"><button class="btn primary" id="approveBtn">${icon("check")}Approve</button><button class="btn" id="rejectBtn">${icon("edit")}Reject &amp; re-run</button></div>`;
  $("#approveBtn").onclick = () => go(`/api/runs/${encodeURIComponent(S.thread)}/review`, { decision: "approve", comment: $("#comment").value.trim() }, "Approving…");
  $("#rejectBtn").onclick = () => {
    const c = $("#comment"), err = $("#commentError");
    if (!c.value.trim()) {   // a rejection needs a reason the agents can act on
      c.setAttribute("aria-invalid", "true"); err.innerHTML = `${icon("alert")}Say what is wrong, for example “line 7 is planned, not NPT”.`;
      err.classList.remove("hidden"); c.focus(); return;
    }
    go(`/api/runs/${encodeURIComponent(S.thread)}/review`, { decision: "reject", comment: c.value.trim() }, "Re-running…");
  };
}
function renderAnswerKey() {
  const v = S.state, k = v?.answer_key, el = $("#answerKey");
  if (!v) { el.innerHTML = `<p class="empty-state">Synthetic reports have a known answer to compare with.</p>`; return; }
  if (!k) { el.innerHTML = `<p class="empty-state">Custom report: there is no answer key to compare with.</p>`; return; }
  const rows = [["Hours logged", 24, v.total_hours], ["Total NPT (h)", k.total_npt, v.total_npt], ["Depth vs plan (m)", k.depth_vs_plan_m, v.depth_vs_plan_m],
    ["ROP (m/h)", k.rop_m_per_h, v.rop?.rop_m_per_h],
    ...[...new Set([...Object.keys(k.npt_hours), ...Object.keys(v.npt_hours || {})])].map((c) => [`${pretty(c)} (h)`, k.npt_hours[c] || 0, v.npt_hours?.[c] || 0])];
  el.innerHTML = `<table><thead><tr><th scope="col">Check</th><th scope="col">Expected</th><th scope="col">Agents</th><th scope="col"><span class="sr-only">Match</span></th></tr></thead><tbody>${rows.map(([n, e, a]) => {
    const ok = a != null && Math.abs(Number(e ?? 0) - Number(a ?? 0)) < 0.06;
    return `<tr><td>${esc(n)}</td><td>${fmt(e, 2)}</td><td>${fmt(a, 2)}</td><td class="${ok ? "ok" : "bad"}">${icon(ok ? "check" : "x")}<span class="sr-only">${ok ? "matches" : "does not match"}</span></td></tr>`;
  }).join("")}</tbody></table>`;
}

// ---------------- evaluation ----------------
async function loadEval() {
  const g = $("#evalGrid");
  g.innerHTML = `<div class="tile span-4 skeleton skel-tile"></div><div class="tile span-4 skeleton skel-tile"></div><div class="tile span-4 skeleton skel-tile"></div><div class="tile span-12 skeleton" style="min-height:240px"></div>`;
  let m;
  try { m = await getJSON("/api/metrics"); banner(null); } catch (e) { g.innerHTML = ""; banner(e.message, loadEval); return; }
  const o = m.ollama?.runs;
  if (!o) { g.innerHTML = `<div class="tile span-12"><p class="empty-state">No evaluation results yet. Run <code>python evaluate.py</code> with Ollama.</p></div>`; return; }
  const G = o["Graph with Critic"], N = o["Graph without Critic"], C = o["Single-prompt chatbot"];
  const bars = [["Graph with Critic", G.npt_hours_mae, "var(--accent)"], ["Graph without Critic", N.npt_hours_mae, "color-mix(in srgb, var(--accent) 50%, var(--muted))"], ["Single-prompt chatbot", C.npt_hours_mae, "var(--muted)"]];
  const max = Math.max(...bars.map((b) => b[1])), W = 760;
  const svg = `<svg viewBox="0 0 ${W} ${bars.length * 46}" role="img" aria-label="Mean NPT hours error: graph with Critic ${G.npt_hours_mae.toFixed(2)}, without Critic ${N.npt_hours_mae.toFixed(2)}, chatbot ${C.npt_hours_mae.toFixed(2)} hours">${bars.map(([n, v, c], i) => {
    const w = Math.max(6, (v / max) * (W - 260));
    return `<text class="cat" x="180" y="${i * 46 + 26}" text-anchor="end">${n}</text><rect x="192" y="${i * 46 + 10}" width="${w}" height="24" rx="4" style="fill:${c}"></rect><text class="val" x="${200 + w}" y="${i * 46 + 27}">${v.toFixed(2)} h</text>`;
  }).join("")}</svg>`;
  const first = (s) => String(s ?? "").split(" / ")[0];
  const table = (runs) => `<table class="data"><thead><tr><th scope="col">Run</th><th scope="col">NPT error</th><th scope="col">Category acc.</th><th scope="col">Critic catch</th><th scope="col">Evidence</th><th scope="col">24 h logs</th><th scope="col">Exact NPT</th></tr></thead><tbody>${Object.entries(runs).map(([n, r]) =>
    r.npt_hours_mae == null ? `<tr><td>${esc(n)}</td><td colspan="6" class="muted">${esc(r.note || "Not run")}</td></tr>` :
    `<tr><td>${esc(n)}</td><td class="${n === "Graph with Critic" ? "hl" : ""}">${r.npt_hours_mae.toFixed(2)} h</td><td>${(r.category_accuracy * 100).toFixed(1)}%</td><td>${r.critic_catch_rate == null ? "n/a" : Math.round(r.critic_catch_rate * 2) + " / 2"}</td><td>${(r.evidence_rate * 100).toFixed(0)}%</td><td>${esc(first(r.logs_adding_to_24h))}</td><td>${esc(first(r.reports_exact_npt))}</td></tr>`).join("")}</tbody></table>`;
  g.innerHTML = `
    <section class="tile kpi accent span-4"><h2 class="tile-label">Our graph · NPT error per report</h2><div class="stat-big accent">${G.npt_hours_mae.toFixed(2)} h</div><div class="kpi-sub">${esc(m.ollama.meta.model)}, ${m.ollama.meta.reports} reports</div></section>
    <section class="tile kpi span-4"><h2 class="tile-label">Single-prompt chatbot · same model</h2><div class="stat-big">${C.npt_hours_mae.toFixed(2)} h</div><div class="kpi-sub">About ${Math.round(C.npt_hours_mae / G.npt_hours_mae)}× more error</div></section>
    <section class="tile kpi span-4"><h2 class="tile-label">Planted errors caught by the Critic</h2><div class="stat-big">${Math.round(G.critic_catch_rate * 2)} / 2</div><div class="kpi-sub">Before any human saw them</div></section>
    <section class="tile span-7"><h2 class="tile-label">Mean NPT hours error · lower is better</h2><div class="chart">${svg}</div></section>
    <section class="tile span-5"><h2 class="tile-label">How to read it</h2><p>“Without Critic” looks slightly better only because two mistakes cancel on W-02 Day 14. Only ${esc(N.logs_adding_to_24h)} of its logs add up to 24 h. The chatbot's logs add up to 24 h in ${esc(first(C.logs_adding_to_24h))}.</p></section>
    <section class="tile span-12"><h2 class="tile-label">${esc(m.ollama.meta.model)} · generated ${esc(m.ollama.meta.generated)}</h2><div class="scroll">${table(o)}</div></section>
    ${m.mock ? `<section class="tile span-12"><h2 class="tile-label">Mock keyword classifier (fallback) · optimistic, its keywords match the synthetic data</h2><div class="scroll">${table(m.mock.runs)}</div></section>` : ""}`;
}

// ---------------- memory ----------------
async function loadMemory() {
  const g = $("#memGrid");
  g.innerHTML = `<div class="tile span-4 skeleton skel-tile"></div><div class="tile span-4 skeleton skel-tile"></div><div class="tile span-4 skeleton skel-tile"></div><div class="tile span-12 skeleton" style="min-height:240px"></div>`;
  let m;
  try { m = await getJSON("/api/memory"); banner(null); } catch (e) { g.innerHTML = ""; banner(e.message, loadMemory); return; }
  const t = (rows, cols, empty) => rows.length ? `<table class="data"><thead><tr>${cols.map(([, h]) => `<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${cols.map(([c]) => `<td>${esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>` : `<p class="empty-state">${empty}</p>`;
  g.innerHTML = `
    <section class="tile kpi span-4"><h2 class="tile-label">Past incidents</h2><div class="stat-big">${m.incidents.length}</div><div class="kpi-sub">Cause and fix, read by Research</div></section>
    <section class="tile kpi span-4"><h2 class="tile-label">Failed cases</h2><div class="stat-big">${m.failed_cases.length}</div><div class="kpi-sub">Written by the Critic</div></section>
    <section class="tile kpi span-4"><h2 class="tile-label">Engineer decisions</h2><div class="stat-big">${m.engineer_feedback.length}</div><div class="kpi-sub">Approvals and rejections</div></section>
    <section class="tile span-12"><h2 class="tile-label">Past NPT incidents</h2><div class="scroll">${t(m.incidents, [["well_id", "Well"], ["day", "Day"], ["category", "Category"], ["cause", "Cause"], ["fix", "Fix"], ["hours_lost", "Hours lost"]], "No incidents.")}</div></section>
    <section class="tile span-7"><h2 class="tile-label">Failed cases from the Critic</h2><div class="scroll">${t(m.failed_cases, [["report_id", "Report"], ["attempt", "Check"], ["problems", "Problems"]], "None yet. Run a report with a planted error.")}</div></section>
    <section class="tile span-5"><h2 class="tile-label">Engineer decisions</h2><div class="scroll">${t(m.engineer_feedback, [["report_id", "Report"], ["decision", "Decision"], ["comment", "Comment"]], "None yet. Approve or reject a report.")}</div></section>`;
}
$("#resetMem").addEventListener("click", async () => {
  if (!(await confirmDialog("Reset memory?", "This deletes all failed cases and engineer decisions and restores the 9 seed incidents. Everyone using this server shares this memory.", "Reset"))) return;
  try { await api("/api/memory/reset", { method: "POST" }); toast("Memory reset to the seed data"); loadMemory(); }
  catch (e) { toast(e.message, true); }
});

// ---------------- start ----------------
applyThemeButton();
route(location.pathname, false);
loadStatus();
loadReports();
