"""Reading the Rig - Streamlit PoC. Run: streamlit run app.py"""
import warnings

warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

import html  # noqa: E402
import json  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from langgraph.types import Command  # noqa: E402

import graph  # noqa: E402
import llm  # noqa: E402
import memory  # noqa: E402
from tools import report_lines  # noqa: E402

ROOT = Path(__file__).parent
DATA = ROOT / "data"
KEYS = json.loads((DATA / "answer_key.json").read_text())
CUSTOM = "CUSTOM"
SAMPLE_CUSTOM = """WELL W-02  DAY 15  DEPTH 2,700 m  COST USD 431,000
00:00-05:00 Drilled 12-1/4 in hole from 2,610 m to 2,700 m
05:00-07:30 Mud motor failure, POOH to change motor
07:30-12:00 RIH with new motor
12:00-2:00 Circulated and conditioned mud
14:00-24:00 Waited on weather, swell above 4 m
"""

st.set_page_config(page_title="Reading the Rig", page_icon="🛢️", layout="wide")

# categorical slots validated for the dark surface (dataviz reference palette, dark steps)
CAT_COLORS = {
    "productive": "#4a4f5a",
    "stuck_pipe": "#3987e5",
    "lost_circulation": "#d95926",
    "tool_failure": "#199e70",
    "waiting_on_weather": "#c98500",
    "waiting_on_equipment": "#d55181",
    "well_control": "#008300",
}
# node colour key from the architecture diagram
ROLE = {"ingest_report": ("#6b7280", "Ingest"), "orchestrator": ("#16a34a", "Orchestrator"),
        "planner": ("#3b82f6", "Planner"), "research": ("#3b82f6", "Research"),
        "executor": ("#3b82f6", "Executor"), "tools": ("#8b5cf6", "ToolNode"),
        "critic": ("#3b82f6", "Critic"), "report_writer": ("#6b7280", "Report writer"),
        "human_review": ("#eab308", "Engineer")}

st.markdown("""
<style>
.block-container {padding-top: 2rem; max-width: 1400px;}
h1, h2, h3 {letter-spacing: -0.01em;}
.hero {padding: 1.1rem 1.3rem; border: 1px solid #262a33; border-radius: 14px;
       background: linear-gradient(135deg, #181b22 0%, #13161c 100%); margin-bottom: 1rem;}
.hero .kicker {color: #F2A93B; font-size: .75rem; letter-spacing: .12em; text-transform: uppercase; font-weight: 600;}
.hero .title {font-size: 1.6rem; font-weight: 700; margin: .15rem 0 .25rem;}
.hero .sub {color: #a3a3a3; font-size: .95rem;}
.tag {display:inline-block; padding: 2px 9px; border-radius: 999px; font-size: .72rem; font-weight: 600;
      border: 1px solid #333844; color: #cfcfcf; margin-right: 6px; background:#1d2129;}
.tag.err {border-color:#7f1d1d; color:#fca5a5; background:#2a1416;}
.tag.ok {border-color:#14532d; color:#86efac; background:#11221a;}
.ddr {font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .82rem; border: 1px solid #262a33;
      border-radius: 12px; overflow: hidden; background: #12151b;}
.ddr .row {display:flex; gap:.6rem; padding: .38rem .7rem; border-left: 3px solid transparent; align-items: baseline;}
.ddr .row + .row {border-top: 1px solid #1c2028;}
.ddr .ln {color:#5b6170; min-width: 1.6rem; text-align:right; user-select:none;}
.ddr .tx {flex: 1; color:#d9d7d2; white-space: pre-wrap;}
.ddr .hdr .tx {color:#F2A93B; font-weight:600;}
.ddr .chip {font-family: Inter, sans-serif; font-size:.68rem; padding:1px 7px; border-radius:999px; color:#0f1115;
            font-weight:700; white-space:nowrap;}
.ddr .note {display:block; font-family: Inter, sans-serif; font-size:.7rem; color:#F2A93B; margin-top:2px;}
.trace {display:flex; flex-direction:column; gap:4px;}
.trace .ev {display:flex; gap:.6rem; align-items:flex-start; padding:.35rem .55rem; border-radius:8px; background:#151820;}
.trace .ev.minor {background:transparent; padding:.1rem .55rem; opacity:.75;}
.trace .pill {font-size:.68rem; font-weight:700; padding:2px 8px; border-radius:6px; color:#fff; white-space:nowrap; min-width:92px; text-align:center;}
.trace .sum {font-size:.82rem; color:#d4d4d4;}
.trace .fail {color:#fca5a5;} .trace .pass {color:#86efac;}
.legend {display:flex; flex-wrap:wrap; gap:10px; font-size:.75rem; color:#bdbdbd; margin:.2rem 0 .4rem;}
.legend span {display:inline-flex; align-items:center; gap:5px;}
.legend i {width:10px; height:10px; border-radius:3px; display:inline-block;}
.stamp {border:1px solid #14532d; background:#0f1f17; border-radius:12px; padding:.9rem 1rem; color:#bbf7d0;}
.stamp b {color:#86efac;}
div[data-testid="stMetric"] {background:#151820; border:1px solid #262a33; border-radius:12px; padding:.7rem .9rem;}
</style>
""", unsafe_allow_html=True)


