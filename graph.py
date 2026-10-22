"""Reading the Rig - LangGraph multi-agent graph for Daily Drilling Report (DDR) analysis.

Hub and spoke: the Orchestrator (plain Python route()) reads the shared DDRState and picks the
next agent. Planner -> Research -> Executor (tools via ToolNode) -> Critic (retry loop, max 3)
-> report_writer -> human_review (interrupt, engineer approves or rejects) -> END.
"""
import json
import operator
import re
import sqlite3
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import interrupt

import llm
import memory
from tools import (EXECUTOR_TOOLS, get_npt_codes, hhmm, lookup_well_plan, parse_header,
                   report_lines, search_incidents)

ROOT = Path(__file__).parent
NPT_CODES = llm.CATEGORIES
MAX_RETRIES = 3
RECURSION_LIMIT = 50
CONFIDENCE_FLOOR = 0.7


class DDRState(TypedDict, total=False):
    report_id: str
    well_id: str
    day: int
    report_text: str
    header: dict
    task_plan: list[str]
    well_plan: dict
    npt_codes: dict
    budget: dict
    similar_events: list[dict]
    suspected_categories: list[str]
    reference_warnings: list[str]   # missing or unreadable reference data
    activities: list[dict] | None   # start, end, hours, text, category, confidence, source_line
    parse_issues: list[dict]        # malformed lines / invalid timestamps found by the parser
    total_hours: float              # must be 24.0
    npt_hours: dict
    total_npt: float
    rop: dict
    depth_vs_plan_m: float | None
    cost_status: str
    critic_verdict: str | None      # "PASS" or "FAIL"
    critic_feedback: list[str]
    critic_warnings: list[str]
    critic_retryable: bool          # False when a retry cannot change the result (source-data errors)
    fix_hints: list[dict]           # structured feedback the Executor acts on
    retry_count: int                # max 3
    attempts: Annotated[list, operator.add]
    draft_report: str
    evidence_lines: list[int]
    overrides: dict                 # source_line -> category, from the engineer
    engineer_decision: str
    engineer_comment: str
    trace: Annotated[list, operator.add]
    messages: Annotated[list, add_messages]


def _t(node: str, summary: str) -> list[dict]:
    return [{"node": node, "summary": summary}]


# ---------------- nodes ----------------

def ingest_report(state: DDRState) -> dict:
    text = state.get("report_text") or (ROOT / "data" / "reports" / f"{state['report_id']}.txt").read_text()
    h = parse_header(text)
    n_log = sum(1 for line in text.splitlines() if re.match(r"\d{2}:\d{2}", line.strip()))
    return {"report_text": text, "header": h, "well_id": h.get("well_id", state.get("well_id")),
            "day": h.get("day", state.get("day")), "retry_count": 0, "critic_verdict": None,
            "fix_hints": [], "overrides": {},
            "trace": _t("ingest_report", f"Read {state['report_id']}: "
                                         f"{'header' if h else 'NO valid header'} + {n_log} time-log lines"
                                         f"{', REMARKS section present' if 'REMARKS' in text.upper() else ''}")}


def orchestrator(state: DDRState) -> dict:
    return {}   # pure Python: the decision lives in route()


def make_route(use_critic: bool = True):
    def route(state: DDRState) -> str:
        if not state.get("task_plan"):
            return "planner"
        if not state.get("well_plan") or "similar_events" not in state:
            return "research"
        if state.get("activities") is None:
            return "executor"
        if not use_critic:
            return "report_writer"
        if not state.get("critic_verdict"):
            return "critic"
        if (state["critic_verdict"] == "FAIL" and state.get("critic_retryable", True)
                and state.get("retry_count", 0) < MAX_RETRIES):
            return "executor"
        return "report_writer"
    return route


