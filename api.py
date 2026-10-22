"""Reading the Rig - web API and server-rendered pages for the frontend in web/.

Wraps the LangGraph agents. Runs stream each agent step to the browser as
Server-Sent Events, and the engineer's decision resumes the paused graph.
Pages (/, /evaluation, /memory, /architecture) are rendered on the server with their own title, description,
canonical URL, Open Graph tags and structured data; unknown paths get a real 404 page.
Run: python -m uvicorn api:app --port 8000   then open http://127.0.0.1:8000
"""
import hashlib
import html
import json
import logging
import os
import time
import uuid
import warnings
from collections import OrderedDict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Literal

warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse  # noqa: E402,E501
from fastapi.staticfiles import StaticFiles  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

import graph  # noqa: E402
import llm  # noqa: E402
import memory  # noqa: E402

log = logging.getLogger("rig-sight")
ROOT = Path(__file__).parent
DATA, WEB = ROOT / "data", ROOT / "web"
KEYS = json.loads((DATA / "answer_key.json").read_text())
REPORT_TEXT = {rid: (DATA / "reports" / f"{rid}.txt").read_text() for rid in KEYS}
SITE_URL = os.getenv("RIG_SITE_URL", "https://rig-sight.onrender.com").rstrip("/")
RUN_LIMIT = int(os.getenv("RIG_RUN_LIMIT", "30"))           # agent runs + reviews per client per window
RUN_WINDOW = int(os.getenv("RIG_RUN_WINDOW", "600"))        # seconds
MAX_RUNS_KEPT = 500
MAX_REPORT_CHARS = 20_000
MAX_COMMENT_CHARS = 1_000

# cache-busting version for static files: changes whenever a web file changes
ASSET_V = hashlib.sha1(b"".join(p.read_bytes() for p in sorted(WEB.glob("*.*")) if p.suffix in
                                {".css", ".js", ".svg", ".webp", ".png", ".jpg"})).hexdigest()[:10]
TEMPLATE = (WEB / "index.html").read_text(encoding="utf-8")
LASTMOD = date.fromtimestamp(max(p.stat().st_mtime for p in WEB.glob("*.*"))).isoformat()

app = FastAPI(title="Reading the Rig", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)   # Starlette skips text/event-stream, so streaming still works
_graphs: dict[bool, object] = {}
_runs: "OrderedDict[str, bool]" = OrderedDict()   # thread_id -> critic on/off, oldest dropped first
_hits: dict[str, deque] = {}


# ---------------- security and caching headers ----------------

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()")
    path = request.url.path
    if path.startswith("/static/"):
        h["Cache-Control"] = "public, max-age=31536000, immutable" if request.query_params.get("v") \
            else "public, max-age=3600"
    elif path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    elif "text/html" in h.get("content-type", ""):
        h["Cache-Control"] = "no-cache"
    return resp


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def rate_limit(request: Request) -> None:
    """Agent runs cost CPU: allow RUN_LIMIT per client per RUN_WINDOW seconds."""
    now, q = time.time(), _hits.setdefault(client_ip(request), deque())
    while q and now - q[0] > RUN_WINDOW:
        q.popleft()
    if len(q) >= RUN_LIMIT:
        raise HTTPException(429, f"Too many runs. Try again in {int(RUN_WINDOW - (now - q[0])) + 1} seconds.")
    q.append(now)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(x) for x in first.get("loc", [])[1:]) or "request"
    return JSONResponse({"detail": f"Invalid {field}: {first.get('msg', 'bad value')}"}, status_code=422)


