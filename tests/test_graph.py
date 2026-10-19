"""End-to-end graph behaviour: the dataset against its answer key, the Critic, retries,
human review, the no-Critic ablation, malformed reports and the LLM honesty guards."""
import json
from pathlib import Path

import pytest
from langgraph.types import Command

import graph
import llm
import memory

ROOT = Path(__file__).resolve().parents[1]
KEYS = json.loads((ROOT / "data" / "answer_key.json").read_text())


@pytest.mark.parametrize("rid", list(KEYS))
def test_every_report_hours_and_structure(run, rid):
    """Model-independent checks on all 15 reports: 24 h, Critic PASS, evidence, depth, ROP."""
    s, _ = run(rid)
    k = KEYS[rid]
    assert s["total_hours"] == 24.0
    assert s["critic_verdict"] == "PASS"
    assert s["depth_vs_plan_m"] == k["depth_vs_plan_m"]
    assert s["rop"]["rop_m_per_h"] == k["rop_m_per_h"]
    lines = (ROOT / "data" / "reports" / f"{rid}.txt").read_text().splitlines()
    for a in s["activities"]:
        if a["category"] != "productive":
            assert a["text"] in lines[a["source_line"] - 1]       # every NPT claim cites its own line
    assert sum(a["hours"] for a in s["activities"] if a["category"] != "productive") == pytest.approx(s["total_npt"])


def test_mock_matches_answer_key_except_the_woc_trap(run):
    """With the mock, 14/15 reports give exactly the right NPT hours. W-03 D14 'waited on cement'
    is a deliberate trap: planned work the keyword rules call NPT, flagged as low confidence."""
    wrong = {}
    for rid, k in KEYS.items():
        s, _ = run(rid)
        if s["npt_hours"] != pytest.approx(k["npt_hours"]):
            wrong[rid] = s
    assert list(wrong) == ["W-03_D14"]
    assert any("low confidence" in w for w in wrong["W-03_D14"]["critic_warnings"])


def test_clean_report_has_zero_npt(run):
    s, _ = run("W-01_D10")
    assert s["total_npt"] == 0 and s["npt_hours"] == {} and "No non-productive time" in s["draft_report"]


def test_critic_catches_hidden_remarks_line_and_retry_fixes_it(run):
    s, _ = run("W-02_D14")
    first, last = s["attempts"][0], s["attempts"][-1]
    assert first["verdict"] == "FAIL" and first["total_hours"] == 23.5
    assert any("20:30-21:00" in p for p in first["problems"])
    assert last["verdict"] == "PASS" and s["retry_count"] == 1
    assert s["npt_hours"] == {"tool_failure": 5.5, "waiting_on_weather": 2.0, "waiting_on_equipment": 0.5}
    assert "recovered from REMARKS" in s["draft_report"]           # the correction is disclosed, not hidden


def test_critic_catches_overlap(run):
    s, _ = run("W-03_D13")
    first = s["attempts"][0]
    assert first["verdict"] == "FAIL" and first["total_hours"] == 24.5
    assert any("overlap" in p for p in first["problems"])
    assert s["critic_verdict"] == "PASS" and s["total_hours"] == 24.0
    assert "confirm with the rig crew" in s["draft_report"]


def test_failed_cases_are_written_to_memory(run):
    run("W-02_D14")
    assert any(r["report_id"] == "W-02_D14" for r in memory.table("failed_cases"))


def test_without_critic_errors_slip_through():
    from langgraph.checkpoint.memory import MemorySaver
    app = graph.build(MemorySaver(), use_critic=False)
    s = graph.run_until_review(app, "W-02_D14", "nc")
    assert s["total_hours"] == 23.5 and not s.get("attempts")
    assert "Critic was switched off" in s["draft_report"]


def test_approve_ends_the_graph(app, run):
    s, cfg = run("W-02_D10")
    assert app.get_state(cfg).next == ("human_review",)
    app.invoke(Command(resume={"decision": "approve", "comment": "ok"}), cfg)
    snap = app.get_state(cfg)
    assert snap.next == () and snap.values["engineer_decision"] == "approve"
    assert memory.table("engineer_feedback")[0]["decision"] == "approve"


def test_reject_with_override_reruns_and_corrects(app, run):
    s, cfg = run("W-03_D14")
    assert s["total_npt"] == 8.0                                     # mock counts WOC as NPT
    app.invoke(Command(resume={"decision": "reject", "comment": "line 7 is planned, not NPT"}), cfg)
    snap = app.get_state(cfg)
    assert snap.next == ("human_review",)                            # back for approval after the re-run
    v = snap.values
    assert v["overrides"] == {"7": "productive"} and v["total_npt"] == 4.0
    assert [a for a in v["activities"] if a["source_line"] == 7][0]["labelled_by"] == "engineer"


