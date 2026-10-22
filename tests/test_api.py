"""Web API behind the bento frontend: reports, streaming runs, engineer review, memory, metrics."""
import json

import pytest
from fastapi.testclient import TestClient

import api


@pytest.fixture
def client():
    return TestClient(api.app)


def events(resp):
    out = []
    for block in resp.text.strip().split("\n\n"):
        ev = block.split("\n")[0].removeprefix("event: ")
        out.append((ev, json.loads(block.split("data: ", 1)[1])))
    return out


def test_frontend_and_reports(client):
    assert "READING THE RIG" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    reps = client.get("/api/reports").json()
    assert len(reps) == 15 and {r["id"] for r in reps if r["planted_error"]} == {"W-02_D14", "W-03_D13"}


def test_run_streams_steps_then_pauses_for_review(client):
    ev = events(client.post("/api/runs", json={"report_id": "W-02_D14", "critic": True}))
    kinds = [e for e, _ in ev]
    assert kinds[0] == "start" and kinds[-1] == "done" and "step" in kinds
    assert any(d.get("summary", "").startswith("FAIL") for e, d in ev if e == "step")
    final = ev[-1][1]
    assert final["paused"] and final["total_npt"] == 8.0 and final["answer_key"]["total_npt"] == 8.0
    thread = ev[0][1]["thread"]
    done = events(client.post(f"/api/runs/{thread}/review", json={"decision": "approve", "comment": "ok"}))[-1][1]
    assert done["engineer_decision"] == "approve" and not done["paused"]
    assert client.post(f"/api/runs/{thread}/review", json={"decision": "approve"}).status_code == 409


def test_reject_with_override(client):
    ev = events(client.post("/api/runs", json={"report_id": "W-03_D14"}))
    thread, first = ev[0][1]["thread"], ev[-1][1]
    assert first["total_npt"] == 8.0
    again = events(client.post(f"/api/runs/{thread}/review",
                                json={"decision": "reject", "comment": "line 7 is planned, not NPT"}))[-1][1]
    assert again["total_npt"] == 4.0 and again["paused"]


def test_custom_report_and_no_critic(client):
    text = "WELL W-02  DAY 15  DEPTH 2,700 m\n00:00-12:00 Drilled 12-1/4 in hole from 2,610 m to 2,700 m\n12:00-2:00 bad\n"
    final = events(client.post("/api/runs", json={"report_id": "CUSTOM", "report_text": text}))[-1][1]
    assert final["critic_verdict"] == "FAIL" and final["parse_issues"][0]["line"] == 3 and final["answer_key"] is None
    nc = events(client.post("/api/runs", json={"report_id": "W-02_D14", "critic": False}))[-1][1]
    assert nc["total_hours"] == 23.5 and nc["critic_on"] is False


def test_bad_requests(client):
    assert client.post("/api/runs", json={"report_id": "W-99"}).status_code == 404
    assert client.post("/api/runs", json={"report_id": "CUSTOM", "report_text": " "}).status_code == 400
    assert client.post("/api/runs/nope/review", json={"decision": "approve"}).status_code == 404
    assert client.post("/api/model", json={"name": "gpt"}).status_code == 400


def test_status_memory_metrics(client):
    assert client.post("/api/model", json={"name": "mock"}).json()["backend"] == "mock"
    assert len(client.get("/api/memory").json()["incidents"]) == 9
    assert client.post("/api/memory/reset").status_code == 200
    m = client.get("/api/metrics").json()
    assert m["ollama"]["runs"]["Graph with Critic"]["critic_catch_rate"] == 1.0
