"""Scores the system on all 15 synthetic reports against the answer key.

Three runs on the same reports and the same model:
  1. full graph (with Critic)   2. graph without Critic   3. single-prompt chatbot baseline
Run: python evaluate.py            (RIG_LLM=mock|ollama|auto, default auto)
Writes results/metrics_<backend>.json and results/per_report_<backend>.csv (backend = ollama or mock)
"""
import csv
import os
import json
import shutil
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

import llm  # noqa: E402
import memory  # noqa: E402
from graph import build, run_until_review  # noqa: E402
from tools import report_lines  # noqa: E402

ROOT = Path(__file__).parent
DATA, OUT = ROOT / "data", ROOT / "results"


def score(pred: dict, key: dict, text: str) -> dict:
    lines = report_lines(text)
    truth_lines = {int(k): v for k, v in key["line_labels"].items()}
    labels = pred.get("line_labels", {})
    planted = key["npt_lines"]
    sets = pred.get("line_category_sets", {})
    correct = sum(1 for n in planted if labels.get(n) == truth_lines[n] or truth_lines[n] in sets.get(n, ()))
    claims = pred.get("npt_claims", [])
    traceable = sum(1 for n, t in claims if t and t in lines.get(n, ""))
    false_npt = sum(1 for n, cat in labels.items() if cat != "productive" and truth_lines.get(n) == "productive")
    return {
        "npt_abs_error": abs(pred.get("total_npt", 0) - key["total_npt"]),
        "planted_lines": len(planted), "correct_lines": correct,
        "claims": len(claims), "traceable_claims": traceable,
        "false_npt_lines": false_npt,
        "hours_ok": abs(pred.get("total_hours", 0) - 24.0) < 0.01,
    }


def from_graph(state: dict) -> dict:
    acts = state.get("activities", [])
    return {"total_npt": state.get("total_npt", 0), "total_hours": state.get("total_hours", 0),
            "line_labels": {a["source_line"]: a["category"] for a in acts},
            "npt_claims": [(a["source_line"], a["text"]) for a in acts if a["category"] != "productive"],
            "first_verdict": (state.get("attempts") or [{}])[0].get("verdict"),
            "retries": state.get("retry_count", 0), "warnings": len(state.get("critic_warnings", []))}


def from_baseline(out: dict, text: str) -> dict:
    lines = report_lines(text)
    cats = list(out["npt_hours"])
    # the chatbot cites line numbers but no category per line: give it the benefit of the doubt and
    # count a cited line as correct when its true category is any of the categories it reported
    return {"total_npt": round(sum(out["npt_hours"].values()), 2), "total_hours": out["total_hours"],
            "line_labels": {n: "npt" for n in out["npt_lines"]}, "line_category_sets": {n: set(cats) for n in out["npt_lines"]},
            "npt_claims": [(n, lines.get(n, "")) for n in out["npt_lines"]]}


def aggregate(rows: list[dict], keys: dict, critic: bool) -> dict:
    n = len(rows)
    planted = sum(r["planted_lines"] for r in rows)
    claims = sum(r["claims"] for r in rows)
    err_reports = [r for r in rows if keys[r["report_id"]]["critic_should_fail_first_pass"]]
    return {
        "npt_hours_mae": round(sum(r["npt_abs_error"] for r in rows) / n, 3),
        "category_accuracy": round(sum(r["correct_lines"] for r in rows) / planted, 3),
        "critic_catch_rate": (round(sum(r.get("first_verdict") == "FAIL" for r in err_reports) / len(err_reports), 3)
                              if critic else None),
        "evidence_rate": round(sum(r["traceable_claims"] for r in rows) / claims, 3) if claims else 0.0,
        "logs_adding_to_24h": f"{sum(r['hours_ok'] for r in rows)}/{n}",
        "false_npt_lines": sum(r["false_npt_lines"] for r in rows),
        "reports_exact_npt": f"{sum(r['npt_abs_error'] < 0.01 for r in rows)}/{n}",
    }


