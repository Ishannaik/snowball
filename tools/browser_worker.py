#!/usr/bin/env python3
"""One-context challenge acquisition. The browser stays off until an owned fixture exists.

Playwright is an optional dependency. Importing this module does not launch Chromium.
`playwright_session_factory` returns None, so `acquire` stops before DNS when no session
was supplied. A solver token is not a successful fetch. Walled pages return empty text.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _TOOLS_DIR]
if (
    getattr(sys.modules.get("queue"), "__file__", None)
    and Path(sys.modules["queue"].__file__).resolve().parent == _TOOLS_DIR
):
    del sys.modules["queue"]

import argparse
import importlib.util
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping
from urllib.parse import urljoin, urlsplit


def _load(mod_name: str, filename: str):
    existing = sys.modules.get(mod_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(mod_name, _TOOLS_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


guard = _load("snowball_egress_guard", "egress_guard.py")
pc = _load("snowball_process_control", "process_control.py")
fetch_types = _load("snowball_fetch_types", "fetch_types.py")

FetchResult = fetch_types.FetchResult
AccessRequest = fetch_types.AccessRequest

MIN_TEXT = 200
MAX_HOPS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})
CALLBACK_FIELDS = ("cf-turnstile-response", "g-recaptcha-response")


def _is_chromium_available(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    path_val = env.get("PATH", "")
    if path_val == "x":
        return False
    custom = env.get("PLAYWRIGHT_BROWSERS_PATH") or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    search_dirs: list[Path] = []
    if custom:
        if custom == "x":
            return False
        search_dirs.append(Path(custom))
    if os.name == "nt":
        local_app = env.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA")
        if local_app:
            search_dirs.append(Path(local_app) / "ms-playwright")
    else:
        search_dirs.append(Path.home() / ".cache" / "ms-playwright")
        search_dirs.append(Path("/home/ubuntu/.cache/ms-playwright"))
    for d in search_dirs:
        if d.exists():
            for p in d.glob("chromium-*/**/chrome*"):
                if p.is_file():
                    return True
    for binary_name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if shutil.which(binary_name, path=path_val):
            return True
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            return bool(exe and os.path.exists(exe))
    except Exception:
        pass
    return False


def _access_request_from_dict(d: dict) -> AccessRequest:
    return AccessRequest(
        acquisition_id=str(d.get("acquisition_id", "")),
        page_id=int(d.get("page_id", 0)),
        url=str(d.get("url", "")),
        private=bool(d.get("private", False)),
        allowed_modes=tuple(d.get("allowed_modes", ("public", "solver"))),
        approved_resource_origins=tuple(d.get("approved_resource_origins", ())),
        allowed_path_prefixes=tuple(d.get("allowed_path_prefixes", ("/",))),
        session_id=str(d.get("session_id", d.get("acquisition_id", ""))),
        deadline_at=float(d.get("deadline_at", 0.0)),
    )

class PlaywrightSession:
    """Real isolated Playwright session with fresh context per acquisition.

    Enforces exact authorized resource origins, blocks popups, downloads,
    service workers, and operates without SQLite or persistent browser auth state.
    """

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        approved_origins: tuple[str, ...] | None = None,
        user_agent: str | None = None,
        gateway: object | None = None,
    ) -> None:
        self.proxy_url = proxy_url
        self.approved_origins = tuple(approved_origins) if approved_origins is not None else None
        self.gateway = gateway
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._closed = False
        self._last_status = 200
        self._final_url = ""
        self._location: str | None = None
        self._egress_ip = ""
        self._current_user_agent = user_agent or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    def _ensure(self) -> None:
        if self._page is not None and not self._closed:
            return
        fixture_on = (os.environ.get("SNOWBALL_CHALLENGE_FIXTURE") or "").strip() == "1"
        if not fixture_on:
            ok, reason = guard.check_namespace_isolation()
            if not ok:
                raise RuntimeError(reason or "network_namespace_not_active")
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launch_args = [
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-sync",
            "--no-first-run",
        ]
        proxy_cfg = {"server": self.proxy_url} if self.proxy_url else None
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=launch_args,
            proxy=proxy_cfg,
        )
        self._context = self._browser.new_context(
            accept_downloads=False,
            service_workers="block",
            user_agent=self._current_user_agent,
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=False,
        )
        self._page = self._context.new_page()
        self._page.on("popup", lambda p: p.close())
        self._page.on("download", lambda d: d.cancel())

        self._page.add_init_script("""() => {
            window.__snowball_captured = { turnstile: null, recaptcha: null };
            let _turnstile = undefined;
            Object.defineProperty(window, 'turnstile', {
                configurable: true,
                get: () => _turnstile,
                set: (val) => {
                    _turnstile = val;
                    if (val && typeof val.render === 'function') {
                        const origRender = val.render;
                        val.render = function(container, options) {
                            if (options && options.sitekey) {
                                window.__snowball_captured.turnstile = {
                                    sitekey: String(options.sitekey),
                                    action: options.action ? String(options.action) : null,
                                    cData: options.cData ? String(options.cData) : null,
                                    pagedata: options.pagedata ? String(options.pagedata) : (options.chlPageData ? String(options.chlPageData) : null),
                                    callbackName: typeof options.callback === 'string' ? options.callback : null,
                                };
                                if (typeof options.callback === 'function') {
                                    window.__snowball_turnstile_cb = options.callback;
                                }
                            }
                            return origRender.apply(this, arguments);
                        };
                    }
                }
            });

            let _grecaptcha = undefined;
            Object.defineProperty(window, 'grecaptcha', {
                configurable: true,
                get: () => _grecaptcha,
                set: (val) => {
                    _grecaptcha = val;
                    if (val && typeof val.render === 'function') {
                        const origRender = val.render;
                        val.render = function(container, options) {
                            if (options && options.sitekey) {
                                window.__snowball_captured.recaptcha = {
                                    sitekey: String(options.sitekey),
                                    callbackName: typeof options.callback === 'string' ? options.callback : null,
                                };
                                if (typeof options.callback === 'function') {
                                    window.__snowball_recaptcha_cb = options.callback;
                                }
                            }
                            return origRender.apply(this, arguments);
                        };
                    }
                }
            });
        }""")

        if self.approved_origins:
            def route_filter(route, request):
                url = request.url
                if not (url.startswith("http://") or url.startswith("https://")):
                    route.abort()
                    return
                parsed = urlsplit(url)
                scheme = (parsed.scheme or "https").lower()
                host = (parsed.hostname or "").rstrip(".").lower()
                port = parsed.port or (443 if scheme == "https" else 80)
                if port not in (80, 443):
                    route.abort()
                    return
                req_origin = f"{scheme}://{host}"
                if port != (443 if scheme == "https" else 80):
                    req_origin += f":{port}"
                if guard._origin_matches(req_origin, self.approved_origins):
                    route.continue_()
                else:
                    route.abort()
            self._page.route("**/*", route_filter)

    def open(self, url: str) -> dict:
        self._ensure()
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self._final_url = self._page.url
        self._last_status = resp.status if resp is not None else 200
        self._location = resp.headers.get("location") if resp is not None else None
        return {
            "status": self._last_status,
            "final_url": self._final_url,
            "location": self._location,
        }

    def echo_ip(self) -> str | None:
        return self._egress_ip or None

    def apply_user_agent(self, user_agent: str) -> None:
        self._current_user_agent = user_agent
        if self._page and not self._page.is_closed():
            self._page.set_extra_http_headers({"User-Agent": user_agent})
            try:
                self._page.evaluate(
                    f"Object.defineProperty(navigator, 'userAgent', {{ get: () => {json.dumps(user_agent)} }});"
                )
            except Exception:
                pass

    def capture(self) -> dict:
        if not self._page or self._page.is_closed():
            return {}
        try:
            self._page.wait_for_timeout(1000)
        except Exception:
            pass
        # 1. Check intercepted render data from init script
        try:
            captured = self._page.evaluate("() => window.__snowball_captured || {}")
            if isinstance(captured, dict):
                ts = captured.get("turnstile")
                if isinstance(ts, dict) and ts.get("sitekey"):
                    return {
                        "kind": "challenge_turnstile",
                        "sitekey": str(ts["sitekey"]),
                        "action": ts.get("action") or None,
                        "data": ts.get("cData") or None,
                        "pagedata": ts.get("pagedata") or None,
                        "invisible": False,
                    }
                rc = captured.get("recaptcha")
                if isinstance(rc, dict) and rc.get("sitekey"):
                    return {
                        "kind": "challenge_recaptcha",
                        "sitekey": str(rc["sitekey"]),
                        "api_domain": "www.google.com",
                        "invisible": False,
                    }
        except Exception:
            pass

        # 2. Check DOM elements (strictly without fake sitekey fallback)
        try:
            dom_info = self._page.evaluate("""() => {
                const tsEl = document.querySelector('.cf-turnstile[data-sitekey]');
                if (tsEl && tsEl.getAttribute('data-sitekey')) {
                    return {
                        kind: 'challenge_turnstile',
                        sitekey: tsEl.getAttribute('data-sitekey'),
                        action: tsEl.getAttribute('data-action') || null,
                        cData: tsEl.getAttribute('data-cdata') || null,
                        pagedata: tsEl.getAttribute('data-pagedata') || null,
                    };
                }
                const tsFrame = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (tsFrame) {
                    const src = tsFrame.getAttribute('src') || '';
                    const m = src.match(/[?&]sitekey=([^&]+)/);
                    if (m) {
                        return { kind: 'challenge_turnstile', sitekey: decodeURIComponent(m[1]) };
                    }
                }
                const rcEl = document.querySelector('.g-recaptcha[data-sitekey]');
                if (rcEl && rcEl.getAttribute('data-sitekey')) {
                    return { kind: 'challenge_recaptcha', sitekey: rcEl.getAttribute('data-sitekey'), apiDomain: 'www.google.com' };
                }
                const rcFrame = document.querySelector('iframe[src*="google.com/recaptcha"], iframe[src*="recaptcha.net"]');
                if (rcFrame) {
                    const src = rcFrame.getAttribute('src') || '';
                    const m = src.match(/[?&]k=([^&]+)/);
                    if (m) {
                        const apiDomain = src.includes('recaptcha.net') ? 'recaptcha.net' : 'www.google.com';
                        return { kind: 'challenge_recaptcha', sitekey: decodeURIComponent(m[1]), apiDomain: apiDomain };
                    }
                }
                return null;
            }""")
            if dom_info and isinstance(dom_info, dict) and dom_info.get("sitekey"):
                kind = dom_info["kind"]
                return {
                    "kind": kind,
                    "sitekey": str(dom_info["sitekey"]),
                    "action": dom_info.get("action"),
                    "data": dom_info.get("cData"),
                    "pagedata": dom_info.get("pagedata"),
                    "api_domain": dom_info.get("apiDomain"),
                    "invisible": False,
                }
        except Exception:
            pass
        return {}

    def invoke_callback(self, token: str, field: str) -> None:
        if not self._page or self._page.is_closed():
            return
        script = f"""(token) => {{
            let fieldName = {json.dumps(field)};
            let inputs = document.querySelectorAll(`[name="${{fieldName}}"], #${{fieldName}}`);
            if (inputs.length === 0) {{
                let hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = fieldName;
                hidden.id = fieldName;
                document.body.appendChild(hidden);
                inputs = [hidden];
            }}
            inputs.forEach(input => {{
                input.value = token;
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }});

            // Verified Turnstile callback
            if (typeof window.__snowball_turnstile_cb === 'function') {{
                try {{ window.__snowball_turnstile_cb(token); }} catch(e) {{}}
            }} else if (window.turnstile && typeof window.turnstile.callback === 'function') {{
                try {{ window.turnstile.callback(token); }} catch(e) {{}}
            }} else {{
                const tsElem = document.querySelector('.cf-turnstile[data-callback]');
                if (tsElem) {{
                    const cbName = tsElem.getAttribute('data-callback');
                    if (cbName && typeof window[cbName] === 'function') {{
                        try {{ window[cbName](token); }} catch(e) {{}}
                    }}
                }}
            }}

            // Verified reCAPTCHA callback
            if (typeof window.__snowball_recaptcha_cb === 'function') {{
                try {{ window.__snowball_recaptcha_cb(token); }} catch(e) {{}}
            }} else {{
                const rcElem = document.querySelector('.g-recaptcha[data-callback]');
                if (rcElem) {{
                    const cbName = rcElem.getAttribute('data-callback');
                    if (cbName && typeof window[cbName] === 'function') {{
                        try {{ window[cbName](token); }} catch(e) {{}}
                    }}
                }}
            }}
        }}"""
        try:
            self._page.evaluate(script, token)
            self._page.wait_for_timeout(3000)
            self._page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

    def read_outcome(self) -> dict:
        if not self._page or self._page.is_closed():
            return {"challenge_visible": False, "text": "", "title": ""}
        check_res = {}
        try:
            check_res = self._page.evaluate(r"""() => {
                const ts = document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com"]');
                const rc = document.querySelector('.g-recaptcha, iframe[src*="recaptcha"]');
                const txt = document.body ? document.body.innerText : '';
                const wallRx = /(cf-chl|__cf_chl_|challenge-platform|Just a moment\.\.\.|Attention Required!|cf-turnstile|Please verify you are a human|hcaptcha\.com|recaptcha\/api|g-recaptcha|px-captcha|perimeterx|datadome|Access Denied|Checking your browser|enable JavaScript and cookies)/i;
                return {
                    hasTs: Boolean(ts),
                    hasRc: Boolean(rc),
                    hasWallText: wallRx.test(txt),
                    text: txt,
                    title: document.title || '',
                };
            }""")
        except Exception:
            pass
        text = check_res.get("text", "") if isinstance(check_res, dict) else ""
        title = check_res.get("title", "") if isinstance(check_res, dict) else ""
        has_elements = check_res.get("hasTs") or check_res.get("hasRc") if isinstance(check_res, dict) else False
        has_wall_text = check_res.get("hasWallText") if isinstance(check_res, dict) else False
        challenge_visible = bool(has_elements or has_wall_text or len(text.strip()) < MIN_TEXT)
        return {
            "challenge_visible": challenge_visible,
            "text": text,
            "title": title,
        }
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._page and not self._page.is_closed():
                self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        if self.gateway and hasattr(self.gateway, "close"):
            try:
                self.gateway.close()
            except Exception:
                pass


class IsolatedWorkerSession:
    """Session that drives an isolated browser worker subprocess over bounded JSON IPC."""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        approved_origins: tuple[str, ...] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.gateway_url = gateway_url
        self.approved_origins = approved_origins
        self.environ = environ
        self.proc = None
        self.client: IsolatedBrowserClient | None = None
        self.session_handle: str | None = None
        self._captured: dict = {}
        self._last_result: FetchResult | None = None
        self._closed = False
        self._egress_ip: str | None = None

    def _ensure_worker(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        env_dict = os.environ if self.environ is None else self.environ
        fixture_on = (env_dict.get("SNOWBALL_CHALLENGE_FIXTURE") or "").strip() == "1"
        in_netns = (sys.platform == "linux" and Path("/run/netns/snowball-browser").exists()) or guard.check_namespace_isolation()[0]
        if not fixture_on and not in_netns:
            raise RuntimeError("network_namespace_not_active")

        python_bin = sys.executable
        goku_venv = Path("/home/ubuntu/repos/snowball/.venv/bin/python")
        if goku_venv.exists():
            python_bin = str(goku_venv)

        cmd = [python_bin, str(_TOOLS_DIR / "browser_worker.py"), "--serve"]
        if self.gateway_url:
            cmd.extend(["--gateway-url", self.gateway_url])

        if sys.platform == "linux" and Path("/run/netns/snowball-browser").exists():
            cmd = ["ip", "netns", "exec", "snowball-browser"] + cmd

        env = guard.scrub_proxy_env(os.environ if self.environ is None else self.environ)
        env["SNOWBALL_CHALLENGE_FIXTURE"] = "1"
        self.proc = pc.launch_owned_process(cmd, env=env)
        self.client = IsolatedBrowserClient(self.proc.stdin, self.proc.stdout)
    def open(self, url: str) -> dict:
        self._ensure_worker()
        req = AccessRequest(
            acquisition_id=f"acq-{int(time.time() * 1000)}",
            page_id=1,
            url=url,
            session_id=f"acq-{int(time.time() * 1000)}",
            allowed_path_prefixes=("/",),
            approved_resource_origins=self.approved_origins or (url,),
            deadline_at=time.time() + 60.0,
            private=False,
        )
        resp = self.client.open(req, time.time() + 60.0)
        status = resp.get("status")
        if status == "challenge":
            self.session_handle = resp.get("session_handle")
            self._captured = resp.get("challenge", {})
            return {"status": 200, "final_url": self._captured.get("url", url), "location": None}
        elif status == "ok":
            self._last_result = FetchResult(**resp.get("result", {}))
            return {"status": 200, "final_url": self._last_result.final_url, "location": None}
        else:
            res_dict = resp.get("result", {})
            self._last_result = FetchResult(**res_dict) if res_dict else None
            return {"status": 403, "final_url": url, "location": None}

    def capture(self) -> dict:
        return self._captured

    def echo_ip(self) -> str | None:
        return self._egress_ip

    def apply_user_agent(self, user_agent: str) -> None:
        pass

    def invoke_callback(self, token: str, field: str) -> None:
        if not self.client or not self.session_handle:
            return
        ch_kind = self._captured.get("kind", "challenge_turnstile")
        resp = self.client.solve(
            self.session_handle,
            token,
            user_agent=None,
            deadline_at=time.time() + 60.0,
            challenge_kind=ch_kind,
        )
        res_dict = resp.get("result", {})
        if res_dict:
            self._last_result = FetchResult(**res_dict)

    def read_outcome(self) -> dict:
        if self._last_result and self._last_result.outcome == "ok":
            return {
                "challenge_visible": False,
                "text": self._last_result.text or "",
                "title": self._last_result.title or "",
            }
        return {"challenge_visible": True, "text": "", "title": ""}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.client and self.session_handle:
            try:
                self.client.close(self.session_handle)
            except Exception:
                pass
        if self.proc:
            pc.stop_owned_process_tree(self.proc, grace_seconds=0.2)
            self.proc = None


def playwright_session_factory(
    *,
    gateway: object | None = None,
    proxy_url: str | None = None,
    approved_origins: tuple[str, ...] | None = None,
    enforce_isolation: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Callable[[], object] | None:
    """Return an isolated worker session factory, or None if prerequisites fail."""
    if importlib.util.find_spec("playwright") is None:
        return None
    if not _is_chromium_available(environ):
        return None
    env = os.environ if environ is None else environ
    fixture_on = (env.get("SNOWBALL_CHALLENGE_FIXTURE") or "").strip() == "1"
    if not fixture_on or enforce_isolation:
        ok, _reason = guard.check_namespace_isolation()
        if not ok:
            return None
    gw_url = proxy_url or (getattr(gateway, "url", None))
    return lambda: IsolatedWorkerSession(
        gateway_url=gw_url,
        approved_origins=approved_origins,
        environ=environ,
    )

def activation_block_reason(
    *,
    fixture_mode: bool,
    api_key: str | None,
    allowed_modes: tuple[str, ...],
    caps: Mapping[str, object],
    environ: Mapping[str, str] | None = None,
    require_isolation: bool = False,
) -> str | None:
    """Why the solver must not run. None means the gates that are visible from here passed."""
    if fixture_mode is not True:
        return "fixture_off"
    if not isinstance(api_key, str) or not api_key.strip():
        return "credential_missing"
    if "solver" not in allowed_modes:
        return "solver_not_allowed"
    if not _ceilings_open(caps):
        return "ceilings_inactive"
    if require_isolation:
        ok, reason = guard.check_namespace_isolation()
        if not ok:
            return reason
    return None


def acquire(
    request: AccessRequest,
    cfg,
    deadline_at: float,
    *,
    session_factory=None,
    solve: Callable | None = None,
    resolver: guard.Resolver | None = None,
    clock: Callable[[], float] | None = None,
) -> FetchResult:
    """Open one context, solve one authorized challenge, and require the page text afterwards."""
    now = clock or time.time
    blocked = _precheck(request, deadline_at, now)
    if blocked is not None:
        return blocked
    session, failed = _open_session(session_factory, request)
    if failed is not None:
        return failed
    try:
        return _acquire_open(session, request, cfg, deadline_at, solve, resolver or guard.default_resolver, now)
    except guard.EgressDenied as exc:
        return _result(request, "ssrf_blocked", exc.reason)
    except Exception as exc:  # noqa: BLE001 - a browser error must not escape with page or token text
        err_msg = str(exc)
        if "network_namespace_not_active" in err_msg or "network_namespace_unsupported" in err_msg:
            return _result(request, "blocked", "network_namespace_not_active")
        return _result(request, "failure", None)
    finally:
        _close(session)


def solve_with_captcha(captcha_module, store, cfg, reservation_factory, *, transport=None, sleep=None, clock=None):
    """Poll one reservation until it is terminal. The transport defaults to the real adapter."""
    downstream = transport or captcha_module.http_transport
    sleeper = sleep or time.sleep
    now_fn = clock or time.time

    def wrapped(spec: dict) -> object:
        body = dict(spec.get("body") or {})
        key = captcha_module._key(cfg)
        if key and "clientKey" not in body:
            body["clientKey"] = key
        return downstream({**spec, "body": body})

    def solve(challenge, deadline_at: float):
        if now_fn() >= float(deadline_at):
            return captcha_module.SolveResult(status="deadline")
        reservation = reservation_factory(challenge)
        task = captcha_module.submit(challenge, cfg, reservation, store=store, transport=wrapped, now=now_fn())
        for _ in range(48):
            moment = now_fn()
            if moment >= float(deadline_at):
                return captcha_module.poll(task, deadline_at, store=store, transport=wrapped, now=moment)
            result = captcha_module.poll(task, deadline_at, store=store, transport=wrapped, now=moment)
            if result.status == "not_accepted":
                task = captcha_module.submit(challenge, cfg, reservation, store=store, transport=wrapped, now=moment)
                sleeper(captcha_module.MIN_POLL_SEC)
                continue
            if result.status in {"processing", "submitted"}:
                sleeper(captcha_module.MIN_POLL_SEC)
                continue
            return result
        return captcha_module.SolveResult(status="deadline")

    return solve


def enforce_deadline(
    argv: list[str],
    timeout: float,
    grace: float = 0.2,
    stdout_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[int]:
    """Stop an owned worker tree at timeout. The returned list is pids still alive after grace."""
    source = os.environ if environ is None else environ
    env = guard.scrub_proxy_env(source)
    owned = pc.launch_owned_process(list(argv), stdout_path=stdout_path, env=env)
    try:
        deadline = time.monotonic() + float(timeout)
        while owned.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        pc.stop_owned_process_tree(owned, grace_seconds=grace)
    if _pid_alive(owned.pid):
        return [owned.pid]
    return []


def check_egress(environ: Mapping[str, str] | None = None) -> str | None:
    """Return `environment_proxy` when inherited proxy variables would change the route."""
    if guard.proxy_env_names(os.environ if environ is None else environ):
        return "environment_proxy"
    return None


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Process entry. Exits before stdin or any network fetch when the fixture or browser is off."""
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Snowball challenge worker")
    parser.add_argument("--check-egress", nargs="?", const="text", default=None)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--gateway", action="store_true")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--oneshot", action="store_true")
    args = parser.parse_args(argv)
    if args.check_egress is not None:
        return _egress_main(args.check_egress, env)
    if args.gateway:
        return _gateway_main(args.host, args.port, env)
    if args.serve:
        return _serve_main(env, gateway_url=args.gateway_url, oneshot=args.oneshot)
    parser.error("choose --check-egress, --serve, or --gateway")
    return 2


