"""The only place a language model is called. "The model reads, Python counts."

Backends:
  groq - a free hosted model through the Groq API (needs GROQ_API_KEY; default openai/gpt-oss-20b,
         set RIG_GROQ_MODEL). Free tier, no card, and it runs on a free web server.
  mock - a keyword classifier built from npt_codes.json, so the demo never stalls

RIG_LLM=auto (default) uses Groq when a key is set and Groq answers, else mock.
For local use, put GROQ_API_KEY=... in a .env file next to this one (git-ignored).
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

_env = Path(__file__).with_name(".env")
if _env.exists():                        # tiny .env reader, so no extra package is needed
    for _line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            k, v = _line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

GROQ_MODEL = os.getenv("RIG_GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_URL = "https://api.groq.com/openai/v1"
GROQ_WAIT = float(os.getenv("RIG_GROQ_WAIT", "6"))   # seconds to wait out a rate limit before falling back
CATEGORIES = ["stuck_pipe", "lost_circulation", "tool_failure",
              "waiting_on_weather", "waiting_on_equipment", "well_control"]

# one NPT definition for our classifier and for the chatbot baseline, so the comparison is fair
NPT_RULE = ("Non-productive time (NPT) means paid rig hours where the well does not progress because something went "
            "wrong. Time spent recovering from a failure (pulling out of hole, repairing or changing equipment, running "
            "back in) is NPT in that failure's category. Planned work (drilling, tripping for a planned casing run, BOP "
            "tests, waiting on cement, rig service) is productive.")

# keywords that on their own are weak evidence (e.g. "waited on cement" is planned work)
GENERIC = {"waited on", "repair", "awaiting", "weather"}

_backend = None
_status_cache: dict = {}


def _headers() -> dict:
    # Groq sits behind Cloudflare, which blocks Python's default User-Agent (error 1010)
    return {"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '')}", "Content-Type": "application/json",
            "User-Agent": "rig-sight/1.0"}


def groq_status() -> tuple[bool, str]:
    """Checked once every 5 minutes: is a key set, and does Groq serve the model?"""
    if not os.getenv("GROQ_API_KEY"):
        return False, "Groq needs GROQ_API_KEY on the server"
    hit = _status_cache.get("groq")
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{GROQ_URL}/models", headers=_headers()), timeout=8) as r:
            ids = {m["id"] for m in json.load(r).get("data", [])}
        res = (True, f"Groq · {GROQ_MODEL}") if GROQ_MODEL in ids else (False, f"Groq does not serve {GROQ_MODEL}")
    except urllib.error.HTTPError as e:
        res = (False, "Groq rejected the API key" if e.code == 401 else f"Groq answered HTTP {e.code}")
    except Exception:
        res = (False, "Groq is not reachable")
    _status_cache["groq"] = (time.time(), res)
    return res


def backend() -> str:
    global _backend
    if _backend is None:
        _backend = "groq" if os.getenv("RIG_LLM", "auto") != "mock" and groq_status()[0] else "mock"
    return _backend


def set_backend(name: str) -> str:
    """Switch backend at runtime (UI toggle). Falls back to mock if Groq is unavailable."""
    global _backend
    _backend = "groq" if name == "groq" and groq_status()[0] else "mock"
    return _backend


def model_name() -> str:
    """Who labels the lines, as shown in every report."""
    return f"{GROQ_MODEL} (Groq)" if backend() == "groq" else "mock"


def ask(prompt: str, json_mode: bool = False) -> str:
    """One prompt to Groq. Waits out a rate limit up to GROQ_WAIT seconds, then raises; callers fall back."""
    body = {"model": GROQ_MODEL, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(f"{GROQ_URL}/chat/completions", data=json.dumps(body).encode(), headers=_headers())
    waited = 0.0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            pause = float(e.headers.get("retry-after") or 2)
            if e.code != 429 or waited + pause > GROQ_WAIT:
                raise
            time.sleep(pause)
            waited += pause


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


def classify_many(texts: list[str], codes: dict) -> list[tuple[str, float, str]]:
    """Label every line of one report in a single model call. Returns (category, confidence, labelled by)
    per line. Any line the model leaves out or labels wrongly, or every line if the call fails, uses the mock."""
    labels = {}
    if backend() == "groq" and texts:
        lines = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))
        prompt = (
            "You label lines from an oil and gas Daily Drilling Report.\n" + NPT_RULE + "\n"
            f"Categories: {CATEGORIES + ['productive']}\n"
            f"Keyword hints per category: {json.dumps(codes)}\n"
            'Reply only with JSON like {"labels": [{"id": 1, "category": "...", "confidence": 0.0}]}, '
            "one entry per numbered line.\n\n" + lines
        )
        try:
            for x in json.loads(ask(prompt, json_mode=True)).get("labels", []):
                cat = str(x.get("category", "")).strip()
                if cat in CATEGORIES + ["productive"]:
                    labels[int(x["id"])] = (cat, max(0.0, min(1.0, float(x.get("confidence", 0.5)))), model_name())
        except Exception:
            pass
    return [labels.get(i) or (*mock_classify(t, codes), "mock") for i, t in enumerate(texts, 1)]


def classify(text: str, codes: dict) -> tuple[str, float, str]:
    """One line: (category, confidence, which model labelled it)."""
    return classify_many([text], codes)[0]


# ---------------- writing ----------------

def write_summary(facts: str) -> str | None:
    """LLM turns computed facts into a short paragraph. It may not add numbers. None in mock mode.

    Off by default (RIG_LLM_SUMMARY=1 turns it on): in testing, a small model (qwen2.5:3b) kept every number but swapped
    which category they belonged to ("weather and equipment (3.5 h, 2 h respectively)"). The number guard
    in report_writer cannot catch that, so the template summary is the safe default."""
    if backend() == "mock" or os.getenv("RIG_LLM_SUMMARY") != "1":
        return None
    prompt = (
        "You are a drilling engineer's assistant. Write a 3 to 4 sentence morning summary of this "
        "Daily Drilling Report using ONLY the facts below. Do not invent or change any number. "
        "Plain text, no headings.\n\nFACTS:\n" + facts
    )
    try:
        return ask(prompt).strip()
    except Exception:
        return None


def single_prompt_baseline(report_text: str) -> dict | None:
    """Plain chatbot baseline: one prompt, the model must read AND count. None without a model."""
    if backend() == "mock":
        return None
    prompt = (
        "Read this Daily Drilling Report and list the non-productive time (NPT) hours by category. "
        + NPT_RULE + f" Categories: {CATEGORIES}.\n"
        "Reply only with JSON with three keys: total_hours (the sum of all time-log durations), "
        "npt_hours (an object mapping each NPT category found to its hours, empty if none) and "
        "npt_lines (the 1-based line numbers of the NPT lines, counting the header as line 1).\n\n" + report_text
    )
    try:
        out = json.loads(ask(prompt, json_mode=True))
        npt = {k: float(v) for k, v in (out.get("npt_hours") or {}).items() if k in CATEGORIES}
        return {"total_hours": float(out.get("total_hours", 0) or 0), "npt_hours": npt,
                "npt_lines": [int(x) for x in out.get("npt_lines", []) if str(x).isdigit()]}
    except Exception:
        return {"total_hours": 0.0, "npt_hours": {}, "npt_lines": []}
