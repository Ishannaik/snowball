"""Tool-less chat calls for the research loop.

The model gets text in and returns text out. It has no tools, so text it reads (forum posts,
pages) cannot make anything execute. Calls go straight to the OpenAI-compatible endpoints:
  qwen proxy 127.0.0.1:8800 (research-loop key from the repo .env) for Ornith and the 27B,
  gemma shim 127.0.0.1:8810 for Gemma (public topics only, small prompts only).
Every Ornith call holds ornith.lock, so one research-loop request is in flight to its T4.
Calls stream, so a long generation keeps bytes moving through the Cloudflare tunnel (a quiet
non-streamed request got a 524 after ~2 min on 2 Oct 2026).
"""

import fcntl
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "ornith.lock"
ORNITH = "ornith-1.5-35b-a3b-abliterated"
Q27 = "qwen3.8-27b-abliterated"  # never qwen-best: that name may wake a paid GPU
GEMMA = "gemma-4-31b-it"
GEMMA_MAX_PROMPT_CHARS = 40000  # the free tier allows 16K input tokens per minute per key


class LLMError(RuntimeError):
    pass


def _key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("QWEN_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise LLMError("no QWEN_API_KEY in .env")


def _stream(url: str, headers: dict, body: dict, timeout: int) -> str:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    out, finish = [], None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                if "error" in ev:
                    raise LLMError(f"stream error: {str(ev['error'])[:200]}")
                for ch in ev.get("choices", []):
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        out.append(piece)
                    finish = ch.get("finish_reason") or finish
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise LLMError(f"transport: {type(e).__name__}") from e
    text = "".join(out).strip()
    if not text:
        raise LLMError(f"empty reply (finish={finish})")
    return text


def chat(model: str, system: str, user: str, max_tokens: int = 16000, timeout: int = 1800) -> str:
    """One tool-less completion. Raises LLMError on any failure."""
    body = {"model": model, "stream": True, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if model == GEMMA:
        if len(system) + len(user) > GEMMA_MAX_PROMPT_CHARS:
            raise LLMError("prompt too large for the Gemma free tier")
        return _stream("http://127.0.0.1:8810/v1/chat/completions",
                       {"Content-Type": "application/json"}, body, timeout)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {_key()}"}
    url = "http://127.0.0.1:8800/v1/chat/completions"
    if model == ORNITH:
        with open(LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)  # one in flight to Ornith's single slot
            return _stream(url, headers, body, timeout)
    return _stream(url, headers, body, timeout)


def chat_chain(models: list, system: str, user: str, log=print, **kw) -> tuple:
    """Try models in order. Returns (text, model)."""
    last = None
    for m in models:
        t = time.time()
        try:
            text = chat(m, system, user, **kw)
            log(f"llm {m} ok in {time.time() - t:.0f}s, {len(text)} chars")
            return text, m
        except LLMError as e:
            last = e
            log(f"llm {m} failed: {e}")
    raise LLMError(f"all models failed: {last}")