def test_parse_overrides():
    assert graph.parse_overrides("Line 7 is planned, not NPT") == {"7": "productive"}
    assert graph.parse_overrides("line 4 is stuck pipe; line 5 weather") == {"4": "stuck_pipe",
                                                                              "5": "waiting_on_weather"}
    assert graph.parse_overrides("looks fine") == {}


BAD = """WELL W-02  DAY 15  DEPTH 2,700 m  COST USD 431,000
00:00-05:00 Drilled 12-1/4 in hole from 2,610 m to 2,700 m
05:00-07:30 Mud motor failure, POOH to change motor
07:30-12:00 RIH with new motor
12:00-2:00 Circulated and conditioned mud
14:00-24:00 Waited on weather, swell above 4 m
"""


def test_invalid_timestamp_is_flagged_not_repaired(run):
    s, _ = run("CUSTOM", BAD)
    assert s["critic_verdict"] == "FAIL"
    assert [i["line"] for i in s["parse_issues"]] == [5]
    assert all(a["source_line"] != 5 for a in s["activities"])      # the bad line is not guessed at
    assert len(s["attempts"]) <= 2                                   # at most one retry (for the gap), then it stops
    assert "not resolved" in s["draft_report"] and "Line 5 cannot be used" in s["draft_report"]
    assert s["total_hours"] == 22.0                                  # reported as found


def test_missing_header_and_no_well_plan(run):
    s, _ = run("CUSTOM", "daily report\n00:00-24:00 Drilled 8-1/2 in hole from 100 m to 300 m\n")
    assert s["critic_verdict"] == "FAIL"
    assert any("Header line is missing" in p for p in s["critic_feedback"])
    assert s["depth_vs_plan_m"] is None
    assert any("no well/day" in w for w in s["reference_warnings"])


def test_report_with_no_time_log_terminates(run):
    s, _ = run("CUSTOM", "WELL W-01  DAY 10  DEPTH 3,640 m\nnothing useful here\n")
    assert s["critic_verdict"] == "FAIL" and s["activities"] == []
    assert any("No valid time-log entries" in p for p in s["critic_feedback"])


def test_unknown_well_runs_with_warnings(run):
    s, _ = run("CUSTOM", "WELL W-77  DAY 3  DEPTH 900 m  COST USD 300,000\n00:00-24:00 Drilled 17-1/2 in hole "
                         "from 700 m to 900 m\n")
    assert s["critic_verdict"] == "PASS" and s["depth_vs_plan_m"] is None and s["cost_status"] == "unknown"
    assert any("W-77" in w for w in s["reference_warnings"])


def test_worst_case_stops_after_three_retries(monkeypatch, run):
    from langgraph.checkpoint.memory import MemorySaver
    original = graph.critic

    def always_fail(state):
        out = original(state)
        return out | {"critic_verdict": "FAIL", "critic_retryable": True,
                      "retry_count": state.get("retry_count", 0) + 1}
    monkeypatch.setattr(graph, "critic", always_fail)
    app = graph.build(MemorySaver())
    s, cfg = run("W-02_D14", application=app)
    assert s["retry_count"] == graph.MAX_RETRIES
    assert app.get_state(cfg).next == ("human_review",)              # still reaches the engineer


def test_mock_is_reported_as_mock(run):
    s, _ = run("W-02_D10")
    assert llm.backend() == "mock"
    assert {a["labelled_by"] for a in s["activities"]} == {"mock"}
    assert "labelled by: mock" in s["draft_report"] and "summary written by: template" in s["draft_report"]


def test_llm_summary_with_invented_number_is_rejected(monkeypatch, run):
    monkeypatch.setattr(llm, "write_summary", lambda facts: "The rig lost 99 hours today.")
    s, _ = run("W-02_D10")
    assert "99 hours" not in s["draft_report"] and "summary written by: template" in s["draft_report"]


def test_llm_summary_with_only_computed_numbers_is_kept(monkeypatch, run):
    monkeypatch.setattr(llm, "write_summary", lambda facts: "Well W-02 lost 12 hours of NPT on day 10.")
    s, _ = run("W-02_D10")
    assert "lost 12 hours" in s["draft_report"] and f"summary written by: {llm.MODEL}" in s["draft_report"]


def test_ollama_unavailable_falls_back_to_mock():
    llm._status_cache.clear()
    assert llm.set_backend("ollama") == "mock"
