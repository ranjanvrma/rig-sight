"""The only place a language model is called. "The model reads, Python counts."

Backends:
  ollama - a free local model through Ollama (default model qwen2.5:3b, set RIG_MODEL)
  mock   - a keyword classifier built from npt_codes.json, so the demo never stalls

RIG_LLM=auto (default) uses Ollama when it is running and the model is pulled, else mock.
"""
import json
import os
import re
import time
import urllib.request

MODEL = os.getenv("RIG_MODEL", "qwen2.5:3b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
CATEGORIES = ["stuck_pipe", "lost_circulation", "tool_failure",
              "waiting_on_weather", "waiting_on_equipment", "well_control"]

# keywords that on their own are weak evidence (e.g. "waited on cement" is planned work)
GENERIC = {"waited on", "repair", "awaiting", "weather"}

_backend = None
_chat = None


_status_cache: dict = {}


def ollama_status() -> tuple[bool, str]:
    """Cached for 20 s so the UI does not probe Ollama on every rerun."""
    hit = _status_cache.get("v")
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    _status_cache["v"] = (time.time(), _probe())
    return _status_cache["v"][1]


def _probe() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=0.8) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return False, "Ollama is not running"
    if not any(n == MODEL or n.startswith(MODEL + ":") or n.split(":")[0] == MODEL for n in names):
        return False, f"Ollama is running but {MODEL} is not pulled (ollama pull {MODEL})"
    return True, f"Ollama · {MODEL}"


def backend() -> str:
    global _backend
    if _backend is None:
        mode = os.getenv("RIG_LLM", "auto")
        _backend = "ollama" if mode != "mock" and ollama_status()[0] else "mock"
    return _backend


def set_backend(name: str) -> str:
    """Switch backend at runtime (UI toggle). Falls back to mock if Ollama is unavailable."""
    global _backend
    _backend = "ollama" if name == "ollama" and ollama_status()[0] else "mock"
    return _backend


def _llm(json_mode: bool = False):
    global _chat
    from langchain_ollama import ChatOllama
    key = (MODEL, json_mode)
    if not isinstance(_chat, dict):
        _chat = {}
    if key not in _chat:
        _chat[key] = ChatOllama(model=MODEL, base_url=OLLAMA_URL, temperature=0,
                                format="json" if json_mode else None)
    return _chat[key]


# ---------------- classification ----------------

def mock_classify(text: str, codes: dict) -> tuple[str, float]:
    """Keyword scorer: the category whose matched keywords are longest wins."""
    t = text.lower()
    best, best_score, best_hits = "productive", 0, []
    for cat, words in codes.items():
        hits = [w for w in words if re.search(rf"\b{re.escape(w.lower())}\b", t)]
        score = sum(len(w) for w in hits)
        if score > best_score:
            best, best_score, best_hits = cat, score, hits
    if best == "productive":
        return "productive", 0.85
    if len(best_hits) == 1 and best_hits[0] in GENERIC:
        return best, 0.62          # weak evidence: the Critic will flag it for the engineer
    return best, min(0.95, 0.8 + 0.05 * (len(best_hits) - 1))


def classify(text: str, codes: dict) -> tuple[str, float, str]:
    """Returns (category, confidence, which model labelled it)."""
    if backend() == "ollama":
        prompt = (
            "You label lines from an oil and gas Daily Drilling Report.\n"
            "Non-productive time (NPT) means paid rig hours where the well does not progress "
            "because something went wrong. Planned work (drilling, tripping for a planned casing run, "
            "BOP tests, waiting on cement, rig service) is productive.\n"
            f"Categories: {CATEGORIES + ['productive']}\n"
            f"Keyword hints per category: {json.dumps(codes)}\n"
            'Reply only with JSON like {"category": "...", "confidence": 0.0}.\n'
            f"Line: {text}"
        )
        try:
            out = json.loads(_llm(json_mode=True).invoke(prompt).content)
            cat = str(out["category"]).strip()
            if cat in CATEGORIES + ["productive"]:
                return cat, max(0.0, min(1.0, float(out.get("confidence", 0.5)))), MODEL
        except Exception:
            pass
    cat, conf = mock_classify(text, codes)
    return cat, conf, "mock"


# ---------------- writing ----------------

def write_summary(facts: str) -> str | None:
    """LLM turns computed facts into a short paragraph. It may not add numbers. None in mock mode."""
    if backend() != "ollama":
        return None
    prompt = (
        "You are a drilling engineer's assistant. Write a 3 to 4 sentence morning summary of this "
        "Daily Drilling Report using ONLY the facts below. Do not invent or change any number. "
        "Plain text, no headings.\n\nFACTS:\n" + facts
    )
    try:
        return _llm().invoke(prompt).content.strip()
    except Exception:
        return None


def single_prompt_baseline(report_text: str) -> dict | None:
    """Plain chatbot baseline: one prompt, the model must read AND count. None without a model."""
    if backend() != "ollama":
        return None
    prompt = (
        "Read this Daily Drilling Report and list the non-productive time (NPT) hours by category. "
        f"Categories: {CATEGORIES}. Planned work is not NPT.\n"
        "Reply only with JSON with three keys: total_hours (the sum of all time-log durations), "
        "npt_hours (an object mapping each NPT category found to its hours, empty if none) and "
        "npt_lines (the 1-based line numbers of the NPT lines, counting the header as line 1).\n\n" + report_text
    )
    try:
        out = json.loads(_llm(json_mode=True).invoke(prompt).content)
        npt = {k: float(v) for k, v in (out.get("npt_hours") or {}).items() if k in CATEGORIES}
        return {"total_hours": float(out.get("total_hours", 0) or 0), "npt_hours": npt,
                "npt_lines": [int(x) for x in out.get("npt_lines", []) if str(x).isdigit()]}
    except Exception:
        return {"total_hours": 0.0, "npt_hours": {}, "npt_lines": []}