def planner(state: DDRState) -> dict:
    """Turns one report into a task list. Rule based: the plan adapts to what the report contains."""
    text, h = state["report_text"], state.get("header", {})
    lines = report_lines(text)
    n_log = sum(1 for v in lines.values() if re.match(r"\d{2}:\d{2}", v.strip()))
    has_drilling = bool(re.search(r"\bdrilled\b", text, re.I))
    tasks = [f"Parse the time log into timed activities ({n_log} lines)"]
    if "REMARKS" in text.upper():
        tasks.append("Hold REMARKS in reserve: read them only if the Critic finds a time gap")
    tasks += ["Classify every activity: productive or one of 6 NPT categories",
              "Total NPT hours by category and confirm the log adds to 24.0 h"]
    tasks.append("Compute ROP from the drilled-metres lines" if has_drilling
                 else "No drilling lines today: skip ROP")
    tasks.append(f"Compare end depth {h['depth_m']:,} m with the well plan" if h.get("depth_m")
                 else "Header has no depth: flag for the engineer")
    if h.get("day_cost_usd"):
        tasks.append("Compare day cost with the daily budget")
    tasks += ["Match NPT to past incidents and suggest a fix",
              "Draft the Daily Insight Report for engineer approval"]
    return {"task_plan": tasks, "trace": _t("planner", f"{len(tasks)} tasks planned")}


def research(state: DDRState) -> dict:
    """Reference lookups. Missing or invalid reference data becomes a warning, never a crash."""
    warnings = []
    well, day = state.get("well_id"), state.get("day")
    if well and day is not None:
        plan = json.loads(lookup_well_plan.invoke({"well_id": well, "day": day}))
        warnings += plan.pop("warnings")
    else:
        plan = {"planned_depth_m": None, "daily_budget_usd": None}
        warnings.append("Report header has no well/day, so the well plan and budget cannot be looked up")
    codes_out = json.loads(get_npt_codes.invoke({}))
    warnings += codes_out["warnings"]
    found = json.loads(search_incidents.invoke({"well_id": well or "", "report_text": state["report_text"]}))
    pd, bud = plan["planned_depth_m"], plan["daily_budget_usd"]
    return {"well_plan": {"planned_depth_m": pd}, "budget": {"daily_usd": bud},
            "npt_codes": codes_out["codes"], "similar_events": found["incidents"],
            "suspected_categories": found["suspected"], "reference_warnings": warnings,
            "trace": _t("research", f"Plan {f'{pd:,} m' if pd else 'n/a'}, budget "
                                    f"{f'USD {bud:,}' if bud else 'n/a'}; "
                                    f"{len(found['incidents'])} similar past incidents "
                                    f"({', '.join(found['suspected']) or 'no NPT keywords'})"
                                    + (f"; {len(warnings)} reference warning(s)" if warnings else ""))}