# ---------------- helpers ----------------

@st.cache_resource
def get_app(use_critic: bool):
    return graph.build(use_critic=use_critic)


def ss_init():
    for k, v in {"thread": None, "events": [], "report": None, "critic": True}.items():
        st.session_state.setdefault(k, v)


def cfg():
    return {"configurable": {"thread_id": st.session_state.thread}, "recursion_limit": graph.RECURSION_LIMIT}


def tag_for(rid: str) -> str:
    if rid == CUSTOM:
        return "paste your own text"
    k = KEYS[rid]
    if k["critic_should_fail_first_pass"]:
        return "planted error"
    return "clean day" if not k["npt_hours"] else ", ".join(c.replace("_", " ") for c in k["npt_hours"])


def event_from(node: str, upd) -> dict:
    if node == "__interrupt__":
        return {"node": "human_review", "summary": "Paused, waiting for the engineer's decision", "kind": "pause"}
    upd = upd or {}
    if node == "tools":
        names = [m.name for m in upd.get("messages", [])]
        return {"node": "tools", "summary": "Ran " + ", ".join(f"`{n}`" for n in names)}
    if upd.get("trace"):
        return {"node": node, "summary": upd["trace"][-1]["summary"]}
    if upd.get("messages"):
        return {"node": node, "summary": upd["messages"][-1].content}
    return {"node": node, "summary": ""}


def render_event(ev: dict, nxt: str | None = None) -> str:
    color, label = ROLE.get(ev["node"], ("#6b7280", ev["node"]))
    s = ev["summary"]
    if ev["node"] == "orchestrator":
        s = f"route() → {ROLE.get(nxt, ('', nxt or '…'))[1]}"
    cls = "fail" if s.startswith("FAIL") or "REJECTED" in s else "pass" if s.startswith("PASS") or "APPROVED" in s else ""
    s = html.escape(s).replace("`", "")
    minor = " minor" if ev["node"] in ("orchestrator", "tools") else ""
    return (f'<div class="ev{minor}"><span class="pill" style="background:{color}">{label}</span>'
            f'<span class="sum {cls}">{s}</span></div>')


def render_trace(events: list[dict]) -> str:
    rows = [render_event(e, events[i + 1]["node"] if i + 1 < len(events) else None) for i, e in enumerate(events)]
    return '<div class="trace">' + "".join(rows) + "</div>"


def stream(inp, title: str):
    app = get_app(st.session_state.critic)
    with st.status(title, expanded=True) as box:
        live = st.empty()
        for chunk in app.stream(inp, cfg(), stream_mode="updates"):
            for node, upd in chunk.items():
                st.session_state.events.append(event_from(node, upd))
                live.markdown(render_trace(st.session_state.events[-12:]), unsafe_allow_html=True)
        box.update(label="Run complete", state="complete", expanded=False)
    st.rerun()


