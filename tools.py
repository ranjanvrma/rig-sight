"""Tools the agents call. Executor tools run through the LangGraph ToolNode; Research tools are
plain lookups on the reference files and the incident memory. Every number is computed here."""
import csv
import json
import re
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool

import llm
import memory

DATA = Path(__file__).parent / "data"
TIME_RE = re.compile(r"(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})\s+(.*)")
HEADER_RE = re.compile(r"WELL\s+(\S+)\s+DAY\s+(\d+)\s+DEPTH\s+([\d,]+)\s*m(?:\s+COST\s+USD\s+([\d,]+))?", re.I)
DRILL_RE = re.compile(r"drilled.*?from\s+([\d,]+)\s*m\s+to\s+([\d,]+)\s*m", re.I)


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_header(report_text: str) -> dict:
    m = HEADER_RE.search(report_text.splitlines()[0] if report_text else "")
    if not m:
        return {}
    return {"well_id": m[1], "day": int(m[2]), "depth_m": _num(m[3]),
            "day_cost_usd": _num(m[4]) if m[4] else None}


def report_lines(report_text: str) -> dict[int, str]:
    return {i: line for i, line in enumerate(report_text.splitlines(), start=1)}


# ---------------- reference data (Research) ----------------

@lru_cache
def _well_plan() -> dict:
    with open(DATA / "well_plan.csv") as f:
        return {(r["well_id"], int(r["day"])): int(r["planned_depth_m"]) for r in csv.DictReader(f)}


@lru_cache
def _budget() -> dict:
    with open(DATA / "cost_budget.csv") as f:
        return {r["well_id"]: int(r["daily_budget_usd"]) for r in csv.DictReader(f)}


@tool
def lookup_well_plan(well_id: str, day: int) -> str:
    """Planned end-of-day depth (m) and daily cost budget (USD) for a well and day."""
    return json.dumps({"planned_depth_m": _well_plan().get((well_id, day)),
                       "daily_budget_usd": _budget().get(well_id)})


@tool
def get_npt_codes() -> str:
    """The six NPT categories with the keywords crews use for them."""
    return (DATA / "npt_codes.json").read_text(encoding="utf-8")


@tool
def search_incidents(well_id: str, report_text: str) -> str:
    """Past NPT incidents from memory in the categories this report seems to mention."""
    codes = json.loads(get_npt_codes.invoke({}))
    t = report_text.lower()
    suspected = [c for c, words in codes.items()
                 if any(re.search(rf"\b{re.escape(w)}\b", t) for w in words)]
    return json.dumps({"suspected": suspected,
                       "incidents": memory.search_incidents(well_id, suspected)})


# ---------------- Executor tools (ToolNode) ----------------

@tool
def parse_time_log(report_text: str, fix_hints: list[dict]) -> str:
    """Split the time log into activities (start, end, hours, text, source line).
    Reads the REMARKS section only to fill a time gap the Critic reported, and trims
    overlapping lines the Critic reported."""
    acts, remarks, in_remarks = [], [], False
    for i, raw in enumerate(report_text.splitlines(), start=1):
        if raw.strip().upper().startswith("REMARKS"):
            in_remarks = True
        m = TIME_RE.search(raw)
        if not m:
            continue
        h1, m1, h2, m2, text = m.groups()
        s, e = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
        a = {"start_min": s, "end_min": e, "text": text.strip(), "source_line": i,
             "section": "remarks" if in_remarks else "log", "note": ""}
        (remarks if in_remarks else acts).append(a)

    for h in fix_hints or []:
        if h.get("type") == "gap":
            for r in remarks:
                if r["start_min"] >= h["start_min"] and r["end_min"] <= h["end_min"] and r not in acts:
                    r["note"] = f"recovered from REMARKS to fill the {hhmm(h['start_min'])}-{hhmm(h['end_min'])} gap"
                    acts.append(r)
        elif h.get("type") == "overlap":
            for a in acts:
                if a["source_line"] == h["line"] and a["start_min"] < h["fix_start_min"]:
                    a["note"] = f"start moved {hhmm(a['start_min'])} -> {hhmm(h['fix_start_min'])} to remove overlap"
                    a["start_min"] = h["fix_start_min"]

    acts.sort(key=lambda a: (a["start_min"], a["source_line"]))
    for a in acts:
        a["start"], a["end"] = hhmm(a["start_min"]), hhmm(a["end_min"])
        a["hours"] = round((a["end_min"] - a["start_min"]) / 60, 2)
    return json.dumps(acts)


@tool
def classify_activities(activities: list[dict], overrides: dict) -> str:
    """Label every activity as productive or an NPT category with a confidence score.
    The LLM reads the text; engineer overrides (by source line) win."""
    codes = json.loads(get_npt_codes.invoke({}))
    out = []
    for a in activities:
        o = (overrides or {}).get(str(a["source_line"]))
        if o:
            cat, conf, by = o, 1.0, "engineer"
        else:
            cat, conf, by = llm.classify(a["text"], codes)
        out.append({**a, "category": cat, "confidence": round(conf, 2), "labelled_by": by})
    return json.dumps(out)


@tool
def sum_hours(activities: list[dict]) -> str:
    """Total hours in the time log. A complete DDR must add up to 24.0."""
    return json.dumps({"total_hours": round(sum(a["hours"] for a in activities), 2)})


@tool
def calc_rop(activities: list[dict]) -> str:
    """Rate of penetration = metres drilled / drilling hours, from the 'Drilled ... from X m to Y m' lines."""
    metres, hours, deepest = 0, 0.0, None
    for a in activities:
        m = DRILL_RE.search(a["text"])
        if m:
            a0, b0 = _num(m[1]), _num(m[2])
            metres += b0 - a0
            hours += a["hours"]
            deepest = max(deepest or 0, b0)
    return json.dumps({"drilled_m": metres, "drilling_hours": hours,
                       "rop_m_per_h": round(metres / hours, 1) if hours else None,
                       "deepest_drilled_m": deepest})


@tool
def depth_vs_plan(actual_depth_m: int, planned_depth_m: int | None) -> str:
    """Actual end-of-day depth minus planned depth (negative = behind plan)."""
    if planned_depth_m is None:
        return json.dumps({"depth_vs_plan_m": None})
    return json.dumps({"depth_vs_plan_m": actual_depth_m - planned_depth_m})


@tool
def cost_vs_budget(day_cost_usd: int | None, daily_budget_usd: int | None) -> str:
    """Day cost against the daily budget."""
    if not day_cost_usd or not daily_budget_usd:
        return json.dumps({"cost_delta_usd": None, "cost_status": "unknown"})
    d = day_cost_usd - daily_budget_usd
    pct = d / daily_budget_usd * 100
    status = f"{'over' if d > 0 else 'under'} budget by USD {abs(d):,} ({abs(pct):.1f}%)" if d else "on budget"
    return json.dumps({"cost_delta_usd": d, "cost_status": status})


EXECUTOR_TOOLS = [parse_time_log, classify_activities, sum_hours, calc_rop, depth_vs_plan, cost_vs_budget]
RESEARCH_TOOLS = [lookup_well_plan, get_npt_codes, search_incidents]
