"""Tools the agents call. Executor tools run through the LangGraph ToolNode; Research tools are
plain lookups on the reference files and the incident memory. Every number is computed here."""
import csv
import json
import re
from pathlib import Path

from langchain_core.tools import tool

import llm
import memory

DATA = Path(__file__).parent / "data"
REF_DIR = DATA                       # reference files (tests point this at a temp folder)
TIME_RE = re.compile(r"^\s*(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})\s+(.*\S)")
LOOSE_TIME_RE = re.compile(r"^\s*\d{1,2}[:.]?\d{0,2}\s*-")      # looks like a time range but is not HH:MM-HH:MM
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

def _well_plan() -> dict:
    with open(REF_DIR / "well_plan.csv") as f:
        return {(r["well_id"], int(r["day"])): int(r["planned_depth_m"]) for r in csv.DictReader(f)}


def _budget() -> dict:
    with open(REF_DIR / "cost_budget.csv") as f:
        return {r["well_id"]: int(r["daily_budget_usd"]) for r in csv.DictReader(f)}


@tool
def lookup_well_plan(well_id: str, day: int) -> str:
    """Planned end-of-day depth (m) and daily cost budget (USD) for a well and day.
    Missing or unreadable reference files come back as null plus a warning, never as a crash."""
    out, warnings = {"planned_depth_m": None, "daily_budget_usd": None}, []
    try:
        out["planned_depth_m"] = _well_plan().get((well_id, day))
        if out["planned_depth_m"] is None:
            warnings.append(f"well_plan.csv has no planned depth for {well_id} day {day}")
    except (OSError, KeyError, ValueError) as e:
        warnings.append(f"well_plan.csv could not be read ({type(e).__name__})")
    try:
        out["daily_budget_usd"] = _budget().get(well_id)
        if out["daily_budget_usd"] is None:
            warnings.append(f"cost_budget.csv has no budget for {well_id}")
    except (OSError, KeyError, ValueError) as e:
        warnings.append(f"cost_budget.csv could not be read ({type(e).__name__})")
    return json.dumps(out | {"warnings": warnings})


@tool
def get_npt_codes() -> str:
    """The six NPT categories with the keywords crews use for them. If npt_codes.json is missing or
    invalid, returns the six category names without keywords and a warning."""
    try:
        codes = json.loads((REF_DIR / "npt_codes.json").read_text(encoding="utf-8"))
        if not isinstance(codes, dict) or not codes:
            raise ValueError("not a non-empty object")
        return json.dumps({"codes": codes, "warnings": []})
    except (OSError, ValueError) as e:
        return json.dumps({"codes": {c: [] for c in llm.CATEGORIES},
                           "warnings": [f"npt_codes.json could not be used ({type(e).__name__}): "
                                        "categories kept, keywords missing"]})


def npt_codes() -> dict:
    return json.loads(get_npt_codes.invoke({}))["codes"]


@tool
def search_incidents(well_id: str, report_text: str) -> str:
    """Past NPT incidents from memory in the categories this report seems to mention."""
    codes = npt_codes()
    t = report_text.lower()
    suspected = [c for c, words in codes.items()
                 if any(re.search(rf"\b{re.escape(w)}\b", t) for w in words)]
    return json.dumps({"suspected": suspected,
                       "incidents": memory.search_incidents(well_id, suspected)})


# ---------------- Executor tools (ToolNode) ----------------

def _time_to_min(h: str, m: str) -> int | None:
    h, m = int(h), int(m)
    if m > 59 or h > 24 or (h == 24 and m != 0):
        return None
    return h * 60 + m


@tool
def parse_time_log(report_text: str, fix_hints: list[dict]) -> str:
    """Split the time log into activities (start, end, hours, text, source line).
    Lines that cannot be parsed are returned as issues, not dropped silently.
    Reads the REMARKS section only to fill a time gap the Critic reported, and trims
    overlapping lines the Critic reported (each change is noted on the activity)."""
    acts, remarks, issues, in_remarks = [], [], [], False
    for i, raw in enumerate(report_text.splitlines(), start=1):
        if i == 1 or not raw.strip():
            continue                                         # header line is parsed separately
        if raw.strip().upper().startswith("REMARKS"):
            in_remarks = True
        body = re.sub(r"^\s*REMARKS:\s*", "", raw, flags=re.I)
        m = TIME_RE.match(body)
        if not m:
            if not in_remarks:
                kind = "invalid_timestamp" if LOOSE_TIME_RE.match(body) else "malformed"
                issues.append({"line": i, "type": kind, "text": raw.strip(),
                               "detail": "time range is not HH:MM-HH:MM" if kind == "invalid_timestamp"
                               else "not a time-log entry"})
            continue
        h1, m1, h2, m2, text = m.groups()
        s, e = _time_to_min(h1, m1), _time_to_min(h2, m2)
        if s is None or e is None or e <= s:
            if not in_remarks:
                issues.append({"line": i, "type": "invalid_timestamp", "text": raw.strip(),
                               "detail": "impossible time" if s is None or e is None else "end is not after start"})
            continue
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
                    a["note"] = (f"start moved {hhmm(a['start_min'])} -> {hhmm(h['fix_start_min'])} to remove a "
                                 "double-counted overlap (confirm with the rig crew)")
                    a["start_min"] = h["fix_start_min"]

    acts.sort(key=lambda a: (a["start_min"], a["source_line"]))
    for a in acts:
        a["start"], a["end"] = hhmm(a["start_min"]), hhmm(a["end_min"])
        a["hours"] = round((a["end_min"] - a["start_min"]) / 60, 2)
    return json.dumps({"activities": acts, "issues": issues})


@tool
def classify_activities(activities: list[dict], overrides: dict) -> str:
    """Label every activity as productive or an NPT category with a confidence score.
    The LLM (or the mock) reads the text; engineer overrides (by source line) win."""
    codes = npt_codes()
    overrides = overrides or {}
    todo = [a for a in activities if str(a["source_line"]) not in overrides]
    labels = dict(zip((a["source_line"] for a in todo), llm.classify_many([a["text"] for a in todo], codes)))
    out = []
    for a in activities:
        o = overrides.get(str(a["source_line"]))
        cat, conf, by = (o, 1.0, "engineer") if o else labels[a["source_line"]]
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
def depth_vs_plan(actual_depth_m: int | None, planned_depth_m: int | None) -> str:
    """Actual end-of-day depth minus planned depth (negative = behind plan). Null if either is unknown."""
    if actual_depth_m is None or planned_depth_m is None:
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