def executor(state: DDRState) -> dict:
    """Agent that works through tools. Visit 1: ask the ToolNode to parse. Visit 2: ask it to classify
    and run the maths tools in parallel. Visit 3: read the results and total NPT in Python."""
    msgs = state.get("messages", [])
    trailing = []
    for m in reversed(msgs):
        if not isinstance(m, ToolMessage):
            break
        trailing.insert(0, m)
    results = {m.name: json.loads(m.content) for m in trailing}
    attempt = state.get("retry_count", 0) + 1
    cid = f"a{attempt}_{len(msgs)}"

    if "classify_activities" in results:                       # visit 3: collect
        acts = results["classify_activities"]
        npt: dict = {}
        for a in acts:
            if a["category"] != "productive":
                npt[a["category"]] = round(npt.get(a["category"], 0) + a["hours"], 2)
        total = results["sum_hours"]["total_hours"]
        ev = [a["source_line"] for a in acts if a["category"] != "productive"]
        return {"activities": acts, "total_hours": total, "npt_hours": npt,
                "total_npt": round(sum(npt.values()), 2), "rop": results["calc_rop"],
                "depth_vs_plan_m": results["depth_vs_plan"]["depth_vs_plan_m"],
                "cost_status": results["cost_vs_budget"]["cost_status"], "evidence_lines": ev,
                "critic_verdict": None,
                "messages": [AIMessage(content=f"Executor done: {total} h logged, {sum(npt.values()):g} h NPT")],
                "trace": _t("executor", f"Attempt {attempt}: {len(acts)} activities, {total} h total, "
                                        f"NPT {', '.join(f'{k} {v:g} h' for k, v in npt.items()) or 'none'}")}

    if "parse_time_log" in results:                            # visit 2: classify + maths in parallel
        parsed = results["parse_time_log"]
        acts = parsed["activities"]
        h = state.get("header", {})
        calls = [
            {"name": "classify_activities", "args": {"activities": acts, "overrides": state.get("overrides", {})}},
            {"name": "sum_hours", "args": {"activities": acts}},
            {"name": "calc_rop", "args": {"activities": acts}},
            {"name": "depth_vs_plan", "args": {"actual_depth_m": h.get("depth_m"),
                                               "planned_depth_m": state["well_plan"].get("planned_depth_m")}},
            {"name": "cost_vs_budget", "args": {"day_cost_usd": h.get("day_cost_usd"),
                                                "daily_budget_usd": state["budget"].get("daily_usd")}},
        ]
        for i, c in enumerate(calls):
            c["id"] = f"{cid}_{i}"
        n_bad = len(parsed["issues"])
        bad = f" ({n_bad} unparseable line{'s' * (n_bad != 1)})" if n_bad else ""
        return {"parse_issues": parsed["issues"],
                "messages": [AIMessage(content=f"Parsed {len(acts)} activities{bad}, calling 5 tools",
                                       tool_calls=calls)]}

    hints = state.get("fix_hints", [])                         # visit 1: parse
    call = {"name": "parse_time_log", "id": f"{cid}_p",
            "args": {"report_text": state["report_text"], "fix_hints": hints}}
    return {"messages": [AIMessage(content=f"Attempt {attempt}: parsing with {len(hints)} fix hints", tool_calls=[call])]}