def annotated_report(text: str, acts: list[dict], issues: list[dict] | None = None) -> str:
    by_line = {a["source_line"]: a for a in acts}
    bad = {i["line"]: i for i in issues or []}
    rows = []
    for n, line in report_lines(text).items():
        a = by_line.get(n)
        style, chip, note = "", "", ""
        cls = "row hdr" if n == 1 else "row"
        if n in bad:
            style = ' style="border-left-color:#ef4444; background:#ef444420"'
            chip = '<span class="chip" style="background:#ef4444">not used · source error</span>'
            note = f'<span class="note" style="color:#fca5a5">↳ {html.escape(bad[n]["detail"])}</span>'
        elif a:
            c = CAT_COLORS.get(a["category"], "#888")
            if a["category"] != "productive":
                style = f' style="border-left-color:{c}; background:{c}14"'
                chip = (f'<span class="chip" style="background:{c}">{a["category"].replace("_", " ")} · '
                        f'{a["hours"]:g} h · {a["confidence"]:.2f}</span>')
            if a.get("note"):
                note = f'<span class="note">↳ {html.escape(a["note"])}</span>'
        rows.append(f'<div class="{cls}"{style}><span class="ln">{n}</span>'
                    f'<span class="tx">{html.escape(line)}{note}</span>{chip}</div>')
    return '<div class="ddr">' + "".join(rows) + "</div>"


def legend(cats) -> str:
    return '<div class="legend">' + "".join(
        f'<span><i style="background:{CAT_COLORS[c]}"></i>{c.replace("_", " ")}</span>' for c in cats) + "</div>"


def timeline_chart(acts: list[dict]):
    df = pd.DataFrame([{"start": a["start_min"] / 60, "end": a["end_min"] / 60, "category": a["category"],
                        "activity": a["text"], "hours": a["hours"], "line": a["source_line"],
                        "time": f"{a['start']}-{a['end']}"} for a in acts])
    cats = list(CAT_COLORS)
    return alt.Chart(df).mark_bar(height=34, cornerRadius=4, stroke="#0f1115", strokeWidth=2).encode(
        x=alt.X("start:Q", scale=alt.Scale(domain=[0, 24]), title=None,
                axis=alt.Axis(values=list(range(0, 25, 3)), labelExpr="datum.value + ':00'", grid=False)),
        x2="end:Q",
        color=alt.Color("category:N", scale=alt.Scale(domain=cats, range=[CAT_COLORS[c] for c in cats]), legend=None),
        tooltip=["time", "activity", "category", "hours", "line"],
    ).properties(height=70, background="transparent").configure_view(stroke=None).configure_axis(
        labelColor="#9ca3af", domainColor="#333844", tickColor="#333844")


def npt_bar(npt: dict):
    df = pd.DataFrame([{"category": k, "label": k.replace("_", " "), "hours": v} for k, v in npt.items()])
    base = alt.Chart(df).encode(y=alt.Y("label:N", sort="-x", title=None),
                                x=alt.X("hours:Q", title="NPT hours", axis=alt.Axis(grid=True, gridColor="#1f232b")))
    bars = base.mark_bar(cornerRadiusEnd=4, height=16).encode(
        color=alt.Color("category:N", scale=alt.Scale(domain=list(CAT_COLORS), range=list(CAT_COLORS.values())),
                        legend=None), tooltip=["label", "hours"])
    text = base.mark_text(align="left", dx=6, color="#e8e6e1").encode(text=alt.Text("hours:Q", format=".1f"))
    return (bars + text).properties(height=alt.Step(34), background="transparent").configure_view(
        stroke=None).configure_axis(labelColor="#9ca3af", titleColor="#9ca3af", domainColor="#333844",
                                    labelLimit=220, labelFontSize=12)


# ---------------- sidebar ----------------