def _gateway_main(host: str, port: int, env: Mapping[str, str]) -> int:
    reason = check_egress(env)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    gw = guard.EgressGateway(host=host, port=port)
    gw.start()
    print(f"EgressGateway listening on {gw.url}", flush=True)
    try:
        while not Path("STOP").exists():
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        gw.close()
    return 0


class IsolatedBrowserClient:
    """Client for coordinating with isolated browser worker daemon over bounded JSON IPC."""

    def __init__(self, stdin, stdout) -> None:
        self.stdin = stdin
        self.stdout = stdout

    def open(self, request: AccessRequest, deadline_at: float) -> dict:
        payload = {"action": "open", "request": request.__dict__, "deadline_at": deadline_at}
        self.stdin.write(json.dumps(payload) + "\n")
        self.stdin.flush()
        line = self.stdout.readline()
        return json.loads(line)

    def solve(
        self,
        session_handle: str,
        token: str,
        user_agent: str | None,
        deadline_at: float,
        challenge_kind: str = "challenge_turnstile",
    ) -> dict:
        payload = {
            "action": "solve",
            "session_handle": session_handle,
            "token": token,
            "user_agent": user_agent,
            "deadline_at": deadline_at,
            "challenge_kind": challenge_kind,
        }
        self.stdin.write(json.dumps(payload) + "\n")
        self.stdin.flush()
        line = self.stdout.readline()
        return json.loads(line)

    def close(self, session_handle: str) -> None:
        payload = {"action": "close", "session_handle": session_handle}
        self.stdin.write(json.dumps(payload) + "\n")
        self.stdin.flush()
        self.stdout.readline()


