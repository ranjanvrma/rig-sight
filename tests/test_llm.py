"""Model backends: Gemini key handling, batching, caching, retries, fallbacks and per-session choice.
The network is faked: no test needs a key or internet access."""
import io
import json
import threading
import urllib.error

import pytest

import graph
import llm
import tools

CODES = json.loads((tools.REF_DIR / "npt_codes.json").read_text())


def test_no_key_means_no_gemini():
    assert llm.gemini_key() is None
    assert llm.gemini_status()[0] is False
    assert llm.set_backend("gemini") == "mock"


def test_key_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "  test-key  ")
    assert llm.gemini_key() == "test-key"
    assert llm.set_backend("gemini") == "gemini"


def test_key_from_secrets_file(tmp_path, monkeypatch):
    f = tmp_path / "secrets.toml"
    f.write_text('GEMINI_API_KEY = "from-file"\n')
    monkeypatch.setattr(llm, "SECRETS", f)
    assert llm.gemini_key() == "from-file"


def test_auto_prefers_gemini_then_ollama_then_mock(monkeypatch):
    assert llm.set_backend("auto") == "mock"
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert llm.set_backend("auto") == "gemini"


def test_backend_choice_is_per_thread(monkeypatch):
    """Two Streamlit sessions run in different threads: one choosing Gemini must not switch the other."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    llm.set_backend("mock")
    seen = {}

    def other_session():
        llm.set_backend("gemini")
        seen["other"] = llm.backend()
    t = threading.Thread(target=other_session)
    t.start()
    t.join()
    assert seen["other"] == "gemini" and llm.backend() == "mock"


def fake_gemini(calls, answer=None):
    def _f(prompt, json_mode, attempts=4):
        calls.append(prompt)
        n = sum(1 for line in prompt.splitlines() if line[:1].isdigit() and ". " in line[:4])
        if answer is not None:
            return answer
        return json.dumps([{"line": i, "category": "tool_failure", "confidence": 0.9} for i in range(1, n + 1)])
    return _f


def test_gemini_labels_all_lines_in_one_request_and_caches(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    calls = []
    monkeypatch.setattr(llm, "_gemini", fake_gemini(calls))
    llm.set_backend("gemini")
    out = llm.classify_many(["MWD failed", "Mud motor failure", "Top drive failed"], CODES)
    assert len(calls) == 1
    assert out == [("tool_failure", 0.9, llm.GEMINI_MODEL)] * 3
    llm.classify_many(["MWD failed", "Mud motor failure", "Top drive failed", "New line"], CODES)
    assert len(calls) == 2 and "New line" in calls[1] and "MWD failed" not in calls[1]   # only the new line


def test_bad_gemini_answer_falls_back_to_mock_per_line(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    answer = json.dumps([{"line": 1, "category": "tool_failure", "confidence": 0.8},
                         {"line": 2, "category": "not_a_category", "confidence": 0.9}])
    monkeypatch.setattr(llm, "_gemini", fake_gemini([], answer))
    llm.set_backend("gemini")
    a, b = llm.classify_many(["MWD tool failed", "Waited on weather, wind"], CODES)
    assert a[2] == llm.GEMINI_MODEL and b == (*llm.mock_classify("Waited on weather, wind", CODES), "mock")


def test_gemini_failure_is_reported_as_mock(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    def down(*a, **k):
        raise RuntimeError("Gemini HTTP 403")
    monkeypatch.setattr(llm, "_gemini", down)
    llm.set_backend("gemini")
    assert {by for *_, by in llm.classify_many(["MWD failed", "Drilled ahead"], CODES)} == {"mock"}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_call_sends_key_in_header_and_retries_429(monkeypatch, real_gemini):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key-123")
    seen = []

    def urlopen(req, timeout=0):
        seen.append(req)
        if len(seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "busy", {"Retry-After": "1"}, None)
        body = {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}
        return FakeResp(json.dumps(body).encode())
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    assert real_gemini("hi", True) == '{"ok": true}'
    assert len(seen) == 2
    assert "secret-key-123" not in seen[0].full_url
    assert seen[0].get_header("X-goog-api-key") == "secret-key-123"
    assert json.loads(seen[0].data)["generationConfig"] == {"temperature": 0, "responseMimeType": "application/json"}


def test_http_error_message_does_not_leak_the_key(monkeypatch, real_gemini):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key-123")

    def urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError) as e:
        real_gemini("hi", False)
    assert "secret-key-123" not in str(e.value) and e.value.__cause__ is None


def test_graph_runs_on_gemini(monkeypatch, run):
    """W-02 Day 14 end to end with a fake Gemini that labels like the answer key."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    truth = {"POOH, MWD tool stopped sending data": "tool_failure",
             "Waited on weather, wind above crane limit": "waiting_on_weather",
             "Waited on tong repair before slip and cut": "waiting_on_equipment"}

    def labeller(prompt, json_mode, attempts=4):
        rows = [line.split(". ", 1) for line in prompt.splitlines() if line[:1].isdigit() and ". " in line[:4]]
        return json.dumps([{"line": int(n), "category": truth.get(t, "productive"), "confidence": 0.95}
                           for n, t in rows])
    monkeypatch.setattr(llm, "_gemini", labeller)
    llm.set_backend("gemini")
    s, _ = run("W-02_D14")
    assert s["npt_hours"] == {"tool_failure": 5.5, "waiting_on_weather": 2.0, "waiting_on_equipment": 0.5}
    assert {a["labelled_by"] for a in s["activities"]} == {llm.GEMINI_MODEL}
    assert f"labelled by: {llm.GEMINI_MODEL}" in s["draft_report"]
    assert graph.MAX_RETRIES == 3


def test_baseline_uses_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm, "_gemini", lambda p, j, attempts=4: json.dumps(
        {"total_hours": 24, "npt_hours": {"tool_failure": 5.5, "bogus": 1}, "npt_lines": [4]}))
    llm.set_backend("gemini")
    assert llm.single_prompt_baseline("WELL ...") == {"total_hours": 24.0, "npt_hours": {"tool_failure": 5.5},
                                                      "npt_lines": [4]}


def test_daily_quota_stops_gemini_fast(monkeypatch, real_gemini):
    """A 429 with a long retry delay (daily quota used up) must not hang: stop calling and fall back."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm, "_quota_until", 0.0)
    calls = []
    body = json.dumps({"error": {"details": [{"retryDelay": "46944s"}]}}).encode()

    def urlopen(req, timeout=0):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 429, "quota", {}, io.BytesIO(body))
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError):
        real_gemini("hi", True)
    assert len(calls) == 1                                   # no retries
    assert llm.gemini_status()[0] is False and "quota" in llm.gemini_status()[1]
    assert llm.set_backend("gemini") == "mock"
    with pytest.raises(RuntimeError):
        real_gemini("again", True)
    assert len(calls) == 1                                   # no further requests until the quota resets