ss_init()
with st.sidebar:
    st.markdown("### 🛢️ Reading the Rig")
    st.caption("Agentic AI for Daily Drilling Reports · CA-3")
    rid = st.selectbox("Daily Drilling Report", list(KEYS) + [CUSTOM],
                       format_func=lambda r: ("✍ Custom report" if r == CUSTOM else r.replace('_D', ' · Day '))
                       + f"  —  {tag_for(r)}")
    ok, status = llm.ollama_status()
    choice = st.radio("Language model", ["Mock (keyword rules)", f"Ollama · {llm.MODEL}"],
                      index=1 if llm.backend() == "ollama" else 0, help="Free local model through Ollama. "
                      "The mock keeps the demo running when no model is available.")
    llm.set_backend("ollama" if choice.startswith("Ollama") else "mock")
    if choice.startswith("Ollama") and not ok:
        st.warning(status + ". Using the mock.")
    else:
        st.caption(f"Active: **{'Ollama · ' + llm.MODEL if llm.backend() == 'ollama' else 'mock keyword classifier'}**")
    critic_on = st.toggle("Critic agent", value=True, help="Turn off to see what slips through without self-checking.")
    run = st.button("▶  Run agents", type="primary", width="stretch")
    st.divider()
    st.caption("Demo order: W-01 Day 10 (clean) → W-02 Day 10 (tool failure) → W-02 Day 14 (Critic catches "
               "23.5 h; with Ollama, reject with “line 7 is planned, not NPT”)")

if run:
    st.session_state.update(thread=f"{rid}-{uuid.uuid4().hex[:6]}", events=[], report=rid, critic=critic_on)
    init = {"report_id": rid}
    if rid == CUSTOM:
        init["report_text"] = st.session_state.get("custom_text", SAMPLE_CUSTOM)
    stream(init, f"Agents working on {rid}…")

# ---------------- main ----------------

st.markdown("""<div class="hero"><div class="kicker">Oil &amp; gas · drilling operations</div>
<div class="title">Find the lost hours in every daily drilling report</div>
<div class="sub">A LangGraph team of agents plans, reads, computes, checks itself and waits for the drilling engineer to sign off.
<b>Computed</b> by Python · <b>Sourced</b> to a report line · <b>Approved</b> by a person.</div></div>""",
            unsafe_allow_html=True)

tab_run, tab_eval, tab_mem, tab_arch = st.tabs(["Analyse report", "Evaluation", "Memory", "Architecture"])

