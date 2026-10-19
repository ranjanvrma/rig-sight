"""Deterministic tools: parsing, validation of timestamps, sums, ROP, depth and cost."""
import json

import tools


def parse(text, hints=None):
    return json.loads(tools.parse_time_log.invoke({"report_text": text, "fix_hints": hints or []}))


HEADER = "WELL W-02  DAY 14  DEPTH 2,610 m  COST USD 418,000\n"


def test_header_parsed():
    assert tools.parse_header(HEADER) == {"well_id": "W-02", "day": 14, "depth_m": 2610, "day_cost_usd": 418000}
    assert tools.parse_header("garbage line") == {}


def test_parse_simple_log():
    out = parse(HEADER + "00:00-06:00 Drilled ahead\n06:00-24:00 Circulated\n")
    acts = out["activities"]
    assert [a["hours"] for a in acts] == [6.0, 18.0]
    assert [a["source_line"] for a in acts] == [2, 3]
    assert out["issues"] == []


def test_malformed_and_invalid_lines_are_reported_not_dropped():
    text = HEADER + ("00:00-06:00 Drilled ahead\n"
                     "6:00-07:00 single digit hour\n"         # wrong format
                     "07:00-25:30 impossible time\n"           # hour > 24
                     "12:00-10:00 end before start\n"          # end <= start
                     "crew change, no times given\n"           # not a time-log entry
                     "REMARKS: free text here is fine\n")
    out = parse(text)
    kinds = {i["line"]: i["type"] for i in out["issues"]}
    assert kinds == {3: "invalid_timestamp", 4: "invalid_timestamp", 5: "invalid_timestamp", 6: "malformed"}
    assert [a["source_line"] for a in out["activities"]] == [2]


def test_remarks_read_only_with_a_gap_hint():
    text = HEADER + "00:00-12:00 Drilled\n12:30-24:00 Drilled\nREMARKS: 12:00-12:30 Waited on tong repair\n"
    assert sum(a["hours"] for a in parse(text)["activities"]) == 23.5
    out = parse(text, [{"type": "gap", "start_min": 720, "end_min": 750}])
    assert sum(a["hours"] for a in out["activities"]) == 24.0
    recovered = [a for a in out["activities"] if a["section"] == "remarks"]
    assert recovered and "recovered from REMARKS" in recovered[0]["note"]


def test_overlap_hint_trims_and_notes_the_change():
    text = HEADER + "00:00-10:00 A\n09:30-24:00 B\n"
    out = parse(text, [{"type": "overlap", "line": 3, "fix_start_min": 600}])
    b = out["activities"][1]
    assert b["start"] == "10:00" and b["hours"] == 14.0 and "confirm with the rig crew" in b["note"]


def test_sum_rop_depth_cost():
    acts = parse(HEADER + "00:00-06:00 Drilled 12-1/4 in hole from 2,300 m to 2,450 m\n"
                          "06:00-08:30 Drilled 12-1/4 in hole from 2,450 m to 2,610 m\n"
                          "08:30-24:00 Other\n")["activities"]
    assert json.loads(tools.sum_hours.invoke({"activities": acts}))["total_hours"] == 24.0
    rop = json.loads(tools.calc_rop.invoke({"activities": acts}))
    assert rop == {"drilled_m": 310, "drilling_hours": 8.5, "rop_m_per_h": 36.5, "deepest_drilled_m": 2610}
    assert json.loads(tools.depth_vs_plan.invoke({"actual_depth_m": 2610, "planned_depth_m": 2650}))[
        "depth_vs_plan_m"] == -40
    assert json.loads(tools.depth_vs_plan.invoke({"actual_depth_m": None, "planned_depth_m": 2650}))[
        "depth_vs_plan_m"] is None
    cost = json.loads(tools.cost_vs_budget.invoke({"day_cost_usd": 418000, "daily_budget_usd": 420000}))
    assert cost["cost_delta_usd"] == -2000 and cost["cost_status"].startswith("under budget")


def test_missing_reference_files_degrade_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "REF_DIR", tmp_path)            # empty folder: every file missing
    plan = json.loads(tools.lookup_well_plan.invoke({"well_id": "W-02", "day": 14}))
    assert plan["planned_depth_m"] is None and plan["daily_budget_usd"] is None and len(plan["warnings"]) == 2
    codes = json.loads(tools.get_npt_codes.invoke({}))
    assert len(codes["codes"]) == 6 and codes["warnings"]


def test_invalid_npt_codes_json(tmp_path, monkeypatch):
    (tmp_path / "npt_codes.json").write_text("{not json")
    monkeypatch.setattr(tools, "REF_DIR", tmp_path)
    assert "could not be used" in json.loads(tools.get_npt_codes.invoke({}))["warnings"][0]


def test_unknown_well_is_a_warning():
    plan = json.loads(tools.lookup_well_plan.invoke({"well_id": "W-99", "day": 1}))
    assert plan["planned_depth_m"] is None and any("W-99" in w for w in plan["warnings"])