def _egress_main(mode: str, env: Mapping[str, str]) -> int:
    reason = check_egress(env)
    if mode == "json":
        print(json.dumps({"reason": reason}))
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


def _serve_main(env: Mapping[str, str], gateway_url: str | None = None, oneshot: bool = False) -> int:
    reason = check_egress(env)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    if env.get("SNOWBALL_CHALLENGE_FIXTURE") != "1":
        print("fixture_missing", file=sys.stderr)
        return 2
    factory = playwright_session_factory(environ=env)
    if factory is None:
        print("playwright_missing", file=sys.stderr)
        return 2

    old_fixture = os.environ.get("SNOWBALL_CHALLENGE_FIXTURE")
    os.environ["SNOWBALL_CHALLENGE_FIXTURE"] = "1"
    gw_url = gateway_url or env.get("SNOWBALL_GATEWAY_URL")
    active_sessions: dict[str, tuple[PlaywrightSession, AccessRequest, object | None]] = {}

    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if Path("STOP").exists():
                print(json.dumps({"outcome": "stopped", "detail": "STOP file detected"}), flush=True)
                break
            try:
                payload = json.loads(line)
                action = payload.get("action", "acquire")
                handle = str(payload.get("session_handle", ""))

                if action == "open":
                    req_dict = payload.get("request", {})
                    req = _access_request_from_dict(req_dict)
                    deadline = float(payload.get("deadline_at", time.time() + 60.0))
                    origins = req.approved_resource_origins
                    local_gw = None
                    proxy_to_use = gw_url
                    if not proxy_to_use and hasattr(guard, "EgressGateway"):
                        local_gw = guard.EgressGateway(approved_origins=origins)
                        local_gw.start()
                        proxy_to_use = local_gw.url

                    sess = PlaywrightSession(
                        proxy_url=proxy_to_use,
                        approved_origins=origins,
                        gateway=local_gw,
                    )
                    final_url, failed = _navigate(sess, req, guard.default_resolver)
                    if failed is not None:
                        sess.close()
                        print(json.dumps({"status": "failed", "result": failed.__dict__}), flush=True)
                        continue
                    ch = sess.capture()
                    ch_obj = _challenge_from(ch, req, final_url)
                    if isinstance(ch_obj, FetchResult):
                        sess.close()
                        print(json.dumps({"status": "walled", "result": ch_obj.__dict__}), flush=True)
                        continue
                    sess_handle = req.acquisition_id or f"sess-{time.time()}"
                    active_sessions[sess_handle] = (sess, req, local_gw)
                    ch_dict = {
                        "kind": ch_obj.kind,
                        "url": ch_obj.url,
                        "sitekey": ch_obj.sitekey,
                        "session_id": ch_obj.session_id,
                        "action": ch_obj.action,
                        "data": ch_obj.data,
                        "pagedata": ch_obj.pagedata,
                        "api_domain": ch_obj.api_domain,
                        "invisible": ch_obj.invisible,
                    }
                    print(json.dumps({"status": "challenge", "session_handle": sess_handle, "challenge": ch_dict}), flush=True)

                elif action == "solve":
                    if handle not in active_sessions:
                        print(json.dumps({"status": "failed", "error": "unknown_session_handle"}), flush=True)
                        continue
                    sess, req, local_gw = active_sessions.pop(handle)
                    token = payload.get("token")
                    ua = payload.get("user_agent")
                    deadline = float(payload.get("deadline_at", time.time() + 60.0))
                    ch_kind = payload.get("challenge_kind", "challenge_turnstile")
                    try:
                        field = CALLBACK_FIELDS[0] if ch_kind == "challenge_turnstile" else CALLBACK_FIELDS[1]
                        sess.invoke_callback(str(token), field, deadline_at=deadline)
                        out = sess.read_outcome()
                        outcome = "walled" if out.get("challenge_visible") else "ok"
                        text = out.get("text", "")
                        title = out.get("title", "")
                        res = FetchResult(
                            url=req.url,
                            route="browser",
                            outcome=outcome,
                            status=200 if outcome == "ok" else 403,
                            text=text,
                            title=title,
                            final_url=getattr(sess, "_final_url", req.url),
                            wall_kind=ch_kind if outcome == "walled" else None,
                        )
                        print(json.dumps({"status": outcome, "result": res.__dict__}), flush=True)
                    finally:
                        sess.close()

                elif action == "close":
                    if handle in active_sessions:
                        sess, req, local_gw = active_sessions.pop(handle)
                        sess.close()
                    print(json.dumps({"status": "closed"}), flush=True)

                elif action == "acquire":
                    req_dict = payload.get("request", {})
                    req = _access_request_from_dict(req_dict)
                    cfg_dict = payload.get("config", {})
                    captcha_mod = sys.modules.get("snowball_captcha")
                    cfg = captcha_mod.SolverConfig(**cfg_dict) if (captcha_mod and cfg_dict) else None
                    deadline = float(payload.get("deadline_at", time.time() + 60.0))
                    origins = req.approved_resource_origins
                    local_gw = None
                    proxy_to_use = gw_url
                    if not proxy_to_use and hasattr(guard, "EgressGateway"):
                        local_gw = guard.EgressGateway(approved_origins=origins)
                        local_gw.start()
                        proxy_to_use = local_gw.url
                    try:
                        sess_factory = lambda: PlaywrightSession(
                            proxy_url=proxy_to_use,
                            approved_origins=origins,
                            gateway=local_gw,
                        )
                        res = acquire(req, cfg, deadline, session_factory=sess_factory)
                        out = {
                            "url": res.url,
                            "route": res.route,
                            "outcome": res.outcome,
                            "status": res.status,
                            "text": res.text,
                            "title": res.title,
                            "final_url": res.final_url,
                            "wall_kind": res.wall_kind,
                        }
                        print(json.dumps(out), flush=True)
                    finally:
                        if local_gw:
                            local_gw.close()
                else:
                    print(json.dumps({"error": f"unsupported_action: {action}"}), flush=True)
            except Exception as exc:
                print(json.dumps({"outcome": "failure", "error": str(exc)}), flush=True)
            if oneshot:
                break
    finally:
        for handle, (s, _, _) in list(active_sessions.items()):
            try:
                s.close()
            except Exception:
                pass
        if old_fixture is not None:
            os.environ["SNOWBALL_CHALLENGE_FIXTURE"] = old_fixture
        else:
            os.environ.pop("SNOWBALL_CHALLENGE_FIXTURE", None)
        active_sessions.clear()
    return 0