with tab_run:
    active = st.session_state.report == rid and st.session_state.thread
    snap = get_app(st.session_state.critic).get_state(cfg()) if active else None
    v = snap.values if snap else {}
    k = KEYS.get(rid)
    if rid == CUSTOM:
        text = v.get("report_text") or st.session_state.get("custom_text", SAMPLE_CUSTOM)
    else:
        text = v.get("report_text") or (DATA / "reports" / f"{rid}.txt").read_text()

    tags = f'<span class="tag">{"Custom report" if rid == CUSTOM else rid.replace("_D", " · Day ")}</span>'
    tags += (f'<span class="tag err">planted error</span>' if k and k["critic_should_fail_first_pass"] else "")
    if v.get("critic_verdict") == "FAIL":
        tags += '<span class="tag err">critic fail, unresolved</span>'
    if rid == CUSTOM and active and "total_hours" in v:
        if st.button("✎ Edit the report text"):
            st.session_state.thread = None
            st.rerun()

    if v.get("engineer_decision") == "approve":
        tags += '<span class="tag ok">approved</span>'
    if active and not st.session_state.critic:
        tags += '<span class="tag err">critic off</span>'
    st.markdown(tags, unsafe_allow_html=True)

    if "total_hours" not in v:
        c1, c2 = st.columns([1.15, 1])
        with c1:
            st.subheader("Daily Drilling Report")
            if rid == CUSTOM:
                st.caption("Paste any report in the same format. Errors are left in place for the Critic to find.")
                st.text_area("Report text", value=st.session_state.get("custom_text", SAMPLE_CUSTOM),
                             height=260, key="custom_text", label_visibility="collapsed")
            else:
                st.markdown(annotated_report(text, []), unsafe_allow_html=True)
        with c2:
            st.subheader("How a run works")
            st.markdown("""
1. **Planner** turns the report into a task list
2. **Research** pulls the well plan, budget, NPT codes and similar past incidents
3. **Executor** calls Python tools through a ToolNode: parse, classify, sum, ROP, depth, cost
4. **Critic** checks 24.0 h, gaps, overlaps, categories and evidence, and sends work back (max 3 retries)
5. **Report writer** drafts the Daily Insight Report from computed facts only
6. **Engineer** approves or rejects with a comment, inside the graph
""")
            st.info("Pick a report in the sidebar and press **Run agents**.")
    else:
        npt, total = v.get("npt_hours", {}), v.get("total_hours", 0)
        rop, dvp = v.get("rop", {}), v.get("depth_vs_plan_m")
        attempts = v.get("attempts", [])
        m = st.columns(6)
        m[0].metric("NPT", f"{v.get('total_npt', 0):g} h",
                    f"{v.get('total_npt', 0) / 24 * 100:.0f}% of the day", delta_color="off")
        m[1].metric("Hours logged", f"{total:g} h", "complete" if abs(total - 24) < .01 else "incomplete",
                    delta_color="normal" if abs(total - 24) < .01 else "inverse")
        m[2].metric("Depth vs plan", (f"{dvp:+,} m" if dvp else "0 m") if dvp is not None else "n/a",
                    None if dvp is None else "ahead" if dvp > 0 else "behind" if dvp < 0 else "on plan",
                    delta_color="normal" if (dvp or 0) >= 0 else "inverse")
        m[3].metric("ROP (m/h)", f"{rop.get('rop_m_per_h') or 0:g}", f"{rop.get('drilled_m', 0):,} m drilled",
                    delta_color="off")
        cost, budget = v.get("header", {}).get("day_cost_usd"), v.get("budget", {}).get("daily_usd")
        m[4].metric("Day cost", f"${cost / 1000:,.0f}k" if cost else "n/a",
                    f"{(cost - budget) / budget * 100:+.1f}% budget" if cost and budget else None,
                    delta_color="inverse")
        verdict = v.get("critic_verdict") or ("skipped" if not st.session_state.critic else "-")
        fails = sum(a["verdict"] == "FAIL" for a in attempts[:-1])   # FAILs that led to another attempt
        m[5].metric("Critic", verdict, f"{fails} retr{'y' if fails == 1 else 'ies'}",
                    delta_color="off")

        c1, c2 = st.columns([1.15, 1])
        with c1:
            st.subheader("Daily Drilling Report")
            st.caption("NPT lines are highlighted with category · hours · model confidence")
            st.markdown(annotated_report(text, v.get("activities") or [], v.get("parse_issues")),
                        unsafe_allow_html=True)
            st.subheader("Where the 24 hours went")
            st.markdown(legend(["productive"] + [c for c in CAT_COLORS if c in npt]), unsafe_allow_html=True)
            if v.get("activities"):
                st.altair_chart(timeline_chart(v["activities"]), width="stretch")
            if npt:
                st.altair_chart(npt_bar(npt), width="stretch")
            else:
                st.success("Clean day: no non-productive time found.")
        with c2:
            st.subheader("Agent trace")
            with st.container(height=430):
                st.markdown(render_trace(st.session_state.events), unsafe_allow_html=True)
            with st.expander("Planner's task list"):
                for i, t in enumerate(v.get("task_plan", []), 1):
                    st.markdown(f"{i}. {t}")
            if attempts:
                with st.expander(f"Critic attempts ({len(attempts)})", expanded=len(attempts) > 1):
                    for i, a in enumerate(attempts, 1):
                        icon = "❌" if a["verdict"] == "FAIL" else "✅"
                        st.markdown(f"{icon} **Check {i}** · {a['total_hours']:g} h logged · "
                                    f"{a['total_npt']:g} h NPT")
                        for p in a["problems"]:
                            st.markdown(f"  - {p}")
            if v.get("similar_events"):
                with st.expander("Similar past incidents (memory)"):
                    st.dataframe(pd.DataFrame(v["similar_events"]).drop(columns=["same_well"]),
                                 hide_index=True, width="stretch")

        st.divider()
        r1, r2 = st.columns([1.15, 1])
        with r1:
            with st.container(border=True):
                st.markdown(v.get("draft_report", ""))
        with r2:
            st.subheader("Engineer review")
            paused = snap and snap.next == ("human_review",)
            if paused:
                st.caption("The graph is paused at `interrupt()`. Nothing goes out until you decide.")
                comment = st.text_area("Comment", placeholder="e.g. line 7 is planned, not NPT",
                                       key=f"c-{st.session_state.thread}-{len(st.session_state.events)}")
                a, r = st.columns(2)
                if a.button("✅ Approve", type="primary", width="stretch"):
                    stream(Command(resume={"decision": "approve", "comment": comment}), "Resuming graph…")
                if r.button("↩ Reject and re-run", width="stretch"):
                    stream(Command(resume={"decision": "reject", "comment": comment}),
                           "Sending back to the Executor…")
                st.caption("Rejecting with *line N is &lt;category&gt;* (or *planned / productive*) "
                           "overrides that line on the re-run.")
            elif v.get("engineer_decision") == "approve":
                st.markdown(f'<div class="stamp"><b>Approved</b> by the drilling engineer'
                            f'{": " + html.escape(v.get("engineer_comment", "")) if v.get("engineer_comment") else ""}'
                            f'<br>Computed · Sourced · Approved</div>', unsafe_allow_html=True)
            if k is None:
                st.caption("Custom report: there is no answer key to compare against.")
            else:
                with st.expander("Check against the answer key (synthetic data)"):
                    rows = [("Hours logged", 24.0, total),
                            ("Total NPT (h)", k["total_npt"], v.get("total_npt", 0)),
                            ("Depth vs plan (m)", k["depth_vs_plan_m"], dvp),
                            ("ROP (m/h)", k["rop_m_per_h"], rop.get("rop_m_per_h"))]
                    rows += [(f"{c.replace('_', ' ')} (h)", k["npt_hours"].get(c, 0), npt.get(c, 0))
                             for c in sorted(set(k["npt_hours"]) | set(npt))]
                    st.dataframe(pd.DataFrame([{"check": a, "expected": b, "agents": c,
                                                "match": "✅" if c is not None and abs(float(b or 0) - float(c or 0)) < .06
                                                else "❌"} for a, b, c in rows]),
                                 hide_index=True, width="stretch")

