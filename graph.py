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
    activities: list[dict]          # start, end, hours, text, category, confidence, source_line
    total_hours: float              # must be 24.0
    npt_hours: dict
    total_npt: float
    rop: dict
    depth_vs_plan_m: float | None
    cost_status: str
    critic_verdict: str | None      # "PASS" or "FAIL"
    critic_feedback: list[str]
    critic_warnings: list[str]
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
            "trace": _t("ingest_report", f"Read {state['report_id']}: header + {n_log} time-log lines"
                                         f"{', REMARKS section present' if 'REMARKS' in text.upper() else ''}")}


def orchestrator(state: DDRState) -> dict:
    return {}   # pure Python: the decision lives in route()


def make_route(use_critic: bool = True):
    def route(state: DDRState) -> str:
        if not state.get("task_plan"):
            return "planner"
        if not state.get("well_plan") or "similar_events" not in state:
            return "research"
        if not state.get("activities"):
            return "executor"
        if not use_critic:
            return "report_writer"
        if not state.get("critic_verdict"):
            return "critic"
        if state["critic_verdict"] == "FAIL" and state.get("retry_count", 0) < MAX_RETRIES:
            return "executor"
        return "report_writer"
    return route


def planner(state: DDRState) -> dict:
    """Turns one report into a task list. The plan adapts to what the report contains."""
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
    tasks.append(f"Compare end depth {h.get('depth_m', '?'):,} m with the well plan" if h.get("depth_m")
                 else "Header has no depth: flag for the engineer")
    if h.get("day_cost_usd"):
        tasks.append("Compare day cost with the daily budget")
    tasks += ["Match NPT to past incidents and suggest a fix",
              "Draft the Daily Insight Report for engineer approval"]
    return {"task_plan": tasks, "trace": _t("planner", f"{len(tasks)} tasks planned")}


def research(state: DDRState) -> dict:
    plan = json.loads(lookup_well_plan.invoke({"well_id": state["well_id"], "day": state["day"]}))
    codes = json.loads(get_npt_codes.invoke({}))
    found = json.loads(search_incidents.invoke({"well_id": state["well_id"], "report_text": state["report_text"]}))
    pd = plan["planned_depth_m"]
    return {"well_plan": {"planned_depth_m": pd}, "budget": {"daily_usd": plan["daily_budget_usd"]},
            "npt_codes": codes, "similar_events": found["incidents"],
            "suspected_categories": found["suspected"],
            "trace": _t("research", f"Plan {pd:,} m, budget USD {plan['daily_budget_usd']:,}; "
                                    f"{len(found['incidents'])} similar past incidents "
                                    f"({', '.join(found['suspected']) or 'no NPT keywords'})")}


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
        acts = results["parse_time_log"]
        h = state.get("header", {})
        calls = [
            {"name": "classify_activities", "args": {"activities": acts, "overrides": state.get("overrides", {})}},
            {"name": "sum_hours", "args": {"activities": acts}},
            {"name": "calc_rop", "args": {"activities": acts}},
            {"name": "depth_vs_plan", "args": {"actual_depth_m": h.get("depth_m") or 0,
                                               "planned_depth_m": state["well_plan"].get("planned_depth_m")}},
            {"name": "cost_vs_budget", "args": {"day_cost_usd": h.get("day_cost_usd"),
                                                "daily_budget_usd": state["budget"].get("daily_usd")}},
        ]
        for i, c in enumerate(calls):
            c["id"] = f"{cid}_{i}"
        return {"messages": [AIMessage(content=f"Parsed {len(acts)} activities, calling 5 tools", tool_calls=calls)]}

    hints = state.get("fix_hints", [])                         # visit 1: parse
    call = {"name": "parse_time_log", "id": f"{cid}_p",
            "args": {"report_text": state["report_text"], "fix_hints": hints}}
    return {"messages": [AIMessage(content=f"Attempt {attempt}: parsing with {len(hints)} fix hints", tool_calls=[call])]}