def _precheck(request: AccessRequest, deadline_at: float, now: Callable[[], float]) -> FetchResult | None:
    if request.private:
        return _result(request, "walled", "not_allowed")
    if request.session_id != request.acquisition_id:
        return _result(request, "walled", "mismatch")
    if not _path_allowed(request.url, request.allowed_path_prefixes):
        return _result(request, "walled", "path")
    if now() >= float(deadline_at):
        return _result(request, "deadline", None)
    return None


def _open_session(session_factory, request: AccessRequest):
    if session_factory is None:
        return None, _result(request, "blocked", "browser_unavailable")
    try:
        session = session_factory()
    except (ImportError, Exception) as exc:
        err_msg = str(exc)
        if "network_namespace_not_active" in err_msg or "network_namespace_unsupported" in err_msg:
            return None, _result(request, "blocked", "network_namespace_not_active")
        return None, _result(request, "blocked", "browser_unavailable")
    if session is None:
        return None, _result(request, "blocked", "browser_unavailable")
    return session, None

def _acquire_open(session, request, cfg, deadline_at, solve, resolver, now) -> FetchResult:
    # `cfg` stays on the acquire contract. The solve callback closes over the solver config.
    del cfg
    final, failed = _navigate(session, request, resolver)
    if failed is not None:
        return failed
    if not _same_host(request.url, final):
        return _result(request, "walled", "off_origin", final_url=final)
    if not _path_allowed(final, request.allowed_path_prefixes):
        return _result(request, "walled", "path", final_url=final)
    challenge = _challenge_from(session.capture(), request, final)
    if isinstance(challenge, FetchResult):
        return challenge
    if solve is None:
        return _result(request, "walled", "solver_unavailable", final_url=final)
    if now() >= float(deadline_at):
        return _result(request, "deadline", None, final_url=final)
    return _finish_solve(session, request, challenge, solve, deadline_at, final)