with tab_eval:
    st.subheader("Evaluation on all 15 reports")
    st.caption("Every report is synthetic with planted events, so each has a known right answer. "
               "Results are saved per model, so the table always says which model produced it.")
    if st.button(f"Re-run evaluation with the active model ({llm.backend()})"):
        with st.spinner("Running 15 reports × each configuration…"):
            import evaluate
            evaluate.main()
    names = {"npt_hours_mae": "NPT hours error (MAE, h) ↓", "category_accuracy": "Category accuracy ↑",
             "critic_catch_rate": "Critic catch rate ↑", "evidence_rate": "Evidence rate ↑",
             "logs_adding_to_24h": "Logs adding to 24 h", "reports_exact_npt": "Reports with exact NPT",
             "false_npt_lines": "False NPT lines ↓"}
    found = False
    for backend, title in [("ollama", "Local LLM through Ollama"), ("mock", "Mock keyword classifier (fallback)")]:
        mpath = ROOT / "results" / f"metrics_{backend}.json"
        if not mpath.exists():
            continue
        found = True
        res = json.loads(mpath.read_text())
        st.markdown(f"#### {title}")
        st.caption(f"Model: **{res['meta']['model']}** · generated {res['meta']['generated']}")
        rows = [{"Run": run_name, **{lab: ("n/a" if mm.get(key) is None else mm.get(key)) for key, lab in names.items()}}
                for run_name, mm in res["runs"].items()]
        st.dataframe(pd.DataFrame(rows).astype(str), hide_index=True, width="stretch")
        if "note" in res["runs"].get("Single-prompt chatbot", {}):
            st.caption("Single-prompt chatbot baseline: " + res["runs"]["Single-prompt chatbot"]["note"])
        pr = ROOT / "results" / f"per_report_{backend}.csv"
        if pr.exists():
            with st.expander("Per-report results"):
                st.dataframe(pd.read_csv(pr), hide_index=True, width="stretch")
    if found:
        st.markdown("""
- **NPT hours error**: mean of |predicted NPT − true NPT| per report
- **Category accuracy**: planted NPT lines given the right category ÷ all planted NPT lines
- **Critic catch rate**: reports with a planted error that the Critic failed on the first pass ÷ 2
- **Evidence rate**: NPT claims whose cited source line really contains that text ÷ all NPT claims
- The mock's keywords share a vocabulary with the synthetic reports, so its accuracy is optimistic
""")
    else:
        st.info("No results yet. Press **Re-run evaluation**.")

