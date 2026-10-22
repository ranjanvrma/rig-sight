"""The only place a language model is called. "The model reads, Python counts."

Backends:
  gemini - Google Gemini through its HTTPS API (free tier key from Google AI Studio; model set by RIG_GEMINI_MODEL)
  ollama - a free local model through Ollama (default qwen2.5:3b, set RIG_MODEL)
  mock   - a keyword classifier built from npt_codes.json, so the demo never stalls

RIG_LLM=auto (default) uses Gemini when an API key is configured, else Ollama when it is running and the model
is pulled, else the mock. The Gemini key is read from GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment, or from
.streamlit/secrets.toml. It is sent in a request header and never logged or shown.
"""
import contextvars
import json
import os
import re
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

MODEL = os.getenv("RIG_MODEL", "qwen2.5:3b")
GEMINI_MODEL = os.getenv("RIG_GEMINI_MODEL", "gemini-3.5-flash")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
SECRETS = Path(__file__).parent / ".streamlit" / "secrets.toml"
CATEGORIES = ["stuck_pipe", "lost_circulation", "tool_failure",
              "waiting_on_weather", "waiting_on_equipment", "well_control"]
BACKENDS = ("gemini", "ollama", "mock")

# keywords that on their own are weak evidence (e.g. "waited on cement" is planned work)
GENERIC = {"waited on", "repair", "awaiting", "weather"}

# per Streamlit session (each session runs in its own thread; LangGraph copies the context into tool threads)
_backend: contextvars.ContextVar[str | None] = contextvars.ContextVar("rig_backend", default=None)
_default_backend: str | None = None
_chat: dict = {}
_cache: dict = {}            # (model, line text) -> (category, confidence): retries do not re-ask the model
_status_cache: dict = {}
stats = {"gemini_requests": 0, "gemini_failures": 0}
_quota_until = 0.0         # set when Google says the quota is used up: stop calling until then


# ---------------- configuration ----------------

def gemini_key() -> str | None:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key and SECRETS.exists():
        try:
            data = tomllib.loads(SECRETS.read_text(encoding="utf-8"))
            key = data.get("GEMINI_API_KEY") or data.get("GOOGLE_API_KEY")
        except (OSError, tomllib.TOMLDecodeError):
            key = None
    return key.strip() if isinstance(key, str) and key.strip() else None


def gemini_status() -> tuple[bool, str]:
    if not gemini_key():
        return False, "No Gemini API key configured (set GEMINI_API_KEY)"
    if time.time() < _quota_until:
        hours = (_quota_until - time.time()) / 3600
        return False, f"Gemini free quota is used up (resets in about {hours:.0f} h)"
    return True, f"Gemini · {GEMINI_MODEL}"


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


def status(name: str) -> tuple[bool, str]:
    if name == "gemini":
        return gemini_status()
    if name == "ollama":
        return ollama_status()
    return True, "mock keyword classifier"


def _resolve(name: str) -> str:
    if name == "auto":
        for b in ("gemini", "ollama"):
            if status(b)[0]:
                return b
        return "mock"
    return name if name in BACKENDS and status(name)[0] else "mock"


def backend() -> str:
    global _default_backend
    b = _backend.get()
    if b:
        return b
    if _default_backend is None:
        _default_backend = _resolve(os.getenv("RIG_LLM", "auto"))
    return _default_backend


def set_backend(name: str) -> str:
    """Switch backend for this session / thread. Falls back to the mock if the choice is unavailable."""
    b = _resolve(name)
    _backend.set(b)
    return b


def model_name(b: str | None = None) -> str:
    b = b or backend()
    return {"gemini": GEMINI_MODEL, "ollama": MODEL}.get(b, "mock keyword classifier")


# ---------------- model calls ----------------

def _ollama(prompt: str, json_mode: bool) -> str:
    from langchain_ollama import ChatOllama
    key = (MODEL, json_mode)
    if key not in _chat:
        _chat[key] = ChatOllama(model=MODEL, base_url=OLLAMA_URL, temperature=0,
                                format="json" if json_mode else None)
    return _chat[key].invoke(prompt).content


