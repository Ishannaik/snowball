#!/usr/bin/env python3
"""Local adapter between llmwiki and the qwen proxy (127.0.0.1:8812 -> 127.0.0.1:8800).

llmwiki sends max_tokens=4096. Ornith and the 27B are reasoning models: on a 12-17 KB source
their reasoning alone used ~4K tokens, so the reply hit finish=length before the tool call
("no concepts - will retry"). Measured 2 Oct 2026. This adapter raises max_tokens to MIN_TOKENS
and forwards everything else unchanged. run.sh starts it for the compile and stops it after.
"""
import json
import sys

from aiohttp import ClientSession, ClientTimeout, web

UPSTREAM = "http://127.0.0.1:8800"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8812
MIN_TOKENS = 16384


async def forward(req: web.Request) -> web.StreamResponse:
    body = await req.read()
    try:
        d = json.loads(body)
        for k in ("max_tokens", "max_completion_tokens"):
            if k in d and isinstance(d[k], int) and d[k] < MIN_TOKENS:
                d[k] = MIN_TOKENS
        body = json.dumps(d).encode()
    except ValueError:
        pass
    headers = {"Authorization": req.headers.get("Authorization", ""), "Content-Type": "application/json"}
    async with req.app["http"].post(UPSTREAM + req.path, data=body, headers=headers) as up:
        resp = web.StreamResponse(status=up.status, headers={"Content-Type": up.headers.get("Content-Type", "application/json")})
        await resp.prepare(req)
        async for chunk in up.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp


async def start(app):
    app["http"] = ClientSession(timeout=ClientTimeout(total=1800))


async def stop(app):
    await app["http"].close()


app = web.Application(client_max_size=64 * 1024 * 1024)
app.router.add_post("/{tail:.*}", forward)
app.on_startup.append(start)
app.on_cleanup.append(stop)
web.run_app(app, host="127.0.0.1", port=PORT, print=None, access_log=None)
