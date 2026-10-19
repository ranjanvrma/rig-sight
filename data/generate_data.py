"""Builds the synthetic dataset: 15 DDRs (3 wells x 5 days), the answer key,
reference files and the incident memory.

Every line is written here together with its true label, so the answer key is
independent of the agents' parser and classifier. Run: python data/generate_data.py
"""
import csv
import json
import sqlite3
from pathlib import Path

DATA = Path(__file__).parent
REPORTS = DATA / "reports"

P = "productive"
SP, LC, TF = "stuck_pipe", "lost_circulation", "tool_failure"
WOW, WOE, WC = "waiting_on_weather", "waiting_on_equipment", "well_control"

NPT_CODES = {
    SP: ["stuck pipe", "pipe stuck", "differential sticking", "jarred", "jarring",
         "worked pipe free", "unable to rotate"],
    LC: ["losses", "lost returns", "no returns", "lcm", "loss zone"],
    TF: ["mwd", "failed", "failure", "stopped sending", "no signal"],
    WOW: ["waited on weather", "wow", "wind", "swell", "weather"],
    WOE: ["waited on", "awaiting", "repair", "awaiting parts", "tong"],
    WC: ["kick", "shut in", "influx", "pit gain"],
}


def drill(size, a, b):
    return f"Drilled {size} in hole from {a:,} m to {b:,} m"


# Each report: list of (hours, text, true_category). Times are assigned in order from 00:00.
# Special keys: "remarks" = lines moved out of the time log into REMARKS (planted gap),
#               "overlap" = (line_index, minutes) start of that line written earlier (planted overlap).
SPEC = {
    "W-01": {"size": "8-1/2", "days": {
        10: {"cost": 352_000, "lines": [
            (6.0, drill("8-1/2", 3120, 3270), P),
            (0.5, "Circulated bottoms up, flow check, well static", P),
            (5.5, drill("8-1/2", 3270, 3400), P),
            (1.0, "Serviced top drive and rig", P),
            (11.0, drill("8-1/2", 3400, 3640), P)]},
        11: {"cost": 401_500, "lines": [
            (4.0, drill("8-1/2", 3640, 3730), P),
            (1.5, "Mud motor failure, lost differential pressure, decided to trip", TF),
            (6.0, "POOH for failed mud motor", TF),
            (2.0, "Changed out failed mud motor and function tested on surface", TF),
            (6.5, "RIH with new BHA to bottom after motor failure", TF),
            (4.0, drill("8-1/2", 3730, 3820), P)]},
        12: {"cost": 389_000, "lines": [
            (5.0, drill("8-1/2", 3820, 3930), P),
            (3.5, "Pipe stuck while making connection, differential sticking suspected", SP),
            (4.0, "Jarred down and worked pipe free", SP),
            (1.5, "Circulated hole clean after freeing stuck pipe", SP),
            (10.0, drill("8-1/2", 3930, 4140), P)]},
        13: {"cost": 371_000, "lines": [
            (7.0, drill("8-1/2", 4140, 4290), P),
            (1.0, "Circulated bottoms up and flow checked", P),
            (6.0, "Waited on weather, wind gusting 45 kn, crane operations suspended", WOW),
            (10.0, drill("8-1/2", 4290, 4480), P)]},
        14: {"cost": 395_500, "lines": [
            (6.0, drill("8-1/2", 4480, 4600), P),
            (2.5, "Partial losses of 30 bbl/hr, lost returns at 4,600 m", LC),
            (3.0, "Pumped LCM pill and monitored losses until static", LC),
            (12.5, drill("8-1/2", 4600, 4830), P)]},
    }},
    "W-02": {"size": "12-1/4", "days": {
        10: {"cost": 447_000, "lines": [
            (8.0, drill("12-1/4", 1850, 2000), P),
            (2.0, "MWD pulser failed, no signal at surface", TF),
            (5.0, "POOH to replace failed MWD tool", TF),
            (5.0, "RIH with backup MWD tool after failure", TF),
            (4.0, drill("12-1/4", 2000, 2080), P)]},
        11: {"cost": 452_500, "lines": [
            (6.0, drill("12-1/4", 2080, 2150), P),
            (0.5, "Observed pit gain of 8 bbl, kick detected", WC),
            (1.0, "Shut in well and recorded pressures", WC),
            (4.5, "Circulated out influx with driller's method", WC),
            (1.0, "Flow checked, well static", P),
            (11.0, drill("12-1/4", 2150, 2220), P)]},
        12: {"cost": 468_000, "lines": [
            (3.0, drill("12-1/4", 2220, 2260), P),
            (4.0, "Total losses, no returns at 2,260 m", LC),
            (5.0, "Pumped LCM pill, losses reduced to 10 bbl/hr", LC),
            (6.0, "Built mud volume and monitored losses", LC),
            (6.0, "Reamed hole back to bottom", P)]},
        13: {"cost": 405_000, "lines": [
            (4.0, drill("12-1/4", 2260, 2300), P),
            (12.0, "WOW, swell above 4 m, supply vessel could not offload", WOW),
            (8.0, "Performed planned maintenance on mud pumps", P)]},
        14: {"cost": 418_000, "remarks": [5], "lines": [
            (6.0, drill("12-1/4", 2300, 2450), P),
            (2.5, drill("12-1/4", 2450, 2610), P),
            (5.5, "POOH, MWD tool stopped sending data", TF),
            (2.0, "Waited on weather, wind above crane limit", WOW),
            (4.5, "Tested BOP as per schedule", P),
            (0.5, "Waited on tong repair before slip and cut", WOE),
            (3.0, "Slipped and cut drill line, serviced rig", P)]},
    }},
    "W-03": {"size": "17-1/2", "days": {
        10: {"cost": 331_000, "lines": [
            (6.0, drill("17-1/2", 850, 960), P),
            (2.0, "Pipe stuck at 960 m while backreaming, unable to rotate", SP),
            (5.0, "Jarred up and worked pipe free", SP),
            (11.0, drill("17-1/2", 960, 1150), P)]},
        11: {"cost": 318_500, "lines": [
            (9.0, drill("17-1/2", 1150, 1330), P),
            (1.0, "Circulated hole clean", P),
            (3.0, "Wiper trip to shoe and back to bottom", P),
            (11.0, drill("17-1/2", 1330, 1520), P)]},
        12: {"cost": 344_000, "lines": [
            (5.0, drill("17-1/2", 1520, 1610), P),
            (4.5, "Top drive failed, troubleshot hydraulic leak", TF),
            (1.5, "Circulated hole while top drive failure was fixed", TF),
            (13.0, drill("17-1/2", 1610, 1850), P)]},
        13: {"cost": 336_000, "overlap": (3, 30), "lines": [
            (8.0, drill("17-1/2", 1850, 2010), P),
            (2.0, "Waited on replacement shaker screens, awaiting parts", WOE),
            (4.0, drill("17-1/2", 2010, 2090), P),
            (4.0, "Circulated and conditioned mud, raised mud weight", P),
            (6.0, drill("17-1/2", 2090, 2200), P)]},
        14: {"cost": 362_000, "lines": [
            (3.0, drill("17-1/2", 2200, 2260) + ", section TD", P),
            (2.0, "Circulated hole clean at TD", P),
            (5.0, "POOH to run casing", P),
            (4.0, "Waited on casing crew, awaiting equipment from shore base", WOE),
            (6.0, "Ran 13-3/8 in casing to 2,255 m", P),
            (4.0, "Cemented casing, waited on cement (WOC) as per program", P)]},
    }},
}