def critic(state: DDRState) -> dict:
    """Checks the Executor's work against hard rules. FAIL sends it back with exact feedback.
    Source-data errors (bad header, malformed lines, impossible times) cannot be fixed by a retry:
    they FAIL without a retry and go to the engineer, unrepaired."""
    acts = sorted(state.get("activities") or [], key=lambda a: (a["start_min"], a["source_line"]))
    lines = report_lines(state["report_text"])
    problems, warnings, hints = [], [], []

    if not state.get("header"):
        problems.append("Header line is missing or malformed (expected 'WELL <id> DAY <n> DEPTH <m> m ...')")
    for iss in state.get("parse_issues", []):
        problems.append(f"Line {iss['line']} cannot be used ({iss['detail']}): '{iss['text'][:50]}'")
    if not acts:
        problems.append("No valid time-log entries were found")

    total = state.get("total_hours", 0)
    if abs(total - 24.0) > 0.01:
        problems.append(f"Time log adds to {total:g} h, not 24")
    cursor = 0
    for a in acts:
        if a["start_min"] > cursor:
            problems.append(f"Gap {hhmm(cursor)}-{hhmm(a['start_min'])} is not accounted for in the time log")
            hints.append({"type": "gap", "start_min": cursor, "end_min": a["start_min"]})
        elif a["start_min"] < cursor:
            problems.append(f"Line {a['source_line']} starts {a['start']} but the previous activity ends "
                            f"{hhmm(cursor)} ({cursor - a['start_min']} min overlap)")
            hints.append({"type": "overlap", "line": a["source_line"], "fix_start_min": cursor})
        cursor = max(cursor, a["end_min"])
    if acts and cursor < 24 * 60:
        problems.append(f"Gap {hhmm(cursor)}-24:00 is not accounted for")
        hints.append({"type": "gap", "start_min": cursor, "end_min": 24 * 60})

    for a in acts:
        cat = a.get("category")
        if not cat:
            problems.append(f"Line {a['source_line']} has no category")
        elif cat not in NPT_CODES + ["productive"]:
            problems.append(f"Line {a['source_line']} has unknown category '{cat}'")
        if cat and cat != "productive" and a["text"] not in lines.get(a["source_line"], ""):
            problems.append(f"NPT claim '{a['text'][:40]}' does not match source line {a['source_line']}")
        if a.get("confidence", 0) < CONFIDENCE_FLOOR:
            warnings.append(f"Line {a['source_line']} labelled {cat} with low confidence "
                            f"{a.get('confidence', 0):.2f}: engineer should confirm")
    npt_sum = round(sum(a["hours"] for a in acts if a.get("category") not in (None, "productive")), 2)
    if abs(npt_sum - state.get("total_npt", 0)) > 0.01:
        problems.append(f"NPT total {state.get('total_npt', 0):g} h does not match the NPT lines ({npt_sum:g} h)")

    deepest, header_depth = (state.get("rop") or {}).get("deepest_drilled_m"), state.get("header", {}).get("depth_m")
    if deepest and header_depth and deepest != header_depth:
        problems.append(f"Log drills to {deepest:,} m but the header says {header_depth:,} m")

    attempt = state.get("retry_count", 0) + 1
    entry = {"attempt": attempt, "total_hours": total, "total_npt": state.get("total_npt", 0),
             "verdict": "FAIL" if problems else "PASS", "problems": problems, "warnings": warnings}
    if problems:
        memory.add_failed_case(state["report_id"], attempt, problems)
        known = {json.dumps(h, sort_keys=True) for h in state.get("fix_hints", [])}
        new = [h for h in hints if json.dumps(h, sort_keys=True) not in known]
        retryable = bool(new)          # retry only when there is something new for the Executor to try
        note = f"retry {attempt}/{MAX_RETRIES}" if retryable and attempt < MAX_RETRIES + 1 else "no retry can fix this"
        return {"critic_verdict": "FAIL", "critic_feedback": problems, "critic_warnings": warnings,
                "critic_retryable": retryable, "fix_hints": state.get("fix_hints", []) + new,
                "retry_count": attempt, "attempts": [entry],
                "trace": _t("critic", f"FAIL ({note}): " + "; ".join(problems))}
    return {"critic_verdict": "PASS", "critic_feedback": [], "critic_warnings": warnings,
            "critic_retryable": True, "attempts": [entry],
            "trace": _t("critic", "PASS: 24.0 h, no gaps or overlaps, valid categories, every NPT hour cites its line"
                                  + (f"; {len(warnings)} low-confidence flag(s)" if warnings else ""))}


def _numbers(text: str) -> set[str]:
    return {n.rstrip(".") for n in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))}


def _pretty(cat: str) -> str:
    return cat.replace("_", " ")