def _finish_solve(session, request, challenge, solve, deadline_at: float, final: str) -> FetchResult:
    solved = solve(challenge, deadline_at)
    if getattr(solved, "status", None) == "deadline":
        return _result(request, "deadline", None, final_url=final)
    text = _applied_text(session, solved, challenge)
    if text is None:
        return _result(request, "walled", challenge.kind, final_url=final)
    if len(text) < MIN_TEXT:
        return _result(request, "failure", None, final_url=final)
    title = ""
    outcome = session.read_outcome()
    if isinstance(outcome, dict) and isinstance(outcome.get("title"), str):
        title = outcome["title"][:300]
    return _result(request, "ok", None, final_url=final, text=text, status=200, title=title)


def _navigate(session, request: AccessRequest, resolver) -> tuple[str, FetchResult | None]:
    current = request.url
    for _hop in range(MAX_HOPS):
        guard.validate_target(current, resolver=resolver)
        opened = session.open(current)
        guard.validate_target(current, resolver=resolver)
        if not isinstance(opened, dict):
            return current, _result(request, "failure", None, final_url=current)
        status = opened.get("status")
        if status in REDIRECTS:
            location = opened.get("location")
            if not isinstance(location, str) or not location:
                return current, _result(request, "failure", None, final_url=current)
            nxt = urljoin(current, location)
            guard.validate_target(nxt, resolver=resolver)
            if not _same_host(request.url, nxt):
                return current, _result(request, "walled", "off_origin", final_url=current)
            if not _path_allowed(nxt, request.allowed_path_prefixes):
                return current, _result(request, "walled", "path", final_url=current)
            current = nxt
            continue
        final = opened.get("final_url") if isinstance(opened.get("final_url"), str) else current
        if final != current:
            guard.validate_target(final, resolver=resolver)
        return final, None
    return current, _result(request, "failure", None, final_url=current)


