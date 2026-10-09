#!/usr/bin/env python3
"""ZenRows and residential retrieval. No live call unless every gate has already passed.

ZenRows is off for any URL that is not on an exact origin and path allowlist. A local DNS
pin does not cover ZenRows' resolver. After an allowlisted call the body is kept only when
Zr-Final-Url is still inside that same allowlist entry. mode=auto reserves the 25-credit
weight and must not also send js_render or premium_proxy. Credit weights are provider units,
not micro-USD. A missing X-Request-Credits header leaves the micro-USD reservation in place.

Residential calls use one explicit proxy URL. Environment proxy variables are a hard stop.
curl_cffi's trust_env flag is not used: the fetched client page does not document it.
IP-allowlisted proxies are ineligible. A Fetch session_id is never turned into a browser
or solver proxy.
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

import importlib.util
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit

_spec_budget = importlib.util.spec_from_file_location("snowball_paid_budget", _TOOLS_DIR / "paid_budget.py")
if _spec_budget is None or _spec_budget.loader is None:
    raise RuntimeError("paid_budget.py")
pb = importlib.util.module_from_spec(_spec_budget)
sys.modules["snowball_paid_budget"] = pb
_spec_budget.loader.exec_module(pb)

_spec_guard = importlib.util.spec_from_file_location("snowball_egress_guard", _TOOLS_DIR / "egress_guard.py")
if _spec_guard is None or _spec_guard.loader is None:
    raise RuntimeError("egress_guard.py")
guard = importlib.util.module_from_spec(_spec_guard)
sys.modules["snowball_egress_guard"] = guard
_spec_guard.loader.exec_module(guard)

CONFIRMED_NONBILLABLE = pb.CONFIRMED_NONBILLABLE
ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"
CREDIT_WEIGHT = {"standard": 1, "js_render": 5, "premium_proxy": 10, "protected": 25, "auto": 25}
NONBILLABLE_CODES = frozenset({"REQS002", "CTX0001"})
BILLED_CODES = frozenset({"RESP002", "RESP007"})
PROVIDER_CODE = re.compile(r"^(?:REQS|RESP|AUTH|CTX|REQ_)[A-Z0-9]+$")
TERMINAL_WALLS = frozenset(
    {
        "login",
        "paywall",
        "generic_401",
        "generic_403",
        "generic_407",
        "generic_451",
        "429",
        "ban",
        "robots",
        "unsupported_challenge",
        "http",
    }
)
CHALLENGE_KINDS = frozenset({"challenge_turnstile", "challenge_recaptcha"})
TURNSTILE_RX = re.compile(r"(cf-turnstile|cf-chl|__cf_chl_|challenge-platform|Just a moment\.\.\.)", re.I)
RECAPTCHA_RX = re.compile(r"(g-recaptcha|recaptcha/api)", re.I)
OTHER_CHALLENGE_RX = re.compile(
    r"(hcaptcha\.com|px-captcha|perimeterx|datadome|Checking your browser|"
    r"enable JavaScript and cookies to continue|Please verify you are a human)",
    re.I,
)
ZENROWS_TTL_SEC = {"30s": 30, "1m": 60, "30m": 1800, "1h": 3600, "1d": 86400}
_TAG_RX = re.compile(r"(?is)<(script|style).*?>.*?</\1>|<[^>]+>")


@dataclass
class PaidConfig:
    provider: str = ""
    zenrows_mode: str = "auto"
    zenrows_api_key: str | None = None
    residential_proxy_url: str | None = None
    min_yield: float = 0.30
    max_body_bytes: int = 5_242_880
    max_proxy_bytes: int = 20_971_520
    max_retrievals: int = 2
    timeout_sec: int = 180


@dataclass
class PageContext:
    page_id: int
    domain: str
    predicted_kind: str | None
    yield_score: float
    paid_failures: int
    paid_attempts: int


@dataclass
class HttpExchange:
    status: int
    headers: dict[str, str]
    body: bytes


class TransportError(Exception):
    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass
class PaidAttempt:
    called: bool
    reason: str
    outcome: str
    route: str
    status: int | None = None
    title: str = ""
    text: str = ""
    final_url: str | None = None
    nbytes: int = 0
    reservation_id: int | None = None
    error: str = ""
    credit_weight: int = 0
    wall_kind: str | None = None


Transport = Callable[[dict], HttpExchange]


def challenge_kind(body: str) -> str | None:
    """Map a response body to a policy challenge name, or None when it is ordinary HTML."""
    if TURNSTILE_RX.search(body):
        return "challenge_turnstile"
    if RECAPTCHA_RX.search(body):
        return "challenge_recaptcha"
    if OTHER_CHALLENGE_RX.search(body):
        return "unsupported_challenge"
    return None


def read_exa_contents(payload: object) -> tuple[str, str] | None:
    """Title and text from an Exa contents payload. Search snippets are not accepted here."""
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    text = str(results[0].get("text") or "").strip()
    if len(text) < 200:
        return None
    return str(results[0].get("title") or "")[:300], text


def maybe_escalate(
    *,
    url: str,
    private: bool,
    wall_kind: str | None,
    policy,
    caps: Mapping[str, object],
    store,
    cfg: PaidConfig,
    page: PageContext | None,
    transport: Transport | None = None,
    resolver: guard.Resolver = guard.default_resolver,
    environ: Mapping[str, str] | None = None,
) -> PaidAttempt:
    """Return a paid attempt. `called` is false when no provider request was made."""
    provider = (cfg.provider or "").strip().lower()
    reason = _blocked(url, private, wall_kind, policy, caps, cfg, page, provider, environ)
    if reason:
        return PaidAttempt(False, reason, "blocked", provider)
    if page is None:
        return PaidAttempt(False, "no_page", "blocked", provider)
    if provider == "residential":
        try:
            guard.validate_target(url, resolver=resolver)
        except guard.EgressDenied as exc:
            return PaidAttempt(False, exc.reason, "blocked", provider)
    if provider == "zenrows":
        mode = _mode(cfg.zenrows_mode)
        if isinstance(mode, str):
            return PaidAttempt(False, mode, "blocked", provider)
        weight = mode[1]
    else:
        weight = cfg.max_proxy_bytes
    tariff = _tariff(caps, provider)
    try:
        reservation = pb.reserve(
            store,
            f"page-{page.page_id}-{page.paid_attempts + 1}",
            provider,
            tariff,
            caps=caps,
        )
    except pb.PaidBudgetExhausted as exc:
        return PaidAttempt(False, "budget", "blocked", provider, error=str(exc)[:160])
    _insert_paid(store, page, provider, weight, reservation.id, tariff)
    secrets = _secrets(cfg)
    try:
        if provider == "zenrows":
            attempt = _run_zenrows(url, policy, cfg, reservation.id, weight, transport or urllib_zenrows, secrets)
        else:
            attempt = _run_residential(
                url,
                cfg,
                reservation.id,
                transport or curl_residential,
                resolver,
                environ or {},
                secrets,
            )
    except Exception as exc:  # noqa: BLE001 - a provider error must not escape with a secret in it
        timeout = isinstance(exc, TransportError) and exc.timeout
        outcome = "unknown" if timeout else "transport_error"
        _settle(store, reservation.id, None, outcome, None)
        _finish_row(store, page.page_id, reservation.id, "outage", outcome)
        return PaidAttempt(
            True,
            outcome,
            "outage",
            "zenrows" if provider == "zenrows" else "resi_proxy",
            reservation_id=reservation.id,
            error=guard.redact(str(exc), secrets)[:300],
            credit_weight=weight,
        )
    _settle(store, reservation.id, attempt.actual_micro_usd, attempt.settle_outcome, attempt.task_ref)
    _finish_row(store, page.page_id, reservation.id, attempt.paid.outcome, attempt.settle_outcome)
    if attempt.paid.outcome == "walled":
        _mark_paid_failure(store, page.domain)
    return attempt.paid


@dataclass
class _Ran:
    paid: PaidAttempt
    actual_micro_usd: int | None
    settle_outcome: str
    task_ref: str | None


def _blocked(
    url: str,
    private: bool,
    wall_kind: str | None,
    policy,
    caps: Mapping[str, object],
    cfg: PaidConfig,
    page: PageContext | None,
    provider: str,
    environ: Mapping[str, str] | None,
) -> str | None:
    if provider not in {"zenrows", "residential"}:
        return "provider_not_configured"
    if private:
        return "private_topic"
    if page is None or page.page_id <= 0:
        return "no_page"
    if page.predicted_kind not in {"hub", "item"}:
        return "kind"
    if page.yield_score < cfg.min_yield:
        return "yield_below_0.30"
    if page.paid_failures >= 3:
        return "paid_failures"
    if page.paid_attempts >= cfg.max_retrievals:
        return "retrieval_cap"
    if wall_kind in TERMINAL_WALLS:
        return wall_kind or "terminal"
    decision = policy.decide(url, private=private, wall_kind=wall_kind)
    if "paid" not in decision.allowed_modes:
        return decision.denial_reason or "paid_not_allowed"
    if not _ceilings_open(caps, provider):
        return "ceilings_inactive"
    if provider == "zenrows":
        if not cfg.zenrows_api_key:
            return "credential_missing"
        if isinstance(_mode(cfg.zenrows_mode), str):
            return str(_mode(cfg.zenrows_mode))
    else:
        if not cfg.residential_proxy_url:
            return "credential_missing"
        proxy_reason = _proxy_blocked(cfg.residential_proxy_url, wall_kind, environ or {})
        if proxy_reason:
            return proxy_reason
    return None


def _ceilings_open(caps: Mapping[str, object], provider: str) -> bool:
    return bool(
        _positive(caps.get("daily_micro_usd"))
        and _positive(caps.get("monthly_micro_usd"))
        and _positive(caps.get("daily_retrieval_count"))
        and _tariff(caps, provider) > 0
    )


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _tariff(caps: Mapping[str, object], provider: str) -> int:
    tariffs = caps.get("tariffs")
    if not isinstance(tariffs, Mapping):
        return 0
    value = tariffs.get(provider)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


def _mode(name: str) -> tuple[str, int] | str:
    mode = (name or "auto").strip().lower()
    weight = CREDIT_WEIGHT.get(mode)
    if weight is None:
        return "undocumented_mode"
    return mode, weight


def _proxy_blocked(proxy_url: str, wall_kind: str | None, environ: Mapping[str, str]) -> str | None:
    if guard.proxy_env_names(environ):
        return "environment_proxy"
    try:
        parts = urlsplit(proxy_url)
    except ValueError:
        return "bad_proxy"
    if parts.scheme not in {"http", "socks4", "socks5"}:
        return "proxy_scheme"
    if not parts.username or not parts.password or not parts.hostname or not parts.port:
        return "proxy_not_authenticated"
    if wall_kind in CHALLENGE_KINDS:
        reason = ip_bound_block_reason(proxy_url)
        if reason:
            return reason
    return None


def ip_bound_block_reason(proxy_url: str, acquisition_seconds: int = 240) -> str:
    """Why this proxy cannot carry an IP-bound challenge. Empty means fail-closed sticky."""
    parts = urlsplit(proxy_url)
    userinfo = f"{parts.username or ''} {parts.password or ''}"
    if "sessid_oneip-" in userinfo:
        return ""
    if "sessid-" in userinfo and "sessid_oneip-" not in userinfo:
        return "silent_rotation"
    match = re.search(r"_ttl-([0-9]+[smhd])_session-([A-Za-z0-9]+)", userinfo)
    if match:
        seconds = ZENROWS_TTL_SEC.get(match.group(1))
        if seconds is None:
            return "undocumented_ttl"
        if seconds < acquisition_seconds:
            return "ttl_too_short"
        return "node_loss_unproven"
    return "not_sticky"


def _run_zenrows(url, policy, cfg: PaidConfig, reservation_id: int, weight: int, transport, secrets):
    mode_pair = _mode(cfg.zenrows_mode)
    assert not isinstance(mode_pair, str)
    mode, _weight = mode_pair
    params: dict[str, str] = {
        "apikey": cfg.zenrows_api_key or "",
        "url": url,
        "original_status": "true",
    }
    if mode == "auto":
        params["mode"] = "auto"
    elif mode == "js_render":
        params["js_render"] = "true"
    elif mode == "premium_proxy":
        params["premium_proxy"] = "true"
    elif mode == "protected":
        params["js_render"] = "true"
        params["premium_proxy"] = "true"
    exchange = transport(
        {
            "method": "GET",
            "endpoint": ZENROWS_ENDPOINT,
            "params": params,
            "timeout": cfg.timeout_sec,
            "max_bytes": cfg.max_body_bytes,
        }
    )
    return _interpret_zenrows(exchange, policy, url, cfg, reservation_id, weight, secrets)


def _interpret_zenrows(
    exchange: HttpExchange, policy, url: str, cfg: PaidConfig, reservation_id: int, weight: int, secrets
):
    code, task_ref = _provider_meta(exchange)
    text = ""
    final = _header(exchange.headers, "zr-final-url") or None
    wall: str | None = None
    if len(exchange.body) > cfg.max_body_bytes:
        outcome = "oversize"
    elif code:
        outcome = "failure"
        final = None
        text = ""
    elif not final or not _same_allowlist_entry(policy, url, final):
        outcome = "failure"
        text = ""
    else:
        outcome, wall, text = _classify_target(exchange.status, exchange.body)
    if outcome == "ok":
        settle_outcome = "awaiting_usage_reconciliation"
        actual = None
    elif code in NONBILLABLE_CODES:
        settle_outcome = CONFIRMED_NONBILLABLE
        actual = 0
    elif code in BILLED_CODES or code:
        settle_outcome = "billed_failure" if code in BILLED_CODES else "provider_error"
        actual = None
    else:
        settle_outcome = "unknown" if outcome == "failure" and not final else outcome
        actual = None
    paid = PaidAttempt(
        True,
        code or outcome,
        outcome,
        "zenrows",
        status=None if code else exchange.status,
        text=text if outcome == "ok" else "",
        final_url=final,
        nbytes=len(exchange.body),
        reservation_id=reservation_id,
        credit_weight=weight,
        wall_kind=wall,
        error="" if outcome == "ok" else guard.redact(code or outcome, secrets)[:300],
    )
    return _Ran(paid, actual, settle_outcome, task_ref)


def _classify_target(status: int, body: bytes) -> tuple[str, str | None, str]:
    decoded = body.decode("utf-8", "replace")
    kind = challenge_kind(decoded)
    if kind == "unsupported_challenge":
        return "walled", "challenge", ""
    if kind in CHALLENGE_KINDS:
        return "walled", "challenge", ""
    if status in {401, 403, 407, 451}:
        return "walled", "http", ""
    if status == 429 or status >= 500:
        return "outage", None, ""
    plain = _plain(decoded)
    if status >= 400 or len(plain) < 200:
        return "failure", None, ""
    return "ok", None, plain


def _run_residential(url, cfg: PaidConfig, reservation_id, transport, resolver, environ, secrets):
    proxy = cfg.residential_proxy_url or ""
    current = url
    seen = 0
    exchange: HttpExchange | None = None
    redirects = {301, 302, 303, 307, 308}
    try:
        while seen < 6:
            guard.validate_target(current, resolver=resolver)
            exchange = transport(
                {
                    "url": current,
                    "proxy": proxy,
                    "timeout": cfg.timeout_sec,
                    "max_bytes": min(cfg.max_body_bytes, cfg.max_proxy_bytes),
                    "environ": guard.scrub_proxy_env(environ),
                }
            )
            seen += 1
            if exchange.status not in redirects:
                break
            location = _header(exchange.headers, "location")
            if not location:
                break
            current = urljoin(current, location)
    except guard.EgressDenied as exc:
        paid = PaidAttempt(
            True,
            exc.reason,
            "ssrf_blocked",
            "resi_proxy",
            reservation_id=reservation_id,
            error=exc.reason,
        )
        return _Ran(paid, None, "unknown", None)
    if exchange is None or (seen >= 6 and exchange.status in redirects):
        paid = PaidAttempt(True, "redirects", "failure", "resi_proxy", reservation_id=reservation_id, error="redirects")
        return _Ran(paid, None, "unknown", None)
    outcome, wall, text = _classify_target(exchange.status, exchange.body)
    if len(exchange.body) > cfg.max_proxy_bytes:
        outcome, text, wall = "oversize", "", None
    paid = PaidAttempt(
        True,
        outcome,
        outcome,
        "resi_proxy",
        status=exchange.status,
        text=text if outcome == "ok" else "",
        final_url=current,
        nbytes=len(exchange.body),
        reservation_id=reservation_id,
        wall_kind=wall,
        error="" if outcome == "ok" else guard.redact(outcome, secrets)[:300],
    )
    actual = None
    settle = "awaiting_usage_reconciliation" if outcome == "ok" else "unknown"
    return _Ran(paid, actual, settle, None)


def _same_allowlist_entry(policy, start_url: str, final_url: str) -> bool:
    start, final = urlsplit(start_url), urlsplit(final_url)
    if start.scheme != final.scheme or (start.hostname or "").lower() != (final.hostname or "").lower():
        return False
    if (start.port or 0) != (final.port or 0):
        return False
    decision = policy.decide(final_url, private=False, wall_kind=None)
    return "paid" in decision.allowed_modes


def _provider_meta(exchange: HttpExchange) -> tuple[str | None, str | None]:
    raw = exchange.body.strip()
    code = None
    if raw.startswith(b"{") and len(raw) <= 20_000:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("code"), str) and PROVIDER_CODE.match(obj["code"]):
            code = obj["code"]
    parts: list[str] = []
    request_id = _header(exchange.headers, "x-request-id")
    credits = _header(exchange.headers, "x-request-credits")
    if request_id and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
        parts.append(f"req:{request_id}")
    if credits and re.fullmatch(r"\d{1,6}", credits.strip()):
        parts.append(f"credits:{credits.strip()}")
    return code, " ".join(parts) or None


def _header(headers: Mapping[str, str], name: str) -> str:
    want = name.lower()
    for key, value in headers.items():
        if key.lower() == want:
            return str(value).strip()
    return ""


def _plain(html: str) -> str:
    return " ".join(_TAG_RX.sub(" ", html).split())


def _secrets(cfg: PaidConfig) -> tuple[str, ...]:
    found = [cfg.zenrows_api_key or ""]
    parts = urlsplit(cfg.residential_proxy_url or "")
    if parts.password:
        found.append(parts.password)
    if parts.username:
        found.append(parts.username)
    return tuple(item for item in found if len(item) >= 4)


def _insert_paid(store, page: PageContext, provider: str, weight: int, reservation_id: int, tariff: int) -> None:
    route_provider = "zenrows" if provider == "zenrows" else "resi_proxy"
    with store.tx() as db:
        db.execute(
            "INSERT INTO paid_fetches(page_id, provider, cost_units, at, outcome, reservation_id, "
            "reserved_micro_usd, reservation_state) VALUES (?,?,?,strftime('%s','now'),'reserved',?,?,'reserved')",
            (page.page_id, route_provider, weight, reservation_id, tariff),
        )


def _finish_row(store, page_id: int, reservation_id: int, outcome: str, state: str) -> None:
    with store.tx() as db:
        db.execute(
            "UPDATE paid_fetches SET outcome=?, reservation_state=? WHERE page_id=? AND reservation_id=?",
            (outcome, state, page_id, reservation_id),
        )


def _mark_paid_failure(store, domain: str) -> None:
    with store.tx() as db:
        db.execute("UPDATE domains SET paid_failures=paid_failures+1 WHERE domain=?", (domain,))


def _settle(store, reservation_id: int, actual: int | None, outcome: str, task_ref: str | None) -> None:
    pb.settle(store, reservation_id, actual, outcome, task_ref)


def urllib_zenrows(spec: dict) -> HttpExchange:
    """GET the ZenRows endpoint. Redirects are not followed. The API key stays out of errors."""
    params = dict(spec["params"])
    secret = str(params.get("apikey") or "")
    query = urlencode(params)
    request = urllib.request.Request(ZENROWS_ENDPOINT + "?" + query, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())

    try:
        with opener.open(request, timeout=spec["timeout"]) as response:
            body = response.read(int(spec["max_bytes"]) + 1)
            headers = {key.lower(): value for key, value in response.headers.items()}
            return HttpExchange(response.status, headers, body)
    except urllib.error.HTTPError as exc:
        body = exc.read(int(spec["max_bytes"]) + 1) if exc.fp else b""
        headers = {key.lower(): value for key, value in (exc.headers.items() if exc.headers else [])}
        return HttpExchange(exc.code, headers, body)
    except Exception as exc:  # noqa: BLE001 - provider failures must not carry the request URL
        text = guard.redact(str(exc), (secret,))
        timeout = isinstance(exc, (TimeoutError, urllib.error.URLError)) and "timed out" in text.lower()
        raise TransportError(text or "zenrows transport failed", timeout=timeout) from None


def curl_residential(spec: dict) -> HttpExchange:
    """One explicit proxy request. Refuses to run while process-level proxy variables are set."""
    if guard.proxy_env_names(os.environ):
        raise TransportError("environment proxy variables are set")
    from curl_cffi import requests as cffi

    session = cffi.Session(impersonate="chrome")
    try:
        response = session.get(
            spec["url"],
            proxy=spec["proxy"],
            allow_redirects=False,
            timeout=spec["timeout"],
            stream=True,
        )
        buf = bytearray()
        for chunk in response.iter_content():
            buf += chunk
            if len(buf) > int(spec["max_bytes"]):
                break
        headers = {key.lower(): value for key, value in response.headers.items()}
        return HttpExchange(response.status_code, headers, bytes(buf))
    except Exception as exc:  # noqa: BLE001 - redact proxy credentials before the error leaves
        raise TransportError(guard.redact(str(exc), (spec.get("proxy") or "",))) from None
    finally:
        session.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None