WELL_PLAN = {  # planned end of day depth (m)
    "W-01": {10: 3600, 11: 3860, 12: 4100, 13: 4400, 14: 4800},
    "W-02": {10: 2050, 11: 2200, 12: 2300, 13: 2480, 14: 2650},
    "W-03": {10: 1180, 11: 1500, 12: 1800, 13: 2150, 14: 2260},
}
BUDGET = {"W-01": 380_000, "W-02": 420_000, "W-03": 350_000}

PAST_INCIDENTS = [  # (well, day, category, cause, fix, hours_lost)
    ("W-02", 6, TF, "MWD lost signal at high temperature", "Swapped to a high temperature MWD", 9.0),
    ("W-01", 9, TF, "MWD pulser failure", "Carried a backup tool on the rig", 11.5),
    ("W-01", 4, SP, "Differential sticking across depleted sand", "Minimised static time, added lubricant to mud", 7.0),
    ("W-03", 7, SP, "Hole packing off while backreaming", "Improved hole cleaning, controlled backreaming speed", 6.5),
    ("W-02", 8, LC, "Losses into fractured limestone", "Pre-treated mud with LCM before entering the zone", 13.0),
    ("W-01", 6, WOW, "High winds stopped crane operations", "Moved critical lifts to the morning weather window", 8.0),
    ("W-03", 5, WOE, "Casing crew arrived late from shore base", "Booked crews 48 h ahead of section TD", 5.0),
    ("W-02", 3, WC, "Gas kick from shallow sand", "Raised mud weight 0.2 ppg before the sand", 7.5),
    ("W-03", 8, TF, "Top drive hydraulic leak", "Added top drive hoses to the weekly inspection", 4.0),
]


def hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def build_report(well, size, day, spec):
    lines = spec["lines"]
    remarks_idx = set(spec.get("remarks", []))
    overlap = spec.get("overlap")
    t = 0
    log, remarks, truth = [], [], []
    depth = None
    for i, (hours, text, cat) in enumerate(lines):
        start, end = t, t + int(round(hours * 60))
        t = end
        shown_start = start - overlap[1] if overlap and overlap[0] == i else start
        row = f"{hhmm(shown_start)}-{hhmm(end)} {text}"
        if "Drilled" in text:
            depth = int(text.split(" to ")[1].split(" m")[0].replace(",", ""))
        if i in remarks_idx:
            remarks.append(row)
            truth.append(("remarks", len(remarks) - 1, hours, cat))
        else:
            log.append(row)
            truth.append(("log", len(log) - 1, hours, cat))
    assert t == 24 * 60, f"{well} D{day} adds to {t / 60} h"
    header = f"WELL {well}  DAY {day}  DEPTH {depth:,} m  COST USD {spec['cost']:,}"
    text_lines = [header] + log
    if remarks:
        text_lines.append("REMARKS: " + remarks[0])
        text_lines += remarks[1:]
    # source line numbers (1-based, header is line 1)
    def line_no(where, k):
        return 2 + k if where == "log" else 2 + len(log) + k
    npt_hours, npt_lines, line_labels = {}, [], {}
    for where, k, hours, cat in truth:
        n = line_no(where, k)
        line_labels[str(n)] = cat
        if cat != P:
            npt_hours[cat] = npt_hours.get(cat, 0) + hours
            npt_lines.append(n)
    drilled = [(h, x) for h, x, c in lines if x.startswith("Drilled")]
    metres = sum(int(x.split(" to ")[1].split(" m")[0].replace(",", "")) -
                 int(x.split("from ")[1].split(" m")[0].replace(",", "")) for _, x in drilled)
    drill_h = sum(h for h, _ in drilled)
    key = {
        "well_id": well, "day": day,
        "total_hours": 24.0,
        "npt_hours": npt_hours,
        "total_npt": round(sum(npt_hours.values()), 2),
        "npt_lines": npt_lines,
        "line_labels": line_labels,
        "depth_m": depth,
        "depth_vs_plan_m": depth - WELL_PLAN[well][day],
        "rop_m_per_h": round(metres / drill_h, 1) if drill_h else None,
        "day_cost_usd": spec["cost"],
        "planted_error": None,
        "critic_should_fail_first_pass": False,
    }
    if remarks_idx:
        key["planted_error"] = "line hidden in REMARKS, first pass adds to 23.5 h"
        key["critic_should_fail_first_pass"] = True
    if overlap:
        key["planted_error"] = "two log lines overlap by 30 min, log adds to 24.5 h"
        key["critic_should_fail_first_pass"] = True
    return "\n".join(text_lines) + "\n", key


def main():
    REPORTS.mkdir(parents=True, exist_ok=True)
    answer_key = {}
    for well, w in SPEC.items():
        for day, spec in w["days"].items():
            rid = f"{well}_D{day}"
            text, key = build_report(well, w["size"], day, spec)
            (REPORTS / f"{rid}.txt").write_text(text, encoding="utf-8")
            answer_key[rid] = key
    (DATA / "answer_key.json").write_text(json.dumps(answer_key, indent=2), encoding="utf-8")
    (DATA / "npt_codes.json").write_text(json.dumps(NPT_CODES, indent=2), encoding="utf-8")
    with open(DATA / "well_plan.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["well_id", "day", "planned_depth_m"])
        for well, days in WELL_PLAN.items():
            for day, d in days.items():
                wr.writerow([well, day, d])
    with open(DATA / "cost_budget.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["well_id", "daily_budget_usd"])
        for well, b in BUDGET.items():
            wr.writerow([well, b])
    db = DATA / "incidents.sqlite"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE incidents (id INTEGER PRIMARY KEY, well_id TEXT, day INTEGER,
                   category TEXT, cause TEXT, fix TEXT, hours_lost REAL, source TEXT DEFAULT 'history')""")
    con.execute("""CREATE TABLE failed_cases (id INTEGER PRIMARY KEY, report_id TEXT, attempt INTEGER,
                   problems TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("""CREATE TABLE engineer_feedback (id INTEGER PRIMARY KEY, report_id TEXT, decision TEXT,
                   comment TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    con.executemany("INSERT INTO incidents (well_id, day, category, cause, fix, hours_lost) VALUES (?,?,?,?,?,?)",
                    PAST_INCIDENTS)
    con.commit()
    con.close()
    n_err = sum(k["critic_should_fail_first_pass"] for k in answer_key.values())
    n_clean = sum(not k["npt_hours"] for k in answer_key.values())
    print(f"wrote {len(answer_key)} reports ({n_clean} clean, {n_err} with planted errors)")


if __name__ == "__main__":
    main()
