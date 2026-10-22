"""Reading the Rig - web API for the bento frontend in web/.

Wraps the same LangGraph agents as the Streamlit app. Runs stream each agent step to the browser as
Server-Sent Events, and the engineer's decision resumes the paused graph.
Run: python -m uvicorn api:app --port 8000   then open http://127.0.0.1:8000
"""
import json
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import graph  # noqa: E402
import llm  # noqa: E402
import memory  # noqa: E402

ROOT = Path(__file__).parent
DATA, WEB = ROOT / "data", ROOT / "web"
KEYS = json.loads((DATA / "answer_key.json").read_text())

app = FastAPI(title="Reading the Rig")
_graphs: dict[bool, object] = {}
_runs: dict[str, bool] = {}          # thread_id -> critic on/off (which compiled graph the run belongs to)


def get_graph(critic: bool):
    if critic not in _graphs:
        _graphs[critic] = graph.build(use_critic=critic)
    return _graphs[critic]


def cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}, "recursion_limit": graph.RECURSION_LIMIT}


def tag_for(rid: str) -> str:
    k = KEYS[rid]
    if k["critic_should_fail_first_pass"]:
        return "planted error"
    return "clean day" if not k["npt_hours"] else ", ".join(c.replace("_", " ") for c in k["npt_hours"])


def event_from(node: str, upd) -> dict:
    if node == "__interrupt__":
        return {"node": "human_review", "summary": "Paused, waiting for the engineer's decision", "kind": "pause"}
    upd = upd if isinstance(upd, dict) else {}
    if node == "tools":
        return {"node": "tools", "summary": "Ran " + ", ".join(m.name for m in upd.get("messages", []))}
    if upd.get("trace"):
        return {"node": node, "summary": upd["trace"][-1]["summary"]}
    if upd.get("messages"):
        return {"node": node, "summary": upd["messages"][-1].content}
    return {"node": node, "summary": ""}


STATE_KEYS = ["report_id", "report_text", "header", "task_plan", "well_plan", "budget", "similar_events",
              "reference_warnings", "activities", "parse_issues", "total_hours", "npt_hours", "total_npt", "rop",
              "depth_vs_plan_m", "cost_status", "critic_verdict", "critic_feedback", "critic_warnings", "attempts",
              "retry_count", "draft_report", "evidence_lines", "engineer_decision", "engineer_comment"]


def snapshot(thread: str) -> dict:
    snap = get_graph(_runs.get(thread, True)).get_state(cfg(thread))
    v = {k: snap.values.get(k) for k in STATE_KEYS}
    v["paused"] = snap.next == ("human_review",)
    v["critic_on"] = _runs.get(thread, True)
    v["thread"] = thread
    v["answer_key"] = KEYS.get(v.get("report_id") or "")
    return v


def sse(thread: str, inp):
    """Stream node updates, then the final state, as Server-Sent Events."""
    g = get_graph(_runs[thread])

    def gen():
        yield f"event: start\ndata: {json.dumps({'thread': thread})}\n\n"
        try:
            for chunk in g.stream(inp, cfg(thread), stream_mode="updates"):
                for node, upd in chunk.items():
                    yield f"event: step\ndata: {json.dumps(event_from(node, upd))}\n\n"
            yield f"event: done\ndata: {json.dumps(snapshot(thread), default=str)}\n\n"
        except Exception as e:                                   # surface errors to the UI instead of hanging
            yield f"event: error\ndata: {json.dumps({'message': f'{type(e).__name__}: {e}'})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- API ----------------

class RunIn(BaseModel):
    report_id: str = "CUSTOM"
    report_text: str | None = None
    critic: bool = True


class ReviewIn(BaseModel):
    decision: str
    comment: str = ""


class ModelIn(BaseModel):
    name: str


@app.get("/api/reports")
def reports():
    return [{"id": rid, "well": k["well_id"], "day": k["day"], "tag": tag_for(rid),
             "planted_error": k["critic_should_fail_first_pass"],
             "text": (DATA / "reports" / f"{rid}.txt").read_text()} for rid, k in KEYS.items()]


@app.get("/api/status")
def status():
    ok, msg = llm.ollama_status()
    return {"backend": llm.backend(), "model": llm.MODEL if llm.backend() == "ollama" else "mock keyword classifier",
            "ollama_ok": ok, "ollama_message": msg}


@app.post("/api/model")
def set_model(m: ModelIn):
    if m.name not in ("ollama", "mock"):
        raise HTTPException(400, "model must be 'ollama' or 'mock'")
    llm.set_backend(m.name)
    return status()


@app.post("/api/runs")
def start_run(r: RunIn):
    if r.report_id != "CUSTOM" and r.report_id not in KEYS:
        raise HTTPException(404, "unknown report")
    if r.report_id == "CUSTOM" and not (r.report_text or "").strip():
        raise HTTPException(400, "paste a report first")
    thread = f"{r.report_id}-{uuid.uuid4().hex[:8]}"
    _runs[thread] = r.critic
    inp = {"report_id": r.report_id} | ({"report_text": r.report_text} if r.report_id == "CUSTOM" else {})
    return sse(thread, inp)


@app.post("/api/runs/{thread}/review")
def review(thread: str, rv: ReviewIn):
    if thread not in _runs:
        raise HTTPException(404, "unknown run")
    if rv.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    if get_graph(_runs[thread]).get_state(cfg(thread)).next != ("human_review",):
        raise HTTPException(409, "this run is not waiting for a review")
    return sse(thread, Command(resume={"decision": rv.decision, "comment": rv.comment}))


@app.get("/api/runs/{thread}")
def run_state(thread: str):
    if thread not in _runs:
        raise HTTPException(404, "unknown run")
    return snapshot(thread)


@app.get("/api/metrics")
def metrics():
    out = {}
    for b in ("ollama", "mock"):
        f = ROOT / "results" / f"metrics_{b}.json"
        if f.exists():
            out[b] = json.loads(f.read_text())
    return out


@app.get("/api/memory")
def memory_tables():
    return {t: memory.table(t) for t in ("incidents", "failed_cases", "engineer_feedback")}


@app.post("/api/memory/reset")
def memory_reset():
    memory.reset()
    return memory_tables()


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")
app.mount("/figures", StaticFiles(directory=ROOT / "docs" / "figures"), name="figures")