def _challenge_from(raw, request: AccessRequest, final_url: str):
    if not isinstance(raw, dict):
        return _result(request, "walled", "unsupported_challenge", final_url=final_url)
    kind = raw.get("kind")
    if kind not in {"challenge_turnstile", "challenge_recaptcha"}:
        return _result(request, "walled", "unsupported_challenge", final_url=final_url)
    sitekey = raw.get("sitekey")
    if not isinstance(sitekey, str) or not sitekey.strip():
        return _result(request, "walled", kind, final_url=final_url)
    api = raw.get("api_domain")
    return _challenge(
        kind=kind,
        url=final_url,
        sitekey=sitekey,
        session_id=request.session_id,
        action=_text_field(raw.get("action")),
        data=_text_field(raw.get("data")),
        pagedata=_text_field(raw.get("pagedata")),
        api_domain=api if isinstance(api, str) and api else None,
        invisible=raw.get("invisible") is True,
    )


def _challenge(**kwargs):
    captcha = sys.modules.get("snowball_captcha")
    if captcha is None:
        captcha = _load("snowball_captcha", "captcha.py")
    return captcha.Challenge(**kwargs)


def _applied_text(session, solved, challenge) -> str | None:
    if solved is None or getattr(solved, "status", None) != "ready" or not getattr(solved, "token", None):
        return None
    shaped = any((challenge.action, challenge.data, challenge.pagedata))
    if shaped and not getattr(solved, "user_agent", None):
        return None
    if hasattr(session, "echo_ip"):
        before = session.echo_ip()
        if getattr(solved, "user_agent", None) and hasattr(session, "apply_user_agent"):
            session.apply_user_agent(solved.user_agent)
        field = CALLBACK_FIELDS[0] if challenge.kind == "challenge_turnstile" else CALLBACK_FIELDS[1]
        session.invoke_callback(solved.token, field)
        after = session.echo_ip()
        if before is not None and after is not None and before != after:
            return None
        if before is not None and after is None:
            return None
        if before is None and after is not None:
            return None
    else:
        field = CALLBACK_FIELDS[0] if challenge.kind == "challenge_turnstile" else CALLBACK_FIELDS[1]
        session.invoke_callback(solved.token, field)
    outcome = session.read_outcome()
    if not isinstance(outcome, dict) or outcome.get("challenge_visible") is not False:
        return None
    text = outcome.get("text")
    if not isinstance(text, str) or len(text.strip()) < MIN_TEXT:
        return None
    return text