def report_writer(state: DDRState) -> dict:
    """Builds the Daily Insight Report from computed facts only. The LLM (if present) may rephrase, not count.
    Sections keep observed data, inferred causes, validation results and suggested actions apart."""
    h, npt = state.get("header", {}), state.get("npt_hours", {})
    acts = state.get("activities") or []
    top = max(npt, key=npt.get) if npt else None
    events = [e for e in state.get("similar_events", []) if e["category"] == top]
    lines = report_lines(state["report_text"])
    dvp = state.get("depth_vs_plan_m")
    rop = state.get("rop") or {}
    verdict = state.get("critic_verdict")
    well = state.get("well_id") or "unknown well"

    facts = [f"Well {well} day {state.get('day', '?')}"
             + (f", end depth {h['depth_m']:,} m." if h.get("depth_m") else ", end depth not reported."),
             f"The time log covers {state.get('total_hours', 0):g} h. "
             f"Non-productive time (NPT) total: {state.get('total_npt', 0):g} h"
             + (f" ({', '.join(f'{_pretty(k)} {v:g} h' for k, v in npt.items())})." if npt else ".")]
    if dvp is not None:
        facts.append(f"Depth vs plan: {dvp:+,} m ({'ahead of' if dvp > 0 else 'behind'} plan)." if dvp
                     else "Depth is on plan.")
    if rop.get("rop_m_per_h"):
        facts.append(f"ROP: {rop['rop_m_per_h']} m/h over {rop['drilling_hours']:g} h of drilling.")
    facts.append(f"Cost: {state.get('cost_status', 'unknown')}.")
    if events:
        facts.append(f"Similar past event: {events[0]['cause']} on {events[0]['well_id']} day {events[0]['day']}; "
                     f"fix used: {events[0]['fix']}.")
    summary = llm.write_summary("\n".join(facts))
    summary_by = llm.model_name()
    if not summary or not _numbers(summary) <= _numbers(" ".join(facts)):
        summary, summary_by = " ".join(facts), "template"   # guard: the LLM may not add a number Python did not compute

    labellers = sorted({a.get("labelled_by", "?") for a in acts})
    md = [f"### Daily Insight Report · {state['report_id']}", summary, "",
          "**Observed: NPT by category** (from the time log)"]
    md += [f"- {_pretty(k)}: **{v:g} h**" for k, v in npt.items()] or ["- No non-productive time found"]
    md += ["", "**Likely cause** (inferred from incident memory, not observed)"]
    if top and events:
        e = events[0]
        md.append(f"- {_pretty(top)} is the largest loss. Similar to past incident on {e['well_id']} day {e['day']}: "
                  f"*{e['cause']}*.")
    elif top:
        md.append(f"- {_pretty(top)} is the largest loss. No similar incident in memory yet.")
    else:
        md.append("- Clean day, nothing to explain.")
    md += ["", "**Suggested action** (for the engineer to decide)",
           f"- {events[0]['fix']} (worked on {events[0]['well_id']} day {events[0]['day']})." if top and events else
           "- Log this event in incident memory so the next well can learn from it." if top else
           "- None. Keep the current programme."]
    md += ["", "**Evidence (source lines)**"]
    md += [f"- L{n}: `{lines.get(n, '').strip()}`" for n in state.get("evidence_lines", [])] or ["- n/a"]

    md += ["", "**Validation**"]
    if verdict == "PASS":
        md.append(f"- Critic PASS after {len(state.get('attempts', []))} check(s)")
    elif verdict == "FAIL":
        md.append("- ⚠ Critic FAIL, not resolved: " + "; ".join(state.get("critic_feedback", [])))
    else:
        md.append("- ⚠ Not checked: the Critic was switched off for this run")
    changed = [a for a in acts if a.get("note")]
    md += [f"- Correction made by the agents, confirm with the rig crew: L{a['source_line']} {a['note']}"
           for a in changed]
    md += [f"- Reference data: {w}" for w in state.get("reference_warnings", [])]
    md += [f"- {w}" for w in state.get("critic_warnings", [])]
    md.append(f"- Lines labelled by: {', '.join(labellers) or 'n/a'} · summary written by: {summary_by}")
    return {"draft_report": "\n".join(md),
            "trace": _t("report_writer", f"Draft ready (summary: {summary_by}, "
                                         f"{len(state.get('evidence_lines', []))} evidence lines)")}


ALIASES = {"productive": "productive", "planned": "productive", "not npt": "productive",
           "stuck": "stuck_pipe", "losses": "lost_circulation", "lost circulation": "lost_circulation",
           "tool": "tool_failure", "weather": "waiting_on_weather", "wow": "waiting_on_weather",
           "equipment": "waiting_on_equipment", "woe": "waiting_on_equipment",
           "kick": "well_control", "well control": "well_control", **{c: c for c in NPT_CODES}}


def parse_overrides(comment: str) -> dict:
    """'line 7 is planned, not NPT' -> {'7': 'productive'}"""
    out = {}
    parts = re.split(r"(?=\bline\s*\d+)", comment.lower())
    for p in parts:
        m = re.match(r"line\s*(\d+)(.*)", p, re.S)
        if not m:
            continue
        rest = m[2]
        hits = sorted(((rest.find(k), v) for k, v in ALIASES.items() if k in rest), key=lambda x: x[0])
        if hits:
            out[m[1]] = hits[0][1]
    return out