@app.exception_handler(StarletteHTTPException)
async def not_found(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and not request.url.path.startswith("/api/"):
        return HTMLResponse(render_page("/404"), status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


# ---------------- graph helpers ----------------

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
        except Exception:                                        # log details, show a plain message
            log.exception("run %s failed", thread)
            yield f"event: error\ndata: {json.dumps({'message': 'The agents hit an error on this run. Try again.'})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- API ----------------

class RunIn(BaseModel):
    report_id: str = Field("CUSTOM", pattern=r"^(CUSTOM|W-\d{2}_D\d{2})$")
    report_text: str | None = Field(None, max_length=MAX_REPORT_CHARS)
    critic: bool = True


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field("", max_length=MAX_COMMENT_CHARS)


class ModelIn(BaseModel):
    name: Literal["ollama", "groq", "mock"]


@app.get("/api/reports")
def reports():
    return [{"id": rid, "well": k["well_id"], "day": k["day"], "tag": tag_for(rid),
             "planted_error": k["critic_should_fail_first_pass"], "text": REPORT_TEXT[rid]} for rid, k in KEYS.items()]


@app.get("/api/status")
def status():
    ok, msg = llm.ollama_status()
    gok, gmsg = llm.groq_status()
    return {"backend": llm.backend(), "model": llm.model_name() if llm.backend() != "mock" else "mock keyword classifier",
            "ollama_ok": ok, "ollama_message": msg, "groq_ok": gok, "groq_message": gmsg,
            "ollama_model": llm.MODEL, "groq_model": llm.GROQ_MODEL}


@app.post("/api/model")
def set_model(m: ModelIn):
    llm.set_backend(m.name)
    return status()


@app.post("/api/runs")
def start_run(r: RunIn, request: Request):
    if r.report_id != "CUSTOM" and r.report_id not in KEYS:
        raise HTTPException(404, "Unknown report")
    if r.report_id == "CUSTOM" and not (r.report_text or "").strip():
        raise HTTPException(400, "Paste a report first")
    rate_limit(request)
    thread = f"{r.report_id}-{uuid.uuid4().hex}"
    _runs[thread] = r.critic
    while len(_runs) > MAX_RUNS_KEPT:
        _runs.popitem(last=False)
    inp = {"report_id": r.report_id} | ({"report_text": r.report_text} if r.report_id == "CUSTOM" else {})
    return sse(thread, inp)


@app.post("/api/runs/{thread}/review")
def review(thread: str, rv: ReviewIn, request: Request):
    if thread not in _runs:
        raise HTTPException(404, "Unknown run. It may have expired: run the report again.")
    if get_graph(_runs[thread]).get_state(cfg(thread)).next != ("human_review",):
        raise HTTPException(409, "This run is not waiting for a review")
    rate_limit(request)
    return sse(thread, Command(resume={"decision": rv.decision, "comment": rv.comment}))


@app.get("/api/runs/{thread}")
def run_state(thread: str):
    if thread not in _runs:
        raise HTTPException(404, "Unknown run")
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
def memory_reset(request: Request):
    rate_limit(request)
    memory.reset()
    return memory_tables()


# ---------------- pages (server-rendered metadata) ----------------

DESC = ("A LangGraph team of AI agents reads a Daily Drilling Report, measures non-productive time with "
        "Python tools, checks its own work and waits for a drilling engineer to approve.")
PAGES = {
    "/": ("Reading the Rig · Agentic AI for Daily Drilling Reports", DESC, "analyse", None),
    "/evaluation": ("Evaluation · Reading the Rig",
                    "How the agents score on 15 synthetic drilling reports with known answers, compared with a "
                    "single-prompt chatbot on the same model.", "evaluation", "Evaluation"),
    "/memory": ("Incident memory · Reading the Rig",
                "Past NPT incidents, failed checks and engineer decisions that the agents read and write.",
                "memory", "Memory"),
    "/architecture": ("Architecture · Reading the Rig",
                      "How the LangGraph agents work together: Orchestrator, Planner, Research, Executor, Critic "
                      "and the engineer in the loop.", "architecture", "Architecture"),
    "/404": ("Page not found · Reading the Rig", "This page does not exist.", "notfound", None),
}


def jsonld(path: str, title: str, desc: str, crumb: str | None) -> str:
    data = [{"@context": "https://schema.org", "@type": "WebApplication", "name": "Reading the Rig",
             "url": SITE_URL + "/", "description": DESC, "applicationCategory": "BusinessApplication",
             "operatingSystem": "Any (web browser)", "isAccessibleForFree": True,
             "creator": [{"@type": "Person", "name": "Ranjan Verma"}, {"@type": "Person", "name": "Rahul Narkhede"}]}]
    if crumb:
        data.append({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": crumb, "item": SITE_URL + path}]})
    return json.dumps(data).replace("</", "<\\/")


def render_page(path: str) -> str:
    title, desc, view, crumb = PAGES[path]
    canonical = SITE_URL + (path if path != "/404" else "/")
    out = TEMPLATE
    for key, val in {"TITLE": html.escape(title), "DESCRIPTION": html.escape(desc), "CANONICAL": canonical,
                     "SITE": SITE_URL, "VIEW": view, "V": ASSET_V,
                     "ROBOTS": "noindex" if path == "/404" else "index, follow",
                     "JSONLD": jsonld(path, title, desc, crumb)}.items():
        out = out.replace("{{" + key + "}}", val)
    return out


def page(path: str):
    return HTMLResponse(render_page(path))


for _p in ("/", "/evaluation", "/memory", "/architecture"):
    app.add_api_route(_p, (lambda p=_p: page(p)), methods=["GET", "HEAD"], include_in_schema=False)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse(f"User-agent: *\nAllow: /\nDisallow: /api/\n\nSitemap: {SITE_URL}/sitemap.xml\n")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    urls = "".join(f"<url><loc>{SITE_URL}{p}</loc><lastmod>{LASTMOD}</lastmod></url>"
                   for p in ("/", "/evaluation", "/memory", "/architecture"))
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                    media_type="application/xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(WEB / "favicon.ico", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/healthz", include_in_schema=False)
def health():
    return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}


app.mount("/static", StaticFiles(directory=WEB), name="static")
