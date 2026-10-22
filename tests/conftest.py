"""Tests run on the mock classifier with a throwaway memory database, so they are fast,
deterministic and never touch the committed seed data or the Groq API."""
import os
import shutil
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["RIG_LLM"] = "mock"
os.environ["GROQ_API_KEY"] = ""                      # tests never call Groq, even with a local .env
warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import llm
    import memory
    import tools
    llm.set_backend("mock")
    db = tmp_path / "memory.sqlite"
    shutil.copy(ROOT / "data" / "incidents.sqlite", db)
    memory.set_db(db)
    monkeypatch.setattr(tools, "REF_DIR", ROOT / "data")
    yield
    memory.use_default()


@pytest.fixture
def app():
    from langgraph.checkpoint.memory import MemorySaver
    import graph
    return graph.build(MemorySaver())


@pytest.fixture
def run(app):
    """run(report_id or text) -> state paused at the engineer, plus the config to resume it."""
    import graph
    counter = {"n": 0}

    def _run(report_id="CUSTOM", text=None, application=None):
        counter["n"] += 1
        tid = f"t{counter['n']}-{report_id}"
        state = graph.run_until_review(application or app, report_id, tid, report_text=text)
        return state, {"configurable": {"thread_id": tid}, "recursion_limit": graph.RECURSION_LIMIT}
    return _run