def main():
    OUT.mkdir(exist_ok=True)
    eval_db = ROOT / "runtime" / "eval_memory.sqlite"
    eval_db.parent.mkdir(exist_ok=True)
    shutil.copy(DATA / "incidents.sqlite", eval_db)
    memory.set_db(eval_db)
    keys = json.loads((DATA / "answer_key.json").read_text())
    backend = llm.backend()
    print(f"model backend: {backend} ({llm.model_name()})")

    runs, per_report = {}, []
    for name, use_critic in [("Graph with Critic", True), ("Graph without Critic", False)]:
        app = build(MemorySaver(), use_critic=use_critic)
        rows, t0 = [], time.time()
        for rid, key in keys.items():
            state = run_until_review(app, rid, f"eval-{use_critic}-{rid}")
            p = from_graph(state)
            r = {"run": name, "report_id": rid, **score(p, key, state["report_text"]),
                 "pred_npt": p["total_npt"], "true_npt": key["total_npt"], "pred_hours": p["total_hours"],
                 "first_verdict": p["first_verdict"], "retries": p["retries"], "low_conf_flags": p["warnings"]}
            rows.append(r)
        runs[name] = aggregate(rows, keys, use_critic) | {"seconds": round(time.time() - t0, 1)}
        per_report += rows

    name = "Single-prompt chatbot"
    if backend == "ollama":
        # the chatbot's free-form answers vary from run to run, so run it several times and report the spread
        repeats = int(os.getenv("RIG_BASELINE_REPEATS", "3"))
        per_rep, t0 = [], time.time()
        for rep in range(1, repeats + 1):
            rows = []
            for rid, key in keys.items():
                text = (DATA / "reports" / f"{rid}.txt").read_text()
                p = from_baseline(llm.single_prompt_baseline(text), text)
                rows.append({"run": name, "repeat": rep, "report_id": rid, **score(p, key, text),
                             "pred_npt": p["total_npt"], "true_npt": key["total_npt"], "pred_hours": p["total_hours"]})
            per_rep.append(aggregate(rows, keys, False))
            per_report += rows
        maes = [r["npt_hours_mae"] for r in per_rep]
        runs[name] = {
            "npt_hours_mae": round(sum(maes) / repeats, 3),
            "npt_hours_mae_range": [min(maes), max(maes)],
            "category_accuracy": round(sum(r["category_accuracy"] for r in per_rep) / repeats, 3),
            "critic_catch_rate": None,
            "evidence_rate": round(sum(r["evidence_rate"] for r in per_rep) / repeats, 3),
            "logs_adding_to_24h": " / ".join(r["logs_adding_to_24h"] for r in per_rep),
            "reports_exact_npt": " / ".join(r["reports_exact_npt"] for r in per_rep),
            "false_npt_lines": round(sum(r["false_npt_lines"] for r in per_rep) / repeats, 1),
            "repeats": repeats, "per_repeat": per_rep, "seconds": round(time.time() - t0, 1),
            "note": f"mean of {repeats} runs; '/' separates the runs",
        }
    else:
        runs[name] = {"note": "needs a real LLM (Ollama or Groq); a keyword mock cannot stand in for a chatbot"}

    meta = {"backend": backend, "model": llm.model_name() if backend != "mock" else "mock keyword classifier",
            "reports": len(keys), "generated": time.strftime("%Y-%m-%d %H:%M")}
    (OUT / f"metrics_{backend}.json").write_text(json.dumps({"meta": meta, "runs": runs}, indent=2))
    with open(OUT / f"per_report_{backend}.csv", "w", newline="") as f:
        cols = sorted({k for r in per_report for k in r}, key=lambda c: (c not in ("run", "report_id"), c))
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(per_report)

    cols = ["npt_hours_mae", "category_accuracy", "critic_catch_rate", "evidence_rate",
            "logs_adding_to_24h", "reports_exact_npt", "false_npt_lines"]
    print("\n| Run | " + " | ".join(cols) + " |\n|" + "---|" * (len(cols) + 1))
    for name, m in runs.items():
        print(f"| {name} | " + " | ".join(str(m.get(c, "n/a")) if m.get(c) is not None else "n/a" for c in cols) + " |")
    memory.use_default()
    print(f"\nwrote {OUT / f'metrics_{backend}.json'}")


if __name__ == "__main__":
    main()