def _gemini(prompt: str, json_mode: bool, attempts: int = 4) -> str:
    global _quota_until
    key = gemini_key()
    if not key:
        raise RuntimeError("no Gemini API key")
    if time.time() < _quota_until:
        raise RuntimeError("Gemini quota used up")
    cfg = {"temperature": 0}
    if json_mode:
        cfg["responseMimeType"] = "application/json"
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                       "generationConfig": cfg}).encode()
    for i in range(attempts):
        req = urllib.request.Request(GEMINI_URL.format(model=GEMINI_MODEL), data=body, method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        stats["gemini_requests"] += 1
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
            parts = out["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except urllib.error.HTTPError as e:
            wait = _retry_delay(e)
            if e.code == 429 and wait > 60:                        # daily quota used up: stop, do not hang
                _quota_until = time.time() + wait
                stats["gemini_failures"] += 1
                raise RuntimeError("Gemini quota used up") from None
            if e.code in (429, 500, 503) and i < attempts - 1:      # busy or briefly rate limited: back off, retry
                time.sleep(min(10, wait or 2 * (i + 1)))
                continue
            stats["gemini_failures"] += 1
            raise RuntimeError(f"Gemini HTTP {e.code}") from None   # never re-raise with the request (it holds the key)
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
            stats["gemini_failures"] += 1
            raise RuntimeError(f"Gemini call failed ({type(e).__name__})") from None
    raise RuntimeError("Gemini call failed")


def _retry_delay(e: urllib.error.HTTPError) -> float:
    """Seconds Google asks us to wait, from the Retry-After header or the error body's retryDelay."""
    try:
        if e.headers and e.headers.get("Retry-After"):
            return float(e.headers["Retry-After"])
        body = json.loads(e.read() or b"{}")
        for d in body.get("error", {}).get("details", []):
            if "retryDelay" in d:
                return float(str(d["retryDelay"]).rstrip("s"))
    except Exception:
        pass
    return 0.0


def complete(prompt: str, json_mode: bool = False, b: str | None = None) -> str:
    b = b or backend()
    if b == "gemini":
        return _gemini(prompt, json_mode)
    if b == "ollama":
        return _ollama(prompt, json_mode)
    raise RuntimeError("the mock has no language model")


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


INSTRUCTIONS = (
    "You label lines from an oil and gas Daily Drilling Report.\n"
    "Non-productive time (NPT) means paid rig hours where the well does not progress "
    "because something went wrong. Planned work (drilling, tripping for a planned casing run, "
    "BOP tests, waiting on cement, rig service) is productive.\n"
    f"Categories: {CATEGORIES + ['productive']}\n"
)


def _valid(out: dict) -> tuple[str, float] | None:
    cat = str(out.get("category", "")).strip()
    if cat not in CATEGORIES + ["productive"]:
        return None
    try:
        conf = float(out.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return cat, max(0.0, min(1.0, conf))


def classify(text: str, codes: dict) -> tuple[str, float, str]:
    """One line. Returns (category, confidence, which model labelled it)."""
    return classify_many([text], codes)[0]


def classify_many(texts: list[str], codes: dict) -> list[tuple[str, float, str]]:
    """Label several lines. Ollama gets one request per line; Gemini gets all uncached lines in one request,
    to stay within free-tier rate limits. Any line the model cannot label falls back to the mock, and is
    reported as labelled by the mock."""
    b = backend()
    results: dict[int, tuple[str, float, str]] = {}
    if b in ("gemini", "ollama"):
        model = model_name(b)
        todo = []
        for i, t in enumerate(texts):
            hit = _cache.get((model, t))
            if hit:
                results[i] = (*hit, model)
            else:
                todo.append(i)
        if todo and b == "ollama":
            for i in todo:
                prompt = (INSTRUCTIONS + f"Keyword hints per category: {json.dumps(codes)}\n"
                          'Reply only with JSON like {"category": "...", "confidence": 0.0}.\n'
                          f"Line: {texts[i]}")
                try:
                    got = _valid(json.loads(_ollama(prompt, True)))
                except Exception:
                    got = None
                if got:
                    _cache[(model, texts[i])] = got
                    results[i] = (*got, model)
        elif todo:
            lines = "\n".join(f"{n}. {texts[i]}" for n, i in enumerate(todo, 1))
            prompt = (INSTRUCTIONS + f"Keyword hints per category: {json.dumps(codes)}\n"
                      "Label every numbered line below. Reply only with a JSON list with one object per line, "
                      'in order, like [{"line": 1, "category": "...", "confidence": 0.0}].\n\n' + lines)
            try:
                out = json.loads(_gemini(prompt, True))
                by_no = {int(o.get("line", n)): o for n, o in enumerate(out, 1) if isinstance(o, dict)}
                for n, i in enumerate(todo, 1):
                    got = _valid(by_no.get(n, {}))
                    if got:
                        _cache[(model, texts[i])] = got
                        results[i] = (*got, model)
            except Exception:
                pass
    for i, t in enumerate(texts):
        if i not in results:
            results[i] = (*mock_classify(t, codes), "mock")
    return [results[i] for i in range(len(texts))]


# ---------------- writing ----------------

def write_summary(facts: str) -> str | None:
    """LLM turns computed facts into a short paragraph. It may not add numbers. None in mock mode.

    Off by default (RIG_LLM_SUMMARY=1 turns it on): in testing, qwen2.5:3b kept every number but swapped
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
        return complete(prompt).strip()
    except Exception:
        return None


def single_prompt_baseline(report_text: str) -> dict | None:
    """Plain chatbot baseline: one prompt, the model must read AND count. None without a model."""
    if backend() == "mock":
        return None
    prompt = (
        "Read this Daily Drilling Report and list the non-productive time (NPT) hours by category. "
        f"Categories: {CATEGORIES}. Planned work is not NPT.\n"
        "Reply only with JSON with three keys: total_hours (the sum of all time-log durations), "
        "npt_hours (an object mapping each NPT category found to its hours, empty if none) and "
        "npt_lines (the 1-based line numbers of the NPT lines, counting the header as line 1).\n\n" + report_text
    )
    try:
        out = json.loads(complete(prompt, json_mode=True))
        npt = {k: float(v) for k, v in (out.get("npt_hours") or {}).items() if k in CATEGORIES}
        return {"total_hours": float(out.get("total_hours", 0) or 0), "npt_hours": npt,
                "npt_lines": [int(x) for x in out.get("npt_lines", []) if str(x).isdigit()]}
    except Exception:
        return {"total_hours": 0.0, "npt_hours": {}, "npt_lines": []}