def human_review(state: DDRState) -> dict:
    answer = interrupt({"draft": state["draft_report"], "report_id": state["report_id"]})   # graph pauses here
    decision, comment = answer.get("decision", "approve"), answer.get("comment", "")
    memory.add_feedback(state["report_id"], decision, comment)
    if decision == "approve":
        return {"engineer_decision": "approve", "engineer_comment": comment,
                "trace": _t("human_review", "Engineer APPROVED" + (f": {comment}" if comment else ""))}
    overrides = {**state.get("overrides", {}), **parse_overrides(comment)}
    # reject: keep the comment, clear the work so route() sends it back to the Executor
    return {"engineer_decision": "reject", "engineer_comment": comment, "overrides": overrides,
            "activities": None, "critic_verdict": None, "retry_count": 0,
            "trace": _t("human_review", f"Engineer REJECTED: {comment or '(no comment)'}"
                                        + (f" → overrides {overrides}" if overrides else ""))}


def decide(state: DDRState) -> str:
    return state["engineer_decision"]


# ---------------- graph ----------------

def build(checkpointer=None, use_critic: bool = True):
    g = StateGraph(DDRState)
    for name, fn in [("ingest_report", ingest_report), ("orchestrator", orchestrator), ("planner", planner),
                     ("research", research), ("executor", executor), ("critic", critic),
                     ("report_writer", report_writer), ("human_review", human_review)]:
        g.add_node(name, fn)
    g.add_node("tools", ToolNode(EXECUTOR_TOOLS))
    g.add_edge(START, "ingest_report")
    g.add_edge("ingest_report", "orchestrator")
    for a in ["planner", "research", "critic"]:
        g.add_edge(a, "orchestrator")
    g.add_conditional_edges("orchestrator", make_route(use_critic),
                            ["planner", "research", "executor", "critic", "report_writer"])
    g.add_conditional_edges("executor", tools_condition, {"tools": "tools", END: "orchestrator"})
    g.add_edge("tools", "executor")
    g.add_edge("report_writer", "human_review")
    g.add_conditional_edges("human_review", decide, {"approve": END, "reject": "orchestrator"})
    if checkpointer is None:
        from langgraph.checkpoint.sqlite import SqliteSaver
        (ROOT / "runtime").mkdir(exist_ok=True)
        checkpointer = SqliteSaver(sqlite3.connect(ROOT / "runtime" / "checkpoints.db", check_same_thread=False))
    return g.compile(checkpointer=checkpointer)


def run_until_review(app, report_id: str, thread_id: str, report_text: str | None = None) -> dict:
    """Runs one report up to the engineer pause and returns the state there (used by evaluate.py and tests)."""
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    init = {"report_id": report_id} | ({"report_text": report_text} if report_text else {})
    for _ in app.stream(init, cfg, stream_mode="updates"):
        pass
    return app.get_state(cfg).values


if __name__ == "__main__":
    from langgraph.types import Command
    app = build(MemorySaver())
    cfg = {"configurable": {"thread_id": "cli-W-02_D14"}, "recursion_limit": RECURSION_LIMIT}
    for chunk in app.stream({"report_id": "W-02_D14"}, cfg, stream_mode="updates"):
        for node, upd in chunk.items():
            for t in (upd or {}).get("trace", []) if isinstance(upd, dict) else []:
                print(f"{t['node']:>14} | {t['summary']}")
            if node in ("tools", "orchestrator") or node == "__interrupt__":
                print(f"{node:>14} |")
    print("paused at:", app.get_state(cfg).next)
    app.invoke(Command(resume={"decision": "approve", "comment": "ok"}), cfg)
    v = app.get_state(cfg).values
    print("NPT:", v["npt_hours"], "total", v["total_hours"], "retries", v["retry_count"], "->", v["engineer_decision"])
