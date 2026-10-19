"""The Streamlit app loads, runs a report, pauses for the engineer and approves (headless AppTest)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)


def click(at, label):
    [b for b in at.button if label in b.label][0].click().run()


def test_app_runs_and_approves():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    assert not at.exception
    at.selectbox[0].select("W-02_D14").run()
    click(at, "Run agents")
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["NPT"] == "8 h" and metrics["Hours logged"] == "24 h" and metrics["Critic"] == "PASS"
    click(at, "Approve")
    assert not at.exception
    assert any("Approved" in m.value for m in at.markdown)


def test_app_custom_report_shows_unresolved_error():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    at.selectbox[0].select("CUSTOM").run()
    click(at, "Run agents")
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Critic"] == "FAIL"
    assert any("not used · source error" in m.value for m in at.markdown)


def test_other_tabs_render():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Local LLM through Ollama" in text and "Past NPT incidents" in text
    assert not at.get("help_info")        # regression: no stray Streamlit "magic" output in the Memory tab