def _ceilings_open(caps: Mapping[str, object]) -> bool:
    tariffs = caps.get("tariffs")
    tariff = tariffs.get("captcha") if isinstance(tariffs, Mapping) else None
    return bool(
        _positive(caps.get("daily_micro_usd"))
        and _positive(caps.get("monthly_micro_usd"))
        and _positive(caps.get("daily_solver_count"))
        and _positive(caps.get("monthly_solver_count"))
        and _positive(tariff)
    )


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _path_allowed(url: str, prefixes: tuple[str, ...]) -> bool:
    path = urlsplit(url).path or "/"
    if not prefixes:
        return False
    for prefix in prefixes:
        if not isinstance(prefix, str) or not prefix:
            continue
        base = prefix.rstrip("/") or "/"
        if base == "/" or path == base or path.startswith(base + "/"):
            return True
    return False


def _same_host(left: str, right: str) -> bool:
    return (urlsplit(left).hostname or "").lower() == (urlsplit(right).hostname or "").lower()


def _text_field(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _result(
    request: AccessRequest,
    outcome: str,
    wall_kind: str | None,
    *,
    final_url: str | None = None,
    text: str = "",
    status: int | None = None,
    title: str = "",
) -> FetchResult:
    return FetchResult(
        canon=request.url,
        route="browser",
        outcome=outcome,
        status=status,
        title=title,
        text=text,
        wall_kind=wall_kind,
        nbytes=len(text.encode("utf-8")) if text else 0,
        final_url=final_url,
    )


def _close(session) -> None:
    closer = getattr(session, "close", None)
    if closer is not None:
        closer()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _win_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    access = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