with tab_mem:
    st.subheader("Long-term memory (SQLite)")
    st.caption("Research reads past incidents. The Critic writes failed cases. The engineer's decisions are kept. "
               "The app works on a copy in runtime/, so the committed seed data never changes.")
    if st.button("Reset memory to the seed data"):
        memory.reset()
        st.rerun()
    for name, title in [("incidents", "Past NPT incidents (cause → fix)"), ("failed_cases", "Failed cases from the Critic"),
                        ("engineer_feedback", "Engineer decisions")]:
        st.markdown(f"**{title}**")
        rows = memory.table(name)
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("Empty")

with tab_arch:
    st.subheader("LangGraph architecture")
    st.caption("Hub and spoke: every agent hands back to the Orchestrator, which reads the shared DDRState and routes.")
    st.graphviz_chart("""
digraph G {
  bgcolor="transparent"; rankdir=TB; nodesep=0.45; ranksep=0.5;
  node [shape=box style="rounded,filled" fontname="Helvetica" fontsize=11 fontcolor="white" color="#00000000"];
  edge [color="#9ca3af" fontname="Helvetica" fontsize=9 fontcolor="#9ca3af"];
  START [shape=circle label="START" fillcolor="#374151" width=0.6];
  ingest [label="ingest_report" fillcolor="#4b5563"];
  orch [label="Orchestrator\\nroute(state)" shape=hexagon fillcolor="#16a34a"];
  planner [label="Planner" fillcolor="#2563eb"];
  research [label="Research" fillcolor="#2563eb"];
  executor [label="Executor" fillcolor="#2563eb"];
  tools [label="ToolNode\\nparse · classify · sum\\nROP · depth · cost" fillcolor="#7c3aed"];
  critic [label="Critic" fillcolor="#2563eb"];
  writer [label="report_writer" fillcolor="#4b5563"];
  human [label="human_review\\ninterrupt()" fillcolor="#ca8a04"];
  END [shape=circle label="END" fillcolor="#374151" width=0.6];
  mem [label="Incident memory\\n(SQLite)" shape=cylinder fillcolor="#b91c1c"];
  ref [label="Reference data\\nwell plan · NPT codes · budget" shape=cylinder fillcolor="#7c3aed"];
  llm [label="Local LLM (Ollama)\\nor mock" style="rounded,dashed" fontcolor="#d1d5db" color="#9ca3af"];
  START -> ingest -> orch;
  orch -> planner [style=dashed label="no plan"]; planner -> orch;
  orch -> research [style=dashed label="need context"]; research -> orch;
  orch -> executor [style=dashed label="context ready"];
  executor -> tools [style=dotted color="#a78bfa" dir=both label="tool calls"];
  executor -> orch [style=dashed];
  orch -> critic [style=dashed color="#60a5fa" label="check"]; critic -> orch [label="verdict"];
  orch -> executor [style=dashed color="#ef4444" fontcolor="#ef4444" label="FAIL, fixable, retries < 3"];
  orch -> writer [style=dashed penwidth=2 color="#3b82f6" label="PASS / retries used"];
  writer -> human; human -> END [label="approve"];
  human -> orch [style=dashed color="#ef4444" fontcolor="#ef4444" label="reject + comment"];
  research -> ref [style=dotted color="#a78bfa" dir=both]; research -> mem [style=dotted color="#a78bfa"];
  critic -> mem [style=dashed color="#ef4444" label="failed case"];
  tools -> llm [style=dotted color="#9ca3af" label="classify"];
}
""", width="stretch")
