# Reading the Rig

**Agentic AI for Daily Drilling Reports: a LangGraph multi-agent system that finds non-productive time (NPT).**
CA-3 · Foundation of Agentic AI · TY AI-ML B · Symbiosis Institute of Technology, Pune ·
Ranjan Verma and Rahul Narkhede

> Every rig writes a daily report. Lost time hides inside it. A team of AI agents finds it, checks it and
> hands it to an engineer to approve. **Computed** by Python, **sourced** to a report line, **approved** by a person.

| | |
|---|---|
| **Live app** | [rig-sight.onrender.com](https://rig-sight.onrender.com/) (free tier: the first visit after a quiet spell takes about a minute) |
| **Code** | [github.com/ranjanvrma/rig-sight](https://github.com/ranjanvrma/rig-sight) |
| **Submission** | [presentation](submission/Rig_Sight_CA3_Presentation.pptx) · [report](submission/Rig_Sight_CA3_Report.docx) · [repository link](submission/Rig_Sight_CA3_Repository_Link.docx) |

All data is synthetic. This is a classroom proof of concept, not a certified engineering or safety tool.

![Reading the Rig web app](web/preview.jpg)

**Contents:** [Problem](#problem) · [How it works](#how-it-works) · [Results](#results) ·
[Quick start](#quick-start) · [Demo](#demo) · [Agents and routing](#agents-and-routing) ·
[Configuration](#configuration) · [Project structure](#project-structure) · [Data](#data) ·
[Deployment](#deployment) · [Limitations and next steps](#limitations-and-next-steps)

## Problem

Every drilling rig sends a Daily Drilling Report (DDR) each morning. Most of its hour-by-hour time log is free text
typed by the crew, so **non-productive time**, the paid rig hours where the well does not move forward, hides inside
sentences. At about $400,000 a day for a drillship, every lost hour costs roughly $16,000. Today an engineer reads
each report by eye: small losses are never added up, labels differ from person to person, and lessons from past
wells are not reused.

**Question:** how can an engineer get a reliable, evidence-backed account of lost time from every daily report
without reading each one line by line?

## How it works

1. **Read:** the report is split into a header and time-log lines.
2. **Plan and research:** a Planner lists the tasks; Research looks up the well plan, daily budget, NPT categories
   and similar past incidents.
3. **Label and count:** a language model labels each line as productive or one of six NPT categories, with a
   confidence score. Python tools do all the arithmetic: hours, NPT by category, rate of penetration, depth and cost
   against plan.
4. **Check:** a Critic checks the work (hours add to 24, no gaps or overlaps, every NPT hour cites its line). A
   fixable problem goes back with a specific hint, at most 3 times.
5. **Approve:** the graph pauses for the drilling engineer, who approves or rejects with a comment such as
   `line 6 is planned, not NPT`. A rejection re-runs the graph with that correction.

**"The model reads, Python counts":** the model only labels lines. Every number comes from a tool, so the Critic can
check it exactly.

![Architecture](web/architecture.png)

Hub and spoke: every agent hands back to the Orchestrator, and all of them read and write one shared state.
Reference data, the SQLite incident memory and the LangGraph checkpointer sit outside the graph. This is the team's
design diagram, updated to match the code; the drawing simplifies two details (classification is one
`classify_activities` tool over all lines, and the Critic also checks gaps, overlaps, unparseable lines and the
header).

**Technology:** Python · LangGraph 1.2 (StateGraph, ToolNode, `interrupt()`, SqliteSaver checkpointer) ·
LangChain Core · Groq API with `openai/gpt-oss-20b` (free tier) or a keyword mock · FastAPI + plain
HTML/CSS/JavaScript · SQLite · pytest.

## Results

15 synthetic reports with a known answer key, the same model (Groq `openai/gpt-oss-20b`) in all three runs
(23 Oct 2026, [results/metrics_groq.json](results/metrics_groq.json)):

| Run | NPT error / report | Category accuracy | Critic catch | Evidence | Logs = 24 h | Exact NPT |
|---|---|---|---|---|---|---|
| **Graph with Critic** | **0.07 h** | **100%** | **2/2** | **100%** | **15/15** | **14/15** |
| Graph without Critic | 0.77 h | 89.7% | n/a | 100% | 13/15 | 11/15 |
| Single-prompt chatbot | 0.57 h | 94.3% | n/a | 100% | 13/15 | 13/15 |

- **About 8× less error than a chatbot** on the same model, and every log adds up to 24 h.
- **The Critic caught both planted errors** before any human saw them. Without it, W-02 D14 shows 23.5 h and
  W-03 D13 shows 24.5 h, and the error rises from 0.07 h to 0.77 h.
- **The one remaining miss** is W-02 D11: the model counts a 1-hour "flow checked, well static" after a kick as well
  control; the answer key calls it productive. The engineer fixes it with one reject comment.
- **Run-to-run variation:** model output varies even at temperature 0. The chatbot was run three times (mean error
  0.63 h, 0.63 h and 0.43 h; the table shows the mean). On W-02 D14 the model sometimes labels the planned "slipped
  and cut drill line" (3 h) as NPT; rejecting with `line 7 is planned, not NPT` corrects it to 8.0 h.
- **One prompt change, reported openly:** both prompts use the same NPT definition, including "time spent recovering
  from a failure is NPT". Without that sentence the model called the trips out and back in to fix a failure
  productive (error 1.83 h).
- **Mock fallback:** 0.27 h error, 2/2 catches, 14/15 exact. That is optimistic, because the mock's keywords and the
  synthetic reports share a vocabulary.

## Quick start

Python 3.11 or newer (tested on 3.14 and 3.11; on 3.14 LangChain prints a harmless Pydantic V1 warning).

```bash
python -m venv .venv
```
```bash
.venv\Scripts\activate
```
On macOS or Linux: `source .venv/bin/activate`.
```bash
pip install -r requirements-dev.txt
```

For the real model, get a free key at [console.groq.com/keys](https://console.groq.com/keys) (no card) and put it in a
`.env` file in the project folder (git-ignored). Without a key the app uses the keyword mock and says so.

```
GROQ_API_KEY=gsk_...
```

Run the app, then open http://127.0.0.1:8000:

```bash
python -m uvicorn api:app --port 8000
```

Run the tests (58, all passing; they use the mock and a temporary database, so they never call Groq or touch the
committed data):

```bash
python -m pytest
```

Re-run the evaluation (writes `results/metrics_<backend>.json` and `per_report_<backend>.csv`; about 10 minutes on
Groq's free tier):

```bash
python evaluate.py
```

## Demo

Pick a report, press **Run agents**, watch the pipeline, then approve or reject:

1. **W-01 · Day 10:** clean day, 0 h NPT.
2. **W-02 · Day 10:** tool failure, 12 h, with the fix from a past incident pulled from memory.
3. **W-02 · Day 14:** the first pass adds to 23.5 h; the Critic names the 20:30-21:00 gap; the retry recovers it
   from REMARKS and the report matches the answer key (8.0 h).
4. **W-02 · Day 11:** if the model counts line 6 (flow check) as NPT, reject with `line 6 is planned, not NPT`; the
   re-run gives 6.0 h.
5. **Custom report:** paste any report. The sample has an invalid time (`12:00-2:00`), which is marked as a source
   error and left unrepaired.

The web app has four pages: **Analyse** (the run), **Evaluation** (the results above), **Memory** (past incidents,
the Critic's failed cases and the engineer's decisions, with a reset button) and **Architecture**.

## Agents and routing

| Role | Node | Responsibility | Uses the model? |
|---|---|---|---|
| Orchestrator | `orchestrator` + `route()` | Reads the shared `DDRState` and picks the next node | No, plain Python |
| Planner | `planner` | Task list that adapts to the report (REMARKS, drilling lines, depth, cost) | No, rules |
| Research | `research` | Well plan, budget, NPT codes, similar past incidents; missing data becomes a warning | No |
| Executor | `executor` + `tools` | Drives six tools through a LangGraph `ToolNode` in three visits; totals NPT | Labels lines only |
| Critic | `critic` | Hard checks, specific feedback, decides whether a retry can help, logs failed cases | No |
| Report writer | `report_writer` | Insight report from computed facts | Optional rephrase, off by default |
| Engineer | `human_review` | `interrupt()` pause; approve → END, reject + comment → re-run | Human |

**Routing rules** (`make_route` in [graph.py](graph.py)), first match wins:

1. no `task_plan` → Planner
2. no `well_plan` / `similar_events` → Research
3. `activities` is None → Executor
4. no `critic_verdict` → Critic
5. fixable FAIL and `retry_count < 3` → Executor with `fix_hints`
6. otherwise (PASS, an unfixable FAIL or retries used up) → report_writer → human_review
7. engineer rejects → activities and verdict cleared, comment turned into line overrides → rule 3

`recursion_limit = 50`. Measured: 16 steps for a normal report, 24 with one retry, 32 in the worst case (the Critic
failing every time), which still reaches the engineer.

**What the Critic checks:** a valid header; every time-log line parseable with possible times; at least one entry;
hours add to 24.0; no gaps or overlaps (it names the window); every activity has a valid category; every NPT claim's
text is on its cited line; the NPT total equals the NPT lines; the deepest drilled depth equals the header depth.
Model confidence below 0.7 is flagged for the engineer. Source-data errors (a bad header, an impossible time) are
reported, not repaired.

## Configuration

All settings are environment variables (or lines in `.env`); every one is optional.

| Variable | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | not set | free Groq key; read on the server only, never sent to the browser |
| `RIG_LLM` | `auto` | `auto` uses Groq when the key works, else the mock; `mock` forces the mock |
| `RIG_GROQ_MODEL` | `openai/gpt-oss-20b` | any chat model Groq serves |
| `RIG_GROQ_WAIT` | `6` | seconds to wait out a Groq rate limit before falling back to the mock |
| `RIG_LLM_SUMMARY` | off | `1` lets the model rephrase the report summary |
| `RIG_MEMORY_DB` | `runtime/memory.sqlite` | working memory database |
| `RIG_SITE_URL` | `https://rig-sight.onrender.com` | used for canonical URLs, Open Graph tags and the sitemap |
| `RIG_RUN_LIMIT` / `RIG_RUN_WINDOW` | `30` / `600` | agent runs allowed per visitor per window (seconds); more get HTTP 429 |

The model labels all lines of a report in one request, which keeps the free tier's limits out of the way. If a Groq
call fails, those lines fall back to the mock and are labelled `mock` in the app and the report.

The LLM summary is off by default for a reason found during testing with the team's earlier local model: it kept
every number but attached them to the wrong categories ("weather and equipment, 3.5 hours and 2 hours
respectively" when it was 2.0 h and 3.5 h). A guard rejects summaries with invented numbers, but it cannot catch
swapped ones.

## Project structure

```
api.py           FastAPI server: the JSON API, streaming runs and the server-rendered pages
web/             the front end (index.html, styles.css, app.js, images); no build step
graph.py         DDRState, the eight nodes, route(), graph build, human-in-the-loop
tools.py         Executor tools (ToolNode) and Research lookups; all the arithmetic
llm.py           the only place a model is called: Groq, the mock fallback, the chatbot baseline
memory.py        SQLite incident memory, failed cases, engineer feedback
evaluate.py      scores the 15 reports: with Critic, without Critic, single-prompt chatbot
data/            generate_data.py, reports/, answer_key.json, reference files, incidents.sqlite (seed)
results/         metrics_<backend>.json and per_report_<backend>.csv (groq and mock)
tests/           pytest suite: tools, graph, dataset, web API
submission/      the final presentation, report and repository-link document
runtime/         created on first run: working copy of memory and checkpoints (git-ignored)
```

The web app's pages are real URLs (`/`, `/evaluation`, `/memory`, `/architecture`, plus a 404 page), each with its
own title, description, canonical URL, Open Graph tags and structured data; `/robots.txt` and `/sitemap.xml` are
generated. It has light and dark themes, works with a keyboard and screen readers (axe-core: no WCAG 2 A/AA
violations) and fits a 390 px phone screen. The server sends security headers (a Content-Security-Policy, `nosniff`,
no framing), limits pasted reports to 20,000 characters and comments to 1,000, and caches static files by version.

## Data

`python data/generate_data.py` writes everything. Each line is written with its true label, so the answer key does
not depend on the agents.

- **15 reports:** 3 wells × days 10-14. 2 clean days (W-01 D10, W-03 D11), 11 with planted NPT covering all six
  categories, 2 planted errors (W-02 D14: a line hidden in REMARKS, so 23.5 h; W-03 D13: a 30-minute overlap, so
  24.5 h), and one trap: "waited on cement" is planned work.
- **Reference files:** `well_plan.csv` (planned depth per day), `cost_budget.csv`, `npt_codes.json` (six categories
  and crew keywords).
- **Memory:** `incidents.sqlite` holds 9 past incidents with cause and fix, plus tables the Critic and the engineer
  write to.
- **Answer key:** `answer_key.json` has the true label per line, NPT hours, depth against plan and ROP.

The app never writes to `data/`: it copies the seed memory to `runtime/memory.sqlite` on first use.

## Deployment

The app runs on **Render**'s free web service, built from this repository's `main` branch:
**https://rig-sight.onrender.com/**

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn api:app --host 0.0.0.0 --port $PORT` |
| Environment | `PYTHON_VERSION=3.12`, `GROQ_API_KEY=<your key>` (leave `RIG_LLM` unset) |
| Updates | redeploys on every push to `main` (about 1-2 minutes) |
| Sleeps | after about 15 minutes without visitors; the next visit takes about a minute |

The model runs at Groq, not on the server, so the free plan (512 MB of memory) is enough. Memory and checkpoints
live in `runtime/` on the server: they are temporary (wiped when the app restarts, sleeps or redeploys) and shared
by everyone using it. The Evaluation page shows the committed results from `results/`.

## Limitations and next steps

**Limitations**

- Synthetic data: 15 reports, three wells, one report format, six NPT categories.
- The model sometimes labels routine or planned work as NPT with high confidence, so confidence alone is not a safe
  filter. That is why the engineer approves every report.
- The online model needs a Groq key and an internet connection; the free tier has per-minute and per-day limits.
- The parser expects `HH:MM-HH:MM` entries; other formats are reported as errors, not understood.
- Overlap trimming guesses which line is wrong; it is disclosed for the rig crew to confirm.
- Engineer overrides apply to that run; they are stored but not yet learned from.
- Not a certified engineering or safety tool.

**Next steps:** live rig data feeds (WITSML) and many wells at once · learning from engineer corrections · a larger
or fine-tuned classifier evaluated on held-out reports · a morning NPT alert and cross-well trends · MCP connectors
to company systems · support for the IADC standard DDR fields.
