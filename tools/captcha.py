#!/usr/bin/env python3
"""2Captcha task lifecycle. submit() does not call the network unless a transport is passed.

One solver_tasks row per acquisition. Ambiguous, submitting, and unknown rows are not
resubmitted. Tokens, cookies, proxy passwords, and the client key are not stored.
Polling waits at least five seconds and stops at the deadline. A ready token is not access.
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
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit


def cost_to_micro_usd(cost: object) -> int | None:
    """Convert 2Captcha string cost (in USD) to exact integer micro-USD."""
    if cost is None or isinstance(cost, bool):
        return None
    if isinstance(cost, (int, float, Decimal)):
        try:
            dec = Decimal(str(cost))
            if dec < Decimal(0):
                return None
            return int((dec * Decimal(1_000_000)).to_integral_value())
        except (InvalidOperation, ValueError, TypeError):
            return None
    if not isinstance(cost, str) or not cost.strip():
        return None
    try:
        dec = Decimal(cost.strip())
        if dec < Decimal(0):
            return None
        return int((dec * Decimal(1_000_000)).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _cap_at_reservation(store, reservation_id: int, cost_micro: int | None) -> int | None:
    if cost_micro is None:
        return None
    try:
        res_row = store.one(
            "SELECT reserved_micro_usd FROM paid_budget_reservations WHERE id = ?",
            (reservation_id,),
        )
        if res_row is not None:
            reserved = int(res_row["reserved_micro_usd"] if hasattr(res_row, "__getitem__") else res_row[0])
            return min(cost_micro, reserved)
    except Exception:
        pass
    return cost_micro

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


# paid_fetch registers budget and egress under the same names. Load it first so this
# module does not replace those copies with a second exec.
if "snowball_paid_fetch" not in sys.modules:
    _load("snowball_paid_fetch", "paid_fetch.py")
paid = sys.modules["snowball_paid_fetch"]
pb = sys.modules["snowball_paid_budget"]
guard = sys.modules["snowball_egress_guard"]

CREATE_URL = "https://api.2captcha.com/createTask"
RESULT_URL = "https://api.2captcha.com/getTaskResult"
MIN_POLL_SEC = 5.0
TURNSTILE_MAX_AGE = 300
RECAPTCHA_MAX_AGE = 120
PROVIDER = "2captcha"
IMAGE_ERROR_IDS = frozenset({3, 4, 15, 16})
IMAGE_ERROR_CODES = frozenset(
    {
        "ERROR_ZERO_CAPTCHA_FILESIZE",
        "ERROR_TOO_BIG_CAPTCHA_FILESIZE",
        "ERROR_IMAGE_TYPE_NOT_SUPPORTED",
        "ERROR_UPLOAD",
    }
)
ALLOWED_API_DOMAINS = frozenset({"www.google.com", "google.com", "www.recaptcha.net", "recaptcha.net"})
_TASK_COLUMNS = frozenset(
    {
        "task_id",
        "submission_state",
        "cost_text",
        "last_poll_at",
        "cooldown_until",
        "created_at",
        "detail",
    }
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS solver_tasks (
    acquisition_id TEXT PRIMARY KEY,
    reservation_id INTEGER NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    task_id TEXT,
    submission_state TEXT NOT NULL,
    cost_text TEXT,
    last_poll_at REAL,
    cooldown_until REAL,
    created_at REAL NOT NULL,
    detail TEXT
)
"""


class TransportError(Exception):
    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass
class SolverConfig:
    client_key: str | None = None
    proxy_url: str | None = None


@dataclass
class Challenge:
    kind: str
    url: str
    sitekey: str
    session_id: str
    proxy_ref: str | None = None
    user_agent: str = ""
    action: str | None = None
    data: str | None = None
    pagedata: str | None = None
    api_domain: str | None = None
    invisible: bool = False

    def __repr__(self) -> str:
        return f"Challenge(kind={self.kind!r}, url={self.url!r}, session_id={self.session_id!r})"

    def __str__(self) -> str:
        return self.__repr__()


