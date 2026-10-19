"""The synthetic dataset is internally consistent and regenerates exactly from generate_data.py."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEYS = json.loads((ROOT / "data" / "answer_key.json").read_text())


def test_dataset_shape():
    assert len(KEYS) == 15
    assert sum(not k["npt_hours"] for k in KEYS.values()) == 2                 # clean days
    assert sum(k["critic_should_fail_first_pass"] for k in KEYS.values()) == 2  # planted errors
    cats = {c for k in KEYS.values() for c in k["npt_hours"]}
    assert cats == {"stuck_pipe", "lost_circulation", "tool_failure", "waiting_on_weather",
                    "waiting_on_equipment", "well_control"}


def test_answer_key_npt_matches_line_labels():
    for rid, k in KEYS.items():
        assert sorted(int(n) for n, c in k["line_labels"].items() if c != "productive") == sorted(k["npt_lines"])
        assert round(sum(k["npt_hours"].values()), 2) == k["total_npt"]


def test_generator_is_reproducible(tmp_path):
    """Regenerate into a temp copy and compare the text outputs byte for byte."""
    import shutil
    work = tmp_path / "data"
    shutil.copytree(ROOT / "data", work)
    subprocess.run([sys.executable, str(work / "generate_data.py")], check=True, capture_output=True)
    for f in ["answer_key.json", "npt_codes.json", "well_plan.csv", "cost_budget.csv"]:
        assert (work / f).read_bytes() == (ROOT / "data" / f).read_bytes(), f
    for f in (ROOT / "data" / "reports").glob("*.txt"):
        assert (work / "reports" / f.name).read_bytes() == f.read_bytes(), f.name