def critic(state: DDRState) -> dict:
    """Checks the Executor's work against hard rules. FAIL sends it back with exact feedback."""
    acts = sorted(state["activities"], key=lambda a: (a["start_min"], a["source_line"]))
    lines = report_lines(state["report_text"])
    problems, warnings, hints = [], [], []

    total = state["total_hours"]
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
    if cursor < 24 * 60:
        problems.append(f"Gap {hhmm(cursor)}-24:00 is not accounted for")
        hints.append({"type": "gap", "start_min": cursor, "end_min": 24 * 60})

    for a in acts:
        if a["category"] not in NPT_CODES + ["productive"]:
            problems.append(f"Line {a['source_line']} has unknown category '{a['category']}'")
        if a["category"] != "productive" and a["text"] not in lines.get(a["source_line"], ""):
            problems.append(f"NPT claim '{a['text'][:40]}' does not match source line {a['source_line']}")
        if a["confidence"] < CONFIDENCE_FLOOR:
            warnings.append(f"Line {a['source_line']} labelled {a['category']} with low confidence "
                            f"{a['confidence']:.2f}: engineer should confirm")

    deepest, header_depth = state["rop"].get("deepest_drilled_m"), state.get("header", {}).get("depth_m")
    if deepest and header_depth and deepest != header_depth:
        problems.append(f"Log drills to {deepest:,} m but the header says {header_depth:,} m")

    attempt = state.get("retry_count", 0) + 1
    entry = {"attempt": attempt, "total_hours": total, "total_npt": state.get("total_npt", 0),
             "verdict": "FAIL" if problems else "PASS", "problems": problems, "warnings": warnings}
    if problems:
        memory.add_failed_case(state["report_id"], attempt, problems)
        known = {json.dumps(h, sort_keys=True) for h in state.get("fix_hints", [])}
        merged = state.get("fix_hints", []) + [h for h in hints if json.dumps(h, sort_keys=True) not in known]
        return {"critic_verdict": "FAIL", "critic_feedback": problems, "critic_warnings": warnings,
                "fix_hints": merged, "retry_count": attempt, "attempts": [entry],
                "trace": _t("critic", f"FAIL (retry {attempt}/{MAX_RETRIES}): " + "; ".join(problems))}
    return {"critic_verdict": "PASS", "critic_feedback": [], "critic_warnings": warnings, "attempts": [entry],
            "trace": _t("critic", "PASS: 24.0 h, valid categories, every NPT hour cites its line"
                                  + (f"; {len(warnings)} low-confidence flag(s)" if warnings else ""))}


def _numbers(text: str) -> set[str]:
    return {n.rstrip(".") for n in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))}


def _pretty(cat: str) -> str:
    return cat.replace("_", " ")


def report_writer(state: DDRState) -> dict:
    """Builds the Daily Insight Report from computed facts only. The LLM (if present) may rephrase, not count."""
    h, npt = state.get("header", {}), state.get("npt_hours", {})
    top = max(npt, key=npt.get) if npt else None
    events = [e for e in state.get("similar_events", []) if e["category"] == top]
    lines = report_lines(state["report_text"])
    dvp = state.get("depth_vs_plan_m")
    rop = state.get("rop", {})
    unresolved = state.get("critic_verdict") == "FAIL"

    facts = [f"Well {state['well_id']} day {state['day']}, end depth {h.get('depth_m', 0):,} m.",
             f"The time log covers {state['total_hours']:g} h. "
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
    if not summary or not _numbers(summary) <= _numbers(" ".join(facts)):
        summary = " ".join(facts)      # guard: the LLM may not introduce a number Python did not compute

    md = [f"### Daily Insight Report · {state['report_id']}", summary, "",
          "**NPT by category**"]
    md += [f"- {_pretty(k)}: **{v:g} h**" for k, v in npt.items()] or ["- No non-productive time today"]
    md += ["", "**Likely root cause**"]
    if top and events:
        e = events[0]
        md.append(f"- {_pretty(top)} is the largest loss. Matches past incident on {e['well_id']} day {e['day']}: "
                  f"*{e['cause']}*.")
    elif top:
        md.append(f"- {_pretty(top)} is the largest loss. No similar incident in memory yet.")
    else:
        md.append("- Clean day, nothing to explain.")
    md += ["", "**Suggested action**",
           f"- {events[0]['fix']}." if top and events else
           "- Log this event in incident memory so the next well can learn from it." if top else
           "- None. Keep the current programme."]
    md += ["", "**Evidence (source lines)**"]
    md += [f"- L{n}: `{lines.get(n, '').strip()}`" for n in state.get("evidence_lines", [])] or ["- n/a"]
    if unresolved:
        md += ["", f"> ⚠ The Critic could not resolve: {'; '.join(state.get('critic_feedback', []))}"]
    if state.get("critic_warnings"):
        md += ["", "**Needs your eye**"] + [f"- {w}" for w in state["critic_warnings"]]
    return {"draft_report": "\n".join(md),
            "trace": _t("report_writer", f"Draft ready ({'LLM summary' if llm.backend() == 'ollama' else 'template'}, "
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
            "activities": [], "critic_verdict": None, "retry_count": 0,
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
        checkpointer = SqliteSaver(sqlite3.connect(ROOT / "checkpoints.db", check_same_thread=False))
    return g.compile(checkpointer=checkpointer)


def run_until_review(app, report_id: str, thread_id: str) -> dict:
    """Runs one report up to the engineer pause and returns the state there (used by evaluate.py)."""
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    for _ in app.stream({"report_id": report_id}, cfg, stream_mode="updates"):
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