@dataclass
class SolverTask:
    provider: str
    task_id: int | None
    reservation_id: int
    submission_state: str
    acquisition_id: str = ""
    detail: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return (
            f"SolverTask(provider={self.provider!r}, task_id={self.task_id!r}, "
            f"reservation_id={self.reservation_id!r}, submission_state={self.submission_state!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass
class SolveResult:
    status: str
    token: str | None = field(default=None, repr=False)
    user_agent: str | None = field(default=None, repr=False)
    cost_micro_usd: int | None = None

    def __repr__(self) -> str:
        return f"SolveResult(status={self.status!r}, cost_micro_usd={self.cost_micro_usd!r})"

    def __str__(self) -> str:
        return self.__repr__()


Transport = Callable[[dict], object]


def submit(
    challenge: Challenge,
    cfg: SolverConfig,
    reservation,
    *,
    store,
    transport: Transport,
    now: float | None = None,
) -> SolverTask:
    """Create at most one provider task for this acquisition. `transport` is required."""
    moment = time.time() if now is None else float(now)
    secrets = _secrets(cfg, challenge)
    if challenge.session_id != reservation.acquisition_id:
        return _task(reservation, "mismatch", detail="session_mismatch")
    built, reason = _prepare(challenge, cfg)
    if reason or built is None:
        _release(store, reservation.id)
        return _task(reservation, "rejected", detail=_clip(reason or "rejected", secrets))
    _ensure(store)
    existing = _existing(store, reservation.acquisition_id)
    if existing is not None:
        return _resume(store, existing, built, cfg, reservation, transport, moment, secrets)
    try:
        _insert(store, reservation, challenge, moment)
    except sqlite3.IntegrityError:
        existing = _existing(store, reservation.acquisition_id)
        if existing is None:
            return _task(reservation, "unknown", detail="insert_race")
        return _resume(store, existing, built, cfg, reservation, transport, moment, secrets)
    return _post(store, reservation, built, cfg, transport, moment, secrets)


def poll(
    task: SolverTask,
    deadline_at: float,
    *,
    store,
    transport: Transport,
    now: float | None = None,
    min_interval: float = MIN_POLL_SEC,
    pause: float = 0.0,
    sleep: Callable[[float], None] | None = None,
) -> SolveResult:
    """One poll step. Returns without a request when the interval or deadline says to wait."""
    if pause > 0:
        (sleep or time.sleep)(pause)
    moment = time.time() if now is None else float(now)
    interval = MIN_POLL_SEC if min_interval < MIN_POLL_SEC else float(min_interval)
    row = _row_for_task(store, task)
    if row is None:
        return SolveResult(status="failure")
    state = str(row["submission_state"])
    if state in {"ready", "expired", "failed", "unsolvable", "rejected", "insufficient_balance", "unknown"}:
        cost_micro = cost_to_micro_usd(row["cost_text"]) if state == "ready" else None
        return SolveResult(status=state, cost_micro_usd=cost_micro)
    if state == "submitting":
        return SolveResult(status="unknown")
    if state == "not_accepted":
        return _poll_not_accepted(store, row, moment, deadline_at)
    if state != "submitted":
        return SolveResult(status=state or "unknown")
    if moment >= float(deadline_at):
        _settle_open(store, int(row["reservation_id"]), "deadline", _stored_task_id(row["task_id"]))
        return SolveResult(status="deadline")
    earliest = _earliest_poll(row, interval)
    if moment < earliest:
        return SolveResult(status="processing")
    return _poll_http(store, row, transport, moment, deadline_at)


def http_transport(spec: Mapping[str, object]) -> dict:
    """POST createTask or getTaskResult. Redirects are not followed. The client key is redacted."""
    url = str(spec.get("url") or "")
    if url not in {CREATE_URL, RESULT_URL}:
        raise TransportError("transport url is not a captcha endpoint")
    body = spec.get("body")
    secret = ""
    if isinstance(body, dict):
        secret = str(body.get("clientKey") or "")
    payload = json.dumps(body if isinstance(body, dict) else {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    timeout = spec.get("timeout")
    bad_timeout = isinstance(timeout, bool) or not isinstance(timeout, (int, float))
    timeout_sec = 30.0 if bad_timeout else float(timeout)
    try:
        with opener.open(request, timeout=timeout_sec) as response:
            raw = response.read(1_000_000)
            return {"status": int(response.status), "body": _json_body(raw)}
    except urllib.error.HTTPError as exc:
        raw = exc.read(1_000_000) if exc.fp else b""
        return {"status": int(exc.code), "body": _json_body(raw)}
    except Exception as exc:  # noqa: BLE001 - provider failures must not carry the client key
        text = guard.redact(str(exc), (secret,))
        timed_out = isinstance(exc, TimeoutError) or "timed out" in text.lower()
        raise TransportError(text or "captcha transport failed", timeout=timed_out) from None


def _prepare(challenge: Challenge, cfg: SolverConfig) -> tuple[dict | None, str | None]:
    if challenge.kind not in {"challenge_turnstile", "challenge_recaptcha"}:
        return None, "unsupported_kind"
    if not isinstance(challenge.sitekey, str) or not challenge.sitekey.strip():
        return None, "missing_sitekey"
    if not _http_url(challenge.url):
        return None, "bad_url"
    if challenge.kind == "challenge_turnstile":
        built = _turnstile_task(challenge, cfg)
    else:
        built = _recaptcha_task(challenge, cfg)
    if isinstance(built, str):
        return None, built
    if not _key(cfg):
        return None, "credential_missing"
    return built, None


def _turnstile_task(challenge: Challenge, cfg: SolverConfig) -> dict | str:
    action, data, pagedata = _blank(challenge.action), _blank(challenge.data), _blank(challenge.pagedata)
    present = [item is not None for item in (action, data, pagedata)]
    base = {"websiteURL": challenge.url, "websiteKey": challenge.sitekey}
    if not any(present):
        return {"type": "TurnstileTaskProxyless", **base}
    if not all(present):
        return "partial_turnstile"
    proxy = _proxy_fields(_proxy_url(challenge, cfg), challenge.session_id)
    if isinstance(proxy, str):
        return proxy
    return {
        "type": "TurnstileTask",
        **base,
        "action": action,
        "data": data,
        "pagedata": pagedata,
        **proxy,
    }


def _recaptcha_task(challenge: Challenge, cfg: SolverConfig) -> dict | str:
    proxy_fields: dict = {}
    if _google_search(challenge.url):
        proxy_fields = _proxy_fields(_proxy_url(challenge, cfg), challenge.session_id)
        if isinstance(proxy_fields, str):
            return "google_search_proxy" if proxy_fields == "proxy_required" else proxy_fields
        task_type = "RecaptchaV2Task"
    else:
        task_type = "RecaptchaV2TaskProxyless"
    if challenge.api_domain and challenge.api_domain not in ALLOWED_API_DOMAINS:
        return "api_domain"
    task: dict = {"type": task_type, "websiteURL": challenge.url, "websiteKey": challenge.sitekey}
    if challenge.invisible is True:
        task["isInvisible"] = True
    data_s = _blank(challenge.data)
    if data_s is not None:
        task["recaptchaDataSValue"] = data_s
    if challenge.api_domain:
        task["apiDomain"] = challenge.api_domain
    task.update(proxy_fields)
    return task


def _resume(store, row, built: dict, cfg, reservation, transport: Transport, moment: float, secrets) -> SolverTask:
    state = str(row["submission_state"])
    if state == "not_accepted":
        cooldown = row["cooldown_until"]
        if cooldown is not None and moment < float(cooldown):
            return _from_row(row)
        if not _claim_retry(store, reservation.acquisition_id, moment):
            fresh = _existing(store, reservation.acquisition_id)
            return _from_row(fresh) if fresh is not None else _task(reservation, "unknown")
        return _post(store, reservation, built, cfg, transport, moment, secrets)
    if state == "submitting":
        return _task(reservation, "unknown", detail="submitting")
    return _from_row(row)


def _post(store, reservation, built: dict, cfg, transport: Transport, moment: float, secrets) -> SolverTask:
    spec = {
        "url": CREATE_URL,
        "body": {"clientKey": _key(cfg), "task": built},
        "timeout": 30,
    }
    try:
        response = transport(spec)
    except Exception as exc:  # noqa: BLE001 - ambiguous create must not be retried or leak the key
        detail = _clip(str(exc), secrets)
        _update(store, reservation.acquisition_id, submission_state="unknown", detail=detail)
        return _task(reservation, "unknown", detail=detail)
    body = _response_body(response)
    if body is None:
        _update(store, reservation.acquisition_id, submission_state="unknown", detail="unrecognized_response")
        return _task(reservation, "unknown", detail="unrecognized_response")
    return _apply_create(store, reservation, body, moment, secrets)


def _apply_create(store, reservation, body: dict, moment: float, secrets) -> SolverTask:
    kind, wait = _create_disposition(body)
    detail = _clip(str(body.get("errorCode") or kind), secrets)
    acquisition = reservation.acquisition_id
    if kind == "accepted":
        task_id = _new_task_id(body)
        if task_id is None:
            _update(store, acquisition, submission_state="unknown", detail="bad_task_id")
            return _task(reservation, "unknown", detail="bad_task_id")
        _update(store, acquisition, submission_state="submitted", task_id=str(task_id), detail=None)
        return _task(reservation, "submitted", task_id=task_id)
    if kind == "not_accepted":
        _update(
            store,
            acquisition,
            submission_state="not_accepted",
            cooldown_until=moment + (wait or MIN_POLL_SEC),
            task_id=None,
            detail=detail,
        )
        return _task(reservation, "not_accepted", detail=detail)
    if kind in {"unsolvable", "insufficient_balance", "image_terminal", "rejected"}:
        state = "unsolvable" if kind == "unsolvable" else kind
        if kind == "image_terminal":
            state = "rejected"
        _update(store, acquisition, submission_state=state, task_id=None, detail=detail)
        _release(store, reservation.id)
        return _task(reservation, state, detail=detail)
    _update(store, acquisition, submission_state="unknown", detail=detail)
    return _task(reservation, "unknown", detail=detail)


def _create_disposition(body: dict) -> tuple[str, float | None]:
    """Map a createTask body to a state. Slot pressure can retry; balance and unsolvable cannot."""
    error_id = _error_id(body)
    if error_id is None:
        return "unknown", None
    code = body.get("errorCode") if isinstance(body.get("errorCode"), str) else ""
    if error_id == 0:
        return "accepted", None
    if error_id == 12 or code == "ERROR_CAPTCHA_UNSOLVABLE":
        return "unsolvable", None
    if code == "ERROR_ZERO_BALANCE":
        return "insufficient_balance", None
    if error_id == 2 or code == "ERROR_NO_SLOT_AVAILABLE":
        return "not_accepted", 5.0
    if error_id == 10 and not code:
        return "not_accepted", 60.0
    if error_id == 10:
        return "insufficient_balance", None
    if error_id in IMAGE_ERROR_IDS or code in IMAGE_ERROR_CODES:
        return "image_terminal", None
    if (
        code
        in {
            "ERROR_BAD_PARAMETERS",
            "ERROR_TASK_NOT_SUPPORTED",
            "ERROR_GOOGLEKEY",
            "ERROR_KEY_DOES_NOT_EXIST",
            "ERROR_PAGEURL",
            "ERROR_TASK_ABSENT",
            "ERROR_RECAPTCHA_INVALID_SITEKEY",
            "ERROR_BAD_PROXY",
            "ERROR_IP_NOT_ALLOWED",
            "ERROR_IP_BLOCKED",
            "ERROR_ACCOUNT_SUSPENDED",
        }
        or error_id in {1, 5, 11, 21, 22, 23, 31, 55, 110, 130}
    ):
        return "rejected", None
    return "unknown", None


def _poll_not_accepted(store, row, moment: float, deadline_at: float) -> SolveResult:
    if moment >= float(deadline_at) and row["task_id"] is None:
        _update(store, str(row["acquisition_id"]), submission_state="rejected", detail="deadline")
        _release(store, int(row["reservation_id"]))
        return SolveResult(status="deadline")
    return SolveResult(status="not_accepted")


def _poll_http(store, row, transport: Transport, moment: float, deadline_at: float | None = None) -> SolveResult:
    task_id = _stored_task_id(row["task_id"])
    if task_id is None:
        return SolveResult(status="unknown")
    timeout = 30.0
    if deadline_at is not None:
        remaining = float(deadline_at) - moment
        if remaining <= 0:
            _settle_open(store, int(row["reservation_id"]), "deadline", task_id)
            return SolveResult(status="deadline")
        timeout = max(0.1, min(30.0, remaining))
    spec = {"url": RESULT_URL, "body": {"taskId": task_id}, "timeout": timeout}
    try:
        response = transport(spec)
    except Exception as exc:  # noqa: BLE001 - a poll failure stays unknown and is not resubmitted
        detail = guard.redact(str(exc), ())[:300]
        _update(store, str(row["acquisition_id"]), submission_state="unknown", detail=detail, last_poll_at=moment)
        return SolveResult(status="unknown")
    body = _response_body(response)
    if body is None:
        _update(
            store,
            str(row["acquisition_id"]),
            submission_state="unknown",
            detail="unrecognized_response",
            last_poll_at=moment,
        )
        return SolveResult(status="unknown")
    return _apply_poll(store, row, body, moment)


def _apply_poll(store, row, body: dict, moment: float) -> SolveResult:
    disposition = _poll_disposition(body)
    acquisition = str(row["acquisition_id"])
    reservation_id = int(row["reservation_id"])
    task_id = _stored_task_id(row["task_id"])
    if disposition == "processing":
        _update(store, acquisition, last_poll_at=moment)
        return SolveResult(status="processing")
    if disposition == "unsolvable":
        _update(
            store, acquisition, submission_state="unsolvable", detail="ERROR_CAPTCHA_UNSOLVABLE", last_poll_at=moment
        )
        _release(store, reservation_id, task_id)
        return SolveResult(status="unsolvable")
    if disposition == "insufficient_balance":
        _update(
            store, acquisition, submission_state="insufficient_balance", detail="ERROR_ZERO_BALANCE", last_poll_at=moment
        )
        _release(store, reservation_id, task_id)
        return SolveResult(status="insufficient_balance")
    if disposition == "rejected":
        _update(store, acquisition, submission_state="rejected", detail="rejected", last_poll_at=moment)
        _release(store, reservation_id, task_id)
        return SolveResult(status="rejected")
    if disposition == "no_such_task":
        _update(store, acquisition, submission_state="failed", detail="no_such_task", last_poll_at=moment)
        _settle_open(store, reservation_id, "failed", task_id)
        return SolveResult(status="failed")
    if disposition != "ready":
        _update(store, acquisition, submission_state="unknown", detail="unrecognized_poll", last_poll_at=moment)
        return SolveResult(status="unknown")
    status, token, user_agent, cost_text = _validate_ready(body, str(row["kind"]), moment)
    cost_micro = cost_to_micro_usd(cost_text) if status == "ready" else None
    settled_micro = _cap_at_reservation(store, reservation_id, cost_micro) if status == "ready" else None
    _update(store, acquisition, submission_state=status, cost_text=cost_text, detail=None, last_poll_at=moment)
    outcome = {"ready": "ready", "expired": "expired"}.get(status, "token_rejected")
    _settle_open(store, reservation_id, outcome, task_id, actual_cost_micro_usd=settled_micro)
    if status != "ready":
        token = None
        user_agent = None
    return SolveResult(status=status, token=token, user_agent=user_agent, cost_micro_usd=cost_micro)


def _poll_disposition(body: dict) -> str:
    error_id = _error_id(body)
    if error_id is None:
        return "unknown"
    code = body.get("errorCode") if isinstance(body.get("errorCode"), str) else ""
    if error_id == 12 or code == "ERROR_CAPTCHA_UNSOLVABLE":
        return "unsolvable"
    if error_id in {14, 22} or code in {"ERROR_NO_SUCH_CAPCHA_ID", "ERROR_TASK_ABSENT", "ERROR_NO_SUCH_TASK"}:
        return "no_such_task"
    if code == "ERROR_ZERO_BALANCE" or (error_id == 10 and code):
        return "insufficient_balance"
    if (
        code in {"ERROR_KEY_DOES_NOT_EXIST", "ERROR_BAD_PARAMETERS", "ERROR_BAD_PROXY", "ERROR_ACCOUNT_SUSPENDED"}
        or error_id in {1, 5, 23, 31, 55, 110, 130}
    ):
        return "rejected"
    if error_id != 0:
        return "unknown"
    status = body.get("status")
    if status == "processing":
        return "processing"
    if status == "ready":
        return "ready"
    return "unknown"


def _validate_ready(body: dict, kind: str, moment: float) -> tuple[str, str | None, str | None, str | None]:
    cost = body.get("cost")
    cost_text = cost if isinstance(cost, str) else None
    solution = body.get("solution") if isinstance(body.get("solution"), dict) else {}
    token = _token(solution)
    user_agent = solution.get("userAgent")
    user_agent = user_agent if isinstance(user_agent, str) and user_agent else None
    end = _end_time(body)
    if token is None or user_agent is None or end is None:
        return "failure", None, None, cost_text
    limit = RECAPTCHA_MAX_AGE if kind == "challenge_recaptcha" else TURNSTILE_MAX_AGE
    if moment - end > limit:
        return "expired", None, None, cost_text
    return "ready", token, user_agent, cost_text


def _proxy_fields(proxy_url: str | None, session_id: str) -> dict | str:
    if not proxy_url:
        return "proxy_required"
    try:
        parts = urlsplit(proxy_url)
    except ValueError:
        return "bad_proxy"
    if parts.scheme not in {"http", "socks4", "socks5"}:
        return "proxy_scheme"
    if not parts.hostname or parts.port is None or not parts.username or not parts.password:
        return "proxy_not_authenticated"
    if parts.username == session_id or parts.password == session_id:
        return "session_proxy"
    if session_id and len(session_id) >= 8 and session_id in f"{parts.username}:{parts.password}":
        return "session_proxy"
    reason = paid.ip_bound_block_reason(proxy_url, acquisition_seconds=120)
    if reason:
        return reason
    return {
        "proxyType": parts.scheme,
        "proxyAddress": parts.hostname,
        "proxyPort": int(parts.port),
        "proxyLogin": parts.username,
        "proxyPassword": parts.password,
    }


def _ensure(store) -> None:
    with store.tx() as db:
        db.execute(_SCHEMA)


def _insert(store, reservation, challenge: Challenge, moment: float) -> None:
    with store.tx() as db:
        db.execute(_SCHEMA)
        db.execute(
            """
            INSERT INTO solver_tasks (
                acquisition_id, reservation_id, provider, kind, task_id, submission_state,
                cost_text, last_poll_at, cooldown_until, created_at, detail
            ) VALUES (?, ?, ?, ?, NULL, 'submitting', NULL, NULL, NULL, ?, NULL)
            """,
            (reservation.acquisition_id, reservation.id, PROVIDER, challenge.kind, moment),
        )


def _claim_retry(store, acquisition_id: str, moment: float) -> bool:
    with store.tx() as db:
        updated = db.execute(
            """
            UPDATE solver_tasks
               SET submission_state = 'submitting', created_at = ?, last_poll_at = NULL,
                   cooldown_until = NULL, detail = NULL
             WHERE acquisition_id = ?
               AND submission_state = 'not_accepted'
               AND (cooldown_until IS NULL OR cooldown_until <= ?)
            """,
            (moment, acquisition_id, moment),
        )
        return updated.rowcount == 1


def _update(store, acquisition_id: str, **fields) -> None:
    unknown = set(fields) - _TASK_COLUMNS
    if unknown:
        raise ValueError("column")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with store.tx() as db:
        db.execute(
            f"UPDATE solver_tasks SET {assignments} WHERE acquisition_id = ?",
            (*fields.values(), acquisition_id),
        )


def _existing(store, acquisition_id: str):
    names = store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='solver_tasks'")
    if names is None:
        return None
    return store.one("SELECT * FROM solver_tasks WHERE acquisition_id = ?", (acquisition_id,))


def _row_for_task(store, task: SolverTask):
    names = store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='solver_tasks'")
    if names is None:
        return None
    if task.acquisition_id:
        row = store.one("SELECT * FROM solver_tasks WHERE acquisition_id = ?", (task.acquisition_id,))
    else:
        row = store.one("SELECT * FROM solver_tasks WHERE reservation_id = ?", (task.reservation_id,))
    if row is None:
        return None
    if int(row["reservation_id"]) != int(task.reservation_id):
        return None
    stored = _stored_task_id(row["task_id"])
    if task.task_id is not None and stored is not None and stored != task.task_id:
        return None
    return row


def _release(store, reservation_id: int, task_id: int | None = None) -> None:
    ref = None if task_id is None else str(task_id)
    pb.settle(store, reservation_id, 0, pb.CONFIRMED_NONBILLABLE, ref)


def _settle_open(
    store,
    reservation_id: int,
    outcome: str,
    task_id: int | None,
    actual_cost_micro_usd: int | None = None,
) -> None:
    ref = None if task_id is None else str(task_id)
    pb.settle(store, reservation_id, actual_cost_micro_usd, outcome, ref)


def _task(reservation, state: str, *, task_id: int | None = None, detail: str = "") -> SolverTask:
    return SolverTask(
        provider=PROVIDER,
        task_id=task_id,
        reservation_id=reservation.id,
        submission_state=state,
        acquisition_id=reservation.acquisition_id,
        detail=detail,
    )


def _from_row(row) -> SolverTask:
    return SolverTask(
        provider=str(row["provider"]),
        task_id=_stored_task_id(row["task_id"]),
        reservation_id=int(row["reservation_id"]),
        submission_state=str(row["submission_state"]),
        acquisition_id=str(row["acquisition_id"]),
        detail=str(row["detail"] or ""),
    )


def _earliest_poll(row, interval: float) -> float:
    created = float(row["created_at"])
    last = row["last_poll_at"]
    if last is None:
        return created + interval
    return float(last) + interval


def _response_body(response: object) -> dict | None:
    if not isinstance(response, dict):
        return None
    body = response.get("body")
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _json_body(raw: bytes) -> dict | str:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return parsed if isinstance(parsed, dict) else ""


def _error_id(body: dict) -> int | None:
    value = body.get("errorId")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _new_task_id(body: dict) -> int | None:
    value = body.get("taskId")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _stored_task_id(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _end_time(body: dict) -> int | None:
    value = body.get("endTime")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _token(solution: Mapping[str, object]) -> str | None:
    for key in ("token", "gRecaptchaResponse"):
        value = solution.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _blank(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _http_url(url: object) -> bool:
    if not isinstance(url, str) or not url:
        return False
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        return False
    return bool(parts.hostname)


def _google_search(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "google.com" and (parts.path or "/").startswith("/search")


def _proxy_url(challenge: Challenge, cfg: SolverConfig) -> str | None:
    value = getattr(cfg, "proxy_url", None) or getattr(cfg, "residential_proxy_url", None)
    if isinstance(value, str) and value:
        return value
    if isinstance(challenge.proxy_ref, str) and "://" in challenge.proxy_ref:
        return challenge.proxy_ref
    return None


def _key(cfg: SolverConfig) -> str | None:
    value = getattr(cfg, "client_key", None) or getattr(cfg, "captcha_api_key", None)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _secrets(cfg: SolverConfig, challenge: Challenge) -> tuple[str, ...]:
    found = [_key(cfg) or ""]
    proxy = _proxy_url(challenge, cfg) or ""
    parts = urlsplit(proxy) if proxy else None
    if parts is not None and parts.password:
        found.append(parts.password)
    if parts is not None and parts.username:
        found.append(parts.username)
    if challenge.sitekey:
        found.append(challenge.sitekey)
    return tuple(item for item in found if item and len(item) >= 4)


def _clip(text: str, secrets: tuple[str, ...]) -> str:
    return guard.redact(text, secrets)[:300]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None
