# Reading the Rig

**Agentic AI for Daily Drilling Report (DDR) analysis and non-productive time (NPT) detection.**
CA-3 · Foundation of Agentic AI · TY AI-ML B · Symbiosis Institute of Technology

> Every rig writes a daily report. Lost time hides inside it. A team of AI agents finds it,
> checks it and hands it to an engineer to approve. **Computed** by Python, **Sourced** to a
> report line, **Approved** by a person.

## Run it

```bash
pip install -r requirements.txt
python data/generate_data.py      # rebuilds the 15 reports, answer key, reference files, memory
streamlit run app.py              # the PoC UI
python evaluate.py                # scores all 15 reports, writes results/metrics.json
python graph.py                   # one run in the terminal (W-02 Day 14)
```

Optional local LLM (free, no API key):

```bash
ollama pull qwen2.5:3b
```

With Ollama running, the app and `evaluate.py` use it automatically (`RIG_LLM=auto`). Without it, a
keyword classifier built from `npt_codes.json` stands in so the demo never stalls. `RIG_MODEL`
picks another model, and `RIG_LLM=mock` forces the mock.

## Deploy on Streamlit Community Cloud

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io), click **Create app**, pick the repo, branch
   `main` and main file `app.py`, and choose Python 3.11 or 3.12 under **Advanced settings**
3. Deploy. `requirements.txt` and `.streamlit/config.toml` (dark theme) are picked up automatically

The cloud has no Ollama, so the hosted app runs on the mock classifier automatically (the sidebar
says so). The Evaluation tab shows the committed Ollama results from `results/metrics.json`. For
the live LLM demo, run the app locally with Ollama running.

## 1. Problem statement and domain

**Domain:** oil and gas drilling operations.

Every rig sends a Daily Drilling Report each morning: an hour-by-hour time log of the last 24 hours,
mostly free text typed by the crew. **Non-productive time (NPT)** is rig time that is paid for while
the well does not move forward: stuck pipe, lost circulation, tool failure, waiting on weather or
equipment, well control. At roughly $400,000 a day for a drillship, every lost hour costs about
$16,000.

**How it is done today:** an engineer reads each report by eye. Small losses are never added up,
labels differ from person to person and lessons from past wells are not reused.

**The question:** how can an engineer get a reliable account of lost time from every daily report
without reading each one line by line?

## 2. Proposed solution

A LangGraph multi-agent system reads each report, measures lost hours with Python tools, matches
them to past incidents, checks its own work and pauses for the drilling engineer to approve or
reject a short, evidence-backed Daily Insight Report.

| | Manual today | Plain chatbot | This system |
|---|---|---|---|
| Who reads | Engineer, by eye | One LLM prompt | Agents with set roles |
| Numbers | Often not totalled | Model guesses sums | Python tools compute them |
| Checks itself | No | No | Critic, up to 3 retries |
| Uses past wells | If someone remembers | No | Incident memory (SQLite) |
| Proof per claim | No | No | Every NPT hour cites its line |
| Human sign-off | Yes, slow | No | `interrupt()` inside the graph |

## 3. Agents and orchestration

| Role | Node | Does | LLM? |
|---|---|---|---|
| Orchestrator | `orchestrator` + `route()` | Reads the shared state and picks the next agent | No (plain Python, predictable) |
| Planner | `planner` | Turns the report into a task list that adapts to its content (REMARKS present? drilling lines? cost?) | No |
| Research | `research` | Well plan, budget, NPT codes, similar past incidents from memory | No |
| Executor | `executor` + `tools` (ToolNode) | Calls tools: parse, classify, sum, ROP, depth vs plan, cost vs budget | Classify only |
| Critic | `critic` | Hard checks, structured feedback, writes failed cases to memory | No |
| Report writer | `report_writer` | Daily Insight Report from computed facts only | Optional rephrase |
| Engineer | `human_review` | `interrupt()` pause, approve → END, reject + comment → back to work | Human |

**Routing rules** (`graph.py`, `make_route`), first match wins:

