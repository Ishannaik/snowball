#!/usr/bin/env python3
"""OpenAI-compatible shim for Gemma 4 31B on Google AI Studio, rotating cross-account keys.

Listens on 127.0.0.1:8810 only. Forwards /v1/chat/completions (streaming or not) to
Gemini's OpenAI-compatible endpoint. Key handling mirrors PixaRadar's scoring.py:
per-(model, key) cooldown, 5 min after a 429, 1 h after a 503, rotate through the pool.
Extra limits so we never fight PixaRadar for quota:
  - at most PER_KEY_RPM requests per key per minute (default 2),
  - a daily request cap across all keys (DAILY_CAP, default 2000), state in usage.json.
Keys come from GEMINI_API_KEYS in /home/ubuntu/pixaradar/.env and are never logged.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/openai"
MODEL = "gemma-4-31b-it"
ENV_FILE = Path("/home/ubuntu/pixaradar/.env")
STATE_DIR = Path.home() / ".config/gemma-shim"
USAGE = STATE_DIR / "usage.json"
PORT = int(os.environ.get("GEMMA_SHIM_PORT", "8810"))
PER_KEY_RPM = int(os.environ.get("GEMMA_SHIM_PER_KEY_RPM", "2"))
DAILY_CAP = int(os.environ.get("GEMMA_SHIM_DAILY_CAP", "2000"))
COOLDOWN_429 = 300
# Measured 2 Oct 2026: the AI Studio free tier caps Gemma 4 31B input at 16,000 tokens per
# minute per key (metric generate_content_free_tier_input_token_count). A request above that
# 429s on every key, so it is refused here without touching any key.
INPUT_TPM = 16000
tokens_recent: dict[int, deque] = {}  # key index -> (time, est. input tokens)
COOLDOWN_503 = 3600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gemma-shim")

cooldowns: dict[tuple[str, int], float] = {}  # (model, key index) -> resume time
recent: dict[int, deque] = {}  # key index -> request timestamps in the last 60 s
_rr = 0


def load_keys() -> list[str]:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("GEMINI_API_KEYS="):
            raw = line.split("=", 1)[1].strip().strip("'\"")
            return [k.strip() for k in raw.split(",") if k.strip()]
    return []


def today() -> str:
    return time.strftime("%Y-%m-%d")


def usage_get() -> dict:
    try:
        d = json.loads(USAGE.read_text())
    except (OSError, ValueError):
        d = {}
    if d.get("day") != today():
        d = {"day": today(), "requests": 0, "ok": 0, "r429": 0, "r503": 0, "err": 0}
    return d


def usage_bump(field: str) -> None:
    d = usage_get()
    d[field] = d.get(field, 0) + 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USAGE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(USAGE)


def est_tokens(payload: bytes) -> int:
    return len(payload) // 4  # about 4 bytes per token for English chat JSON


def tpm_used(i: int, now: float) -> int:
    q = tokens_recent.setdefault(i, deque())
    while q and now - q[0][0] > 60:
        q.popleft()
    return sum(t for _, t in q)


def pick_order(n: int, need: int = 0) -> list[int]:
    """Key indexes to try, round-robin, skipping cooled and per-minute-full keys."""
    global _rr
    now = time.time()
    start = _rr % n
    _rr += 1
    out = []
    for off in range(n):
        i = (start + off) % n
        if cooldowns.get((MODEL, i), 0) > now:
            continue
        q = recent.setdefault(i, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= PER_KEY_RPM or tpm_used(i, now) + need > INPUT_TPM:
            continue
        out.append(i)
    return out


THOUGHT = re.compile(r"<thought>.*?</thought>", re.S)


async def as_sse(req: web.Request, data: dict) -> web.StreamResponse:
    """Re-emit a whole chat.completion as an OpenAI chat.completion.chunk stream."""
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(req)
    base = {
        "id": data.get("id", "gemma"),
        "object": "chat.completion.chunk",
        "created": data.get("created", int(time.time())),
        "model": MODEL,
    }
    for ch in data.get("choices", []):
        msg = ch.get("message") or {}
        delta = {"role": "assistant"}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            calls = []
            for n, tc in enumerate(msg["tool_calls"]):
                tc = {k: v for k, v in tc.items() if k != "extra_content"}
                tc["index"] = n
                calls.append(tc)
            delta["tool_calls"] = calls
        chunk = dict(base, choices=[{"index": ch.get("index", 0), "delta": delta, "finish_reason": None}])
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        fin = ch.get("finish_reason") or ("tool_calls" if msg.get("tool_calls") else "stop")
        chunk = dict(base, choices=[{"index": ch.get("index", 0), "delta": {}, "finish_reason": fin}])
        if data.get("usage"):
            chunk["usage"] = data["usage"]
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


def err(status: int, msg: str) -> web.Response:
    return web.json_response({"error": {"message": msg, "type": "gemma_shim"}}, status=status)


async def chat(req: web.Request) -> web.StreamResponse:
    keys = req.app["keys"]
    if not keys:
        return err(503, "no GEMINI_API_KEYS configured")
    if usage_get()["requests"] >= DAILY_CAP:
        return err(429, f"gemma-shim daily cap {DAILY_CAP} reached")
    try:
        body = await req.json()
    except ValueError:
        return err(400, "body is not JSON")
    body["model"] = MODEL  # this shim serves one model only
    for k in ("store", "service_tier", "parallel_tool_calls", "stream_options", "reasoning_effort"):
        body.pop(k, None)
    stream = bool(body.get("stream"))
    # Gemini's OpenAI-compatible stream can end without a finish_reason or switch to an error
    # body mid-stream. Ask upstream for a whole response instead (errors then arrive as a status
    # we can retry on the next key) and re-emit it to the client as a short SSE stream.
    body["stream"] = False
    payload = json.dumps(body).encode()
    need = est_tokens(payload)
    if need > INPUT_TPM - 500:
        log.info("refused: ~%d input tokens, over the %d/min free-tier cap", need, INPUT_TPM)
        return err(413, f"request is ~{need} input tokens; Gemma free tier allows {INPUT_TPM} per minute per key")
    last = (503, "every key is cooling down or at its per-minute limit")
    for _attempt in range(3):  # wait briefly for a free key slot before giving up
        order = pick_order(len(keys), need)
        if order:
            break
        await asyncio.sleep(20)
    fails = 0
    for i in order:
        recent.setdefault(i, deque()).append(time.time())
        tokens_recent.setdefault(i, deque()).append((time.time(), need))
        usage_bump("requests")
        headers = {"Authorization": f"Bearer {keys[i]}", "Content-Type": "application/json"}
        try:
            up = await req.app["http"].post(f"{UPSTREAM}/chat/completions", data=payload, headers=headers)
        except Exception as e:  # noqa: BLE001 - transport errors: try the next key once
            usage_bump("err")
            log.info("key#%d transport error: %s", i, type(e).__name__)
            last = (502, f"upstream transport error {type(e).__name__}")
            continue
        if up.status in (429, 503):
            usage_bump("r429" if up.status == 429 else "r503")
            text = await up.text()
            metric = re.findall(r"Quota exceeded for metric: ([^,\\]+)[^0-9]*limit: (\d+)", text)
            tpm_only = metric and all("input_token" in m for m, _ in metric)
            cool = 65 if tpm_only else (COOLDOWN_429 if up.status == 429 else COOLDOWN_503)
            cooldowns[(MODEL, i)] = time.time() + cool
            log.info("key#%d -> %d, cooling %ds %s", i, up.status, cool, metric[:2])
            last = (up.status, text[:300])
            up.release()
            continue
        if up.status in (500, 502, 504) and fails < 2:  # transient Gemini error: next key, short pause
            fails += 1
            cooldowns[(MODEL, i)] = time.time() + 60
            usage_bump("err")
            log.info("key#%d -> %d, trying next key", i, up.status)
            last = (up.status, (await up.text())[:300])
            up.release()
            continue
        if up.status != 200:
            text = await up.text()
            usage_bump("err")
            log.info("key#%d -> %d %s", i, up.status, text[:200].replace(keys[i], "<key>"))
            return web.Response(status=up.status, text=text, content_type="application/json")
        usage_bump("ok")
        log.info("key#%d -> 200 stream=%s", i, stream)
        data = await up.json(content_type=None)
        for ch in data.get("choices", []):
            msg = ch.get("message") or {}
            if isinstance(msg.get("content"), str):  # drop Gemma's visible <thought> block
                msg["content"] = THOUGHT.sub("", msg["content"]).strip()
            msg.pop("extra_content", None)
        if not stream:
            return web.json_response(data)
        return await as_sse(req, data)
    return err(last[0], f"gemma-shim: {last[1]}")


async def models(_req: web.Request) -> web.Response:
    return web.json_response({"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "google"}]})


async def health(req: web.Request) -> web.Response:
    u = usage_get()
    now = time.time()
    cooling = sum(1 for (m, _i), t in cooldowns.items() if t > now)
    return web.json_response({"keys": len(req.app["keys"]), "cooling": cooling, "daily_cap": DAILY_CAP, **u})


async def on_start(app: web.Application) -> None:
    app["http"] = ClientSession(timeout=ClientTimeout(total=600, sock_read=300))


async def on_stop(app: web.Application) -> None:
    await app["http"].close()


def main() -> None:
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app["keys"] = load_keys()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/health", health)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    log.info(
        "gemma-shim on 127.0.0.1:%d, %d keys, %d rpm/key, daily cap %d", PORT, len(app["keys"]), PER_KEY_RPM, DAILY_CAP
    )
    web.run_app(app, host="127.0.0.1", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