1. no `task_plan` → Planner
2. no `well_plan` / `similar_events` → Research
3. no `activities` → Executor
4. no `critic_verdict` → Critic
5. FAIL and `retry_count < 3` → Executor (with the Critic's fix hints)
6. PASS, or retries used up → report_writer → human_review
7. Engineer rejects → activities cleared, comment parsed into line overrides → rule 3

`recursion_limit = 50`. Tested: a normal report takes 16 steps, a report with one retry takes 24, and
the worst case (the Critic fails every time, 3 retries) takes 32 steps and still reaches the engineer.

**The Executor and the ToolNode:** visit 1 asks the ToolNode to `parse_time_log`; visit 2 asks for
`classify_activities`, `sum_hours`, `calc_rop`, `depth_vs_plan` and `cost_vs_budget` in parallel;
visit 3 reads the results and totals NPT by category in Python.

**What the Critic checks:** the log adds to 24.0 h; no time gaps or overlaps (it names the exact
window); every category is valid; every NPT claim's text really is on its cited source line; the
deepest drilled depth matches the header. Confidence below 0.7 is flagged for the engineer, not
failed.

**Self-correction is real, not scripted:** the Critic's feedback is structured (`fix_hints`). A gap
hint makes the parser look in REMARKS for an entry inside exactly that window; an overlap hint
trims the later line's start. The first pass reads only the time log on purpose, because REMARKS
often repeat log entries and would double count.

**Memory:** short term is `DDRState` for one run. Long term is `data/incidents.sqlite` (past
incidents with cause → fix, failed cases from the Critic, engineer decisions). The SQLite
checkpointer (`checkpoints.db`) saves state after every step, which is what lets the graph pause
for the engineer and resume.

## 4. Architecture

The full draw.io diagram (`ddr_langgraph_architecture.drawio` / PNG / PDF) is in the deck. The app's
**Architecture** tab draws the same graph live.

```
START → ingest_report → Orchestrator ⇄ Planner
                         Orchestrator ⇄ Research ⇄ reference data, incident memory
                         Orchestrator → Executor ⇄ ToolNode ⇄ local LLM (classify)
                         Orchestrator ⇄ Critic → incident memory (failed cases)
                         Orchestrator ─FAIL & retries<3→ Executor          (retry loop)
                         Orchestrator ─PASS / retries used→ report_writer → human_review
                         human_review ─approve→ END   ─reject + comment→ Orchestrator
```

## 5. Streamlit PoC

`streamlit run app.py`:

- **Sidebar:** report picker (15 DDRs), model switch (Mock / Ollama), Critic on/off toggle for the
  ablation, Run button
- **Analyse report:** KPI tiles; the report with NPT lines highlighted (category · hours ·
  confidence, plus "recovered from REMARKS" notes); the live agent trace; a 24-hour timeline and
  NPT-by-category chart; the Planner's task list; the Critic's checks; similar past incidents; the
  Daily Insight Report; engineer Approve / Reject; a check against the answer key
- **Evaluation:** metrics table, re-run button, per-report results
- **Memory:** incidents, failed cases, engineer decisions
- **Architecture:** the graph

**Demo order (about 90 seconds):**

1. **W-01 Day 10**: a clean day, 0 h NPT
2. **W-02 Day 10**: tool failure, 12 h, right category, the MWD fix pulled from memory
3. **W-02 Day 14**: the first pass adds to 23.5 h. The Critic fails it and names the 20:30–21:00 gap,
   the retry recovers it from REMARKS, the Critic passes, and the engineer approves
   With the Ollama model, the draft also counts line 7 ("slipped and cut drill line") as 3 h NPT.
   That is the human-in-the-loop moment: reject with `line 7 is planned, not NPT` and NPT drops
   from 11 h to the correct 8 h, then approve
4. *(If time allows)* **W-03 Day 14**: line 7, "waited on cement", is the same kind of mistake. The
   same reject comment takes NPT from 8 h to the correct 4 h

Before the talk, start Ollama and warm the model up with one run (the first call loads the model,
about 1–2 minutes). If anything goes wrong, switch the sidebar to **Mock** and the demo still works.

## 6. Dataset

`data/generate_data.py` writes everything. It is synthetic: real DDRs are confidential, and because
we plant every event we know the right answer.

- 15 reports: 3 wells × days 10–14
- 2 clean days (W-01 D10, W-03 D11)
- 2 planted errors: W-02 D14 (a line hidden in REMARKS, so the log adds to 23.5 h) and W-03 D13 (two lines
  overlap by 30 min, so the log adds to 24.5 h)
- All six NPT categories appear, plus a trap: "waited on cement (WOC)" is planned work, not NPT
- `answer_key.json` gives true labels per line, NPT hours, depth vs plan and ROP
- `well_plan.csv`, `cost_budget.csv`, `npt_codes.json` and `incidents.sqlite` (9 past incidents)

## 7. Output validation and results

`python evaluate.py` runs the same 15 reports through each configuration, using the same model
for each. These are the main results, with the local LLM **qwen2.5:3b on Ollama**:

| Run | NPT hours error (MAE) | Category accuracy | Critic catch rate | Evidence rate | Logs adding to 24 h | Reports with exact NPT | False NPT lines |
|---|---|---|---|---|---|---|---|
| **Graph with Critic** | **0.47 h** | **100%** | **2/2** | **100%** | **15/15** | **13/15** | **2** |
| Graph without Critic | 0.43 h | 96.6% | n/a | 100% | 13/15 | 13/15 | 2 |
| Single-prompt chatbot | 4.43 h | 69% | n/a | 89% | 2/15 | 3/15 | 20 |

**What the numbers say**

- **The agent graph is about 10× more accurate than a chatbot using the same model** (0.47 h vs 4.43 h
  average NPT error, 13/15 vs 3/15 reports exactly right). The chatbot cannot even add up the log:
  it reported totals of 11 h, 111 h and 126 h for 24-hour reports. In our system Python does
  every sum, so all 15 add to 24 h.
- **The Critic catches both planted errors (2/2)** and fixes them before a human sees them. Without
  it, W-02 D14 shows 23.5 h (the 0.5 h tong repair is lost) and W-03 D13 shows 24.5 h.
- **Why "without Critic" has a slightly lower MAE (0.43 vs 0.47):** on W-02 D14 two errors cancel
  out. Without the Critic, the missing 0.5 h offsets part of a separate 3 h false positive. With the
  Critic, the 0.5 h is correctly recovered, so only the false positive is left. The Critic-off
  run is wrong in two ways, not one. Look at "logs adding to 24 h" and category accuracy, not MAE alone.
- **The two remaining errors are planned work the LLM calls NPT with high confidence:**
  "slipped and cut drill line, serviced rig" (W-02 D14, 3 h) and "waited on cement (WOC)" (W-03
  D14, 4 h). Because the model is confident (0.9 or more), the Critic's 0.7 floor does not flag them.
  The engineer reviews the evidence lines and fixes each with one reject comment (e.g.
  `line 7 is planned, not NPT`). **This is exactly why the human stays in the loop.**
- Line-level accuracy of the classifier: 71/73 time-log lines. A "decide planned first" prompt
  scored worse (60/73), so we kept the simpler prompt instead of tuning it to the test set.
- **The LLM never counts.** The report writer's summary is rejected and replaced by the template if
  it contains any number that Python did not compute.

With the mock keyword classifier (`RIG_LLM=mock`, no model needed), the graph scores 0.27 h MAE,
100% category accuracy, 2/2 catches, 15/15 logs adding to 24 h and 14/15 reports exact. That is high
because its keywords and the synthetic reports share a vocabulary, so treat it as a fallback,
not a result.

## 8. Limitations

- Synthetic data, 15 reports, six NPT categories
- A 3B local model mislabels some planned work as NPT with high confidence (see above), so
  confidence alone is not a safe filter
- The mock classifier only knows its keywords
- The time-log format is fixed (`HH:MM-HH:MM text`). Real DDRs vary (IADC is standardising them)
- Engineer overrides apply to that run only; they are stored but not yet learned from
- Not a certified engineering or safety tool. It is a class PoC

## 9. Future scope

- Live rig data feeds instead of files, many wells at once
- Learn from engineer corrections (few-shot examples drawn from `engineer_feedback`)
- A morning NPT alert to the drilling team
- MCP connectors so agents read company systems (WITSML, the DDR database) directly
- Trend analysis across wells: recurring causes, the cost of NPT per category

## Files

```
app.py            Streamlit UI
graph.py          DDRState, nodes, route(), graph build, human-in-the-loop
tools.py          parse, classify, sum, ROP, depth, cost + research lookups
llm.py            Ollama call + mock fallback (the only place a model is used)
memory.py         SQLite incident memory, failed cases, engineer feedback
evaluate.py       scores 15 reports: with Critic / without Critic / chatbot baseline
data/             generate_data.py, reports/, answer_key.json, reference files, incidents.sqlite
results/          metrics.json, per_report.csv
```
