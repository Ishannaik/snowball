"""Authenticated generic goal console API.

Bootstrap token exchange, server-side sessions, per-session CSRF, Origin/Host
verification, framing rejection, strict CSP, bounded JSON, row_version CAS and
actor+request-hashed idempotency. Every goal operation delegates to
generic_runtime.Service imported lazily; the goals database is never opened or
migrated by this module. Auth and idempotency persistence live in a sibling
SQLite database (``<db>.auth.sqlite``) created only on first use.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_BODY = 1024 * 1024
SESSION_COOKIE = "snowball_session"
SESSION_TTL = 12 * 3600
AUTH_DDL = """
CREATE TABLE IF NOT EXISTS generic_auth_bootstrap (
  token_hash TEXT PRIMARY KEY,
  used_at TEXT
);
CREATE TABLE IF NOT EXISTS generic_auth_sessions (
  sid_hash TEXT PRIMARY KEY,
  csrf TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  authenticated_at TEXT
);
CREATE TABLE IF NOT EXISTS generic_auth_idempotency (
  request_hash TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  status INTEGER NOT NULL,
  response_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS generic_auth_idem_scope ON generic_auth_idempotency(actor, idem_key);
"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class ApiError(Exception):
    """Structured failure: HTTP status, stable code, optional field/message."""

    def __init__(self, status: int, code: str, field: str | None = None, message: str | None = None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.field = field
        self.message = message or code


def auth_path(db_path: Path) -> Path:
    p = Path(db_path)
    return p.with_name(p.name + ".auth.sqlite")


def connect_auth(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(auth_path(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(AUTH_DDL)
    conn.commit()
    return conn


def bootstrap_token_hash(token: str) -> str:
    return _sha256_hex("snowball-bootstrap:" + token)


def store_bootstrap_token(conn: sqlite3.Connection, token: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO generic_auth_bootstrap(token_hash, used_at) VALUES(?, NULL)",
        (bootstrap_token_hash(token),),
    )
    conn.commit()


def consume_bootstrap_token(conn: sqlite3.Connection, token: str) -> bool:
    """One-use exchange: the exact hash row is deleted in one transaction."""
    token_hash = bootstrap_token_hash(token)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT 1 FROM generic_auth_bootstrap WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        conn.execute("DELETE FROM generic_auth_bootstrap WHERE token_hash=?", (token_hash,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def issue_session(conn: sqlite3.Connection, actor: str) -> tuple[str, str]:
    """Return (opaque sid, csrf token); only the hashed sid is stored."""
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO generic_auth_sessions(sid_hash, csrf, actor, created_at, authenticated_at)"
        " VALUES(?,?,?,?,?)",
        (_sha256_hex(sid), csrf, actor, _now(), _now()),
    )
    conn.commit()
    return sid, csrf


def _cookies(header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" in part:
            name, _, value = part.partition("=")
            out[name.strip()] = value.strip()
    return out


class GenericServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, db_path: Path, token: str, origin: str, runtime_factory=None):
        super().__init__(address, handler)
        self.db_path = Path(db_path)
        self.token = token
        self.origin = origin
        self.runtime_factory = runtime_factory

    # goal_id -> run thread bookkeeping; guarded by its own lock.
    run_locks: dict[str, threading.Lock] = {}


class GenericHandler(BaseHTTPRequestHandler):
    server: GenericServer

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    # --- response helpers -------------------------------------------------

    def _security_headers(self, nonce: str | None) -> None:
        nonce_part = f"'nonce-{nonce}'" if nonce else ""
        csp = (
            "default-src 'none'; script-src 'self' "
            + nonce_part
            + "; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")

    def _send(self, status: int, content_type: str, body: bytes, nonce: str | None = None, headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if nonce or content_type.startswith("text/html"):
            self._security_headers(nonce)
        else:
            self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers or []:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any], headers: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json", body, headers=headers)

    def _fail(self, status: int, code: str, field: str | None = None, message: str | None = None) -> None:
        error: dict[str, Any] = {"code": code}
        if field:
            error["field"] = field
        if message:
            error["message"] = message
        self._json(status, {"error": error})

    # --- request plumbing -------------------------------------------------

    def _origin_host_ok(self) -> tuple[bool, bool]:
        origin_ok = self.headers.get("Origin") == self.server.origin
        host_header = self.headers.get("Host") or ""
        origin = urlparse(self.server.origin)
        expected_host = origin.hostname or ""
        try:
            host = urlparse(f"//{host_header}") if host_header else None
        except ValueError:
            return origin_ok, False
        host_ok = bool(host and host.hostname == expected_host)
        return origin_ok, host_ok

    def _read_json_body(self) -> tuple[dict[str, Any], bytes]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise ApiError(400, "bad_length") from exc
        if length < 0 or length > MAX_BODY:
            raise ApiError(413, "body_too_large")
        raw = self.rfile.read(length)
        if len(raw) > MAX_BODY:
            raise ApiError(413, "body_too_large")
        if not raw:
            return {}, b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "malformed_json") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "object_body_required")
        return payload, raw

    def _session(self) -> dict[str, Any] | None:
        sid = _cookies(self.headers.get("Cookie")).get(SESSION_COOKIE)
        if not sid:
            return None
        conn = connect_auth(self.server.db_path)
        try:
            row = conn.execute(
                "SELECT sid_hash, csrf, actor, created_at, authenticated_at"
                " FROM generic_auth_sessions WHERE sid_hash=?",
                (_sha256_hex(sid),),
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            record["sid"] = sid
            return record
        finally:
            conn.close()

    def _require_session(self) -> dict[str, Any]:
        # A present Host that does not match the console origin is DNS-rebinding.
        _, host_ok = self._origin_host_ok()
        if not host_ok:
            raise ApiError(403, "host", message="Host header does not match the console origin")
        session = self._session()
        if session is None or session["authenticated_at"] is None:
            raise ApiError(401, "session_required")
        return session

    def _write_guards(self, session: dict[str, Any]) -> None:
        # Same-origin only: no CORS mode distinguishes a read from a write.
        origin_ok, _ = self._origin_host_ok()
        if not origin_ok:
            raise ApiError(403, "origin", message="cross-origin writes are refused")
        supplied = self.headers.get("X-CSRF-Token") or ""
        if not _constant_eq(supplied, session["csrf"]):
            raise ApiError(403, "csrf", message="CSRF token does not match this session")

    def _request_hash(self, actor: str, idem_key: str, body_hash: str) -> str:
        normalized = self.path.split("?", 1)[0]
        parts = [actor, self.command, normalized, idem_key, body_hash]
        return _sha256_hex("\x1f".join(parts))

    def _idempotency_lookup(self, request_hash: str) -> dict[str, Any] | None:
        conn = connect_auth(self.server.db_path)
        try:
            row = conn.execute(
                "SELECT status, response_json FROM generic_auth_idempotency WHERE request_hash=?",
                (request_hash,),
            ).fetchone()
            if row is None:
                return None
            return {"status": row["status"], "payload": json.loads(row["response_json"])}
        finally:
            conn.close()

    def _idempotency_scope_lookup(self, actor: str, idem_key: str) -> dict[str, Any] | None:
        conn = connect_auth(self.server.db_path)
        try:
            row = conn.execute(
                "SELECT request_hash, body_hash, status, response_json FROM generic_auth_idempotency"
                " WHERE actor=? AND idem_key=? ORDER BY rowid DESC LIMIT 1",
                (actor, idem_key),
            ).fetchone()
            if row is None:
                return None
            return {"request_hash": row["request_hash"], "body_hash": row["body_hash"], "status": row["status"], "payload": json.loads(row["response_json"])}
        finally:
            conn.close()

    def _idempotency_record(self, request_hash: str, actor: str, idem_key: str, body_hash: str) -> None:
        conn = connect_auth(self.server.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT 1 FROM generic_auth_idempotency WHERE request_hash=?", (request_hash,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO generic_auth_idempotency"
                    "(request_hash, actor, method, path, idem_key, body_hash, status, response_json)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (request_hash, actor, self.command, self.path.split("?", 1)[0], idem_key, body_hash, 0, "{}"),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _idempotency_complete(self, request_hash: str, status: int, payload: dict[str, Any]) -> None:
        conn = connect_auth(self.server.db_path)
        try:
            conn.execute(
                "UPDATE generic_auth_idempotency SET status=?, response_json=? WHERE request_hash=?",
                (status, json.dumps(payload, ensure_ascii=False), request_hash),
            )
            conn.commit()
        finally:
            conn.close()

    # --- runtime helpers --------------------------------------------------

    def _runtime(self) -> Any:
        factory = self.server.runtime_factory
        if factory is not None:
            return factory(self.server.db_path)
        import importlib

        module = importlib.import_module("tools.generic_runtime")
        return module.Service(self.server.db_path)

    def _run_in_background(self, goal_id: str) -> bool:
        """Start one runtime run thread for the goal; False if one is active."""
        lock = threading.Lock()
        guard = self.server.run_locks.setdefault(goal_id, lock)
        with guard:
            current = self.server.run_locks.get(goal_id + ":active")
        if current is not None and current.is_alive():
            return False
        thread = threading.Thread(target=self._run_supervised, args=(goal_id,), daemon=True)
        with guard:
            self.server.run_locks[goal_id + ":active"] = thread
        if thread is not self.server.run_locks.get(goal_id + ":active"):
            return False
        thread.start()
        return True

    def _run_supervised(self, goal_id: str) -> None:
        runtime = self._runtime()
        try:
            runtime.run(goal_id)
        except Exception:
            pass  # run() already records outcomes; worker exits silently
        finally:
            marker = self.server.run_locks.get(goal_id + ":active")
            if marker is not None and marker is threading.current_thread():
                self.server.run_locks.pop(goal_id + ":active", None)

    # --- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path == "/" or path == "/index.html":
                self._serve_app()
            elif path.startswith("/assets/"):
                self._serve_asset(path[len("/assets/"):])
            elif path == "/api/session":
                self._get_session()
            elif path == "/api/goals":
                self._get_goals()
            elif path.startswith("/api/goals/"):
                goal_id = path[len("/api/goals/"):]
                self._get_goal(goal_id)
            else:
                raise ApiError(404, "not_found")
        except ApiError as exc:
            self._fail(exc.status, exc.code, exc.field, exc.message)
        except Exception:
            self._fail(500, "internal")

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/session":
                self._post_session()
                return
            if path == "/api/logout":
                self._post_logout()
                return
            session = self._require_session()
            self._write_guards(session)
            if path == "/api/goals":
                self._post_goals(session)
            elif path.startswith("/api/goals/"):
                parts = path[len("/api/goals/"):].split("/")
                if len(parts) != 2:
                    raise ApiError(404, "not_found")
                goal_id, action = parts
                self._post_goal_action(session, goal_id, action, path)
            else:
                raise ApiError(404, "not_found")
        except ApiError as exc:
            self._fail(exc.status, exc.code, exc.field, exc.message)
        except BrokenPipeError:
            pass
        except Exception:
            self._fail(500, "internal")

    # --- static app -------------------------------------------------------

    def _serve_app(self) -> None:
        from tools import generic_ui

        nonce = secrets.token_urlsafe(16)
        html = generic_ui.render(None, nonce=nonce)
        self._send(200, "text/html; charset=utf-8", html.encode("utf-8"), nonce=nonce)

    def _serve_asset(self, name: str) -> None:
        from tools import generic_ui

        if name == "app.js":
            self._send(200, "application/javascript; charset=utf-8", generic_ui.APP_JS.encode("utf-8"))
            return
        if name == "app.css":
            self._send(200, "text/css; charset=utf-8", generic_ui.APP_CSS.encode("utf-8"))
            return
        raise ApiError(404, "not_found")

    # --- session routes ---------------------------------------------------

    def _get_session(self) -> None:
        session = self._require_session()
        self._json(200, {"csrf": session["csrf"], "actor": session["actor"]})

    def _post_session(self) -> None:
        payload, _ = self._read_json_body()
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise ApiError(400, "token", field="token")
        conn = connect_auth(self.server.db_path)
        try:
            ok = consume_bootstrap_token(conn, token)
            if not ok:
                raise ApiError(401, "invalid_token")
            sid, csrf = issue_session(conn, "local")
        finally:
            conn.close()
        self._json(
            200,
            {"actor": "local", "csrf": csrf},
            headers=[("Set-Cookie", f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}")],
        )

    def _post_logout(self) -> None:
        session = self._require_session()
        self._write_guards(session)
        conn = connect_auth(self.server.db_path)
        try:
            conn.execute("DELETE FROM generic_auth_sessions WHERE sid_hash=?", (_sha256_hex(session["sid"]),))
            conn.commit()
        finally:
            conn.close()
        clear = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        self._json(200, {"ok": True}, headers=[("Set-Cookie", clear)])

    # --- goal routes ------------------------------------------------------

    def _get_goals(self) -> None:
        self._require_session()
        runtime = self._runtime()
        goals = runtime.list()
        self._json(200, {"goals": goals})

    def _get_goal(self, goal_id: str) -> None:
        self._require_session()
        runtime = self._runtime()
        try:
            view = runtime.view(goal_id)
        except (RuntimeError, ValueError) as exc:
            mapped = map_runtime_error(exc)
            raise ApiError(mapped[0], mapped[1], getattr(exc, "field", None), getattr(exc, "message", mapped[1])) from exc
        self._json(200, view)

    def _post_goals(self, session: dict[str, Any]) -> None:
        payload, raw = self._read_json_body()
        idem_key = self.headers.get("Idempotency-Key") or ""
        title = payload.get("title")
        goal_id = payload.get("id")
        if not isinstance(title, str) or not title.strip():
            raise ApiError(400, "title", field="title")
        if goal_id is not None and (not isinstance(goal_id, str) or not goal_id.strip()):
            raise ApiError(400, "goal_id", field="id")
        body_hash = hashlib.sha256(raw if raw else json.dumps({}).encode()).hexdigest()
        request_hash = self._request_hash(session["actor"], idem_key, body_hash)
        if idem_key:
            prior_scope = self._idempotency_scope_lookup(session["actor"], idem_key)
            if prior_scope is not None and prior_scope["body_hash"] != body_hash:
                raise ApiError(409, "idempotency_mismatch", message="Idempotency-Key was already used with a different request")
            prior = self._idempotency_lookup(request_hash)
            if prior is not None and prior["status"] > 0:
                self._json(prior["status"], prior["payload"], headers=[("X-Idempotent-Replay", "1")])
                return
            self._idempotency_record(request_hash, session["actor"], idem_key, body_hash)
        runtime = self._runtime()
        try:
            view = runtime.create(title.strip(), goal_id.strip() if isinstance(goal_id, str) else None)
        except ApiError:
            raise
        except Exception as exc:
            mapped = map_nested_error(exc)
            if idem_key:
                self._idempotency_complete(request_hash, mapped[0], {"error": {"code": mapped[1], "field": getattr(exc, "field", None), "message": getattr(exc, "message", mapped[1])}})
            raise ApiError(mapped[0], mapped[1], getattr(exc, "field", None), getattr(exc, "message", None)) from exc
        response = {"goal": view["goal"], "draft": view.get("draft")}
        if idem_key:
            self._idempotency_complete(request_hash, 200, response)
        self._json(200, response)

    def _post_goal_action(self, session: dict[str, Any], goal_id: str, action: str, path: str) -> None:
        payload, raw = self._read_json_body()
        idem_key = self.headers.get("Idempotency-Key") or ""
        body_hash = hashlib.sha256(raw if raw else b"").hexdigest()
        request_hash = self._request_hash(session["actor"], idem_key, body_hash)
        if idem_key:
            prior_scope = self._idempotency_scope_lookup(session["actor"], idem_key)
            if prior_scope is not None and prior_scope["body_hash"] != body_hash:
                raise ApiError(409, "idempotency_mismatch", message="Idempotency-Key was already used with a different request")
            prior = self._idempotency_lookup(request_hash)
            if prior is not None and prior["status"] > 0:
                self._json(prior["status"], prior["payload"], headers=[("X-Idempotent-Replay", "1")])
                return
            self._idempotency_record(request_hash, session["actor"], idem_key, body_hash)
        runtime = self._runtime()
        try:
            status, response = self._dispatch_action(runtime, goal_id, action, payload)
        except ApiError:
            raise
        except Exception as exc:
            mapped = map_nested_error(exc)
            if idem_key:
                self._idempotency_complete(request_hash, mapped[0], {"error": {"code": mapped[1], "field": getattr(exc, "field", None), "message": getattr(exc, "message", mapped[1])}})
            raise ApiError(mapped[0], mapped[1], getattr(exc, "field", None), getattr(exc, "message", None)) from exc
        if idem_key:
            self._idempotency_complete(request_hash, status, response)
        self._json(status, response)

    def _dispatch_action(self, runtime: Any, goal_id: str, action: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        row_version = payload.get("row_version")
        if row_version is not None and (isinstance(row_version, bool) or not isinstance(row_version, int)):
            raise ApiError(400, "row_version", field="row_version")
        if action == "answer":
            view = runtime.answer(goal_id, payload.get("question_id"), payload.get("answer"), row_version)
            return 200, view
        if action == "consent":
            view = runtime.consent(goal_id, payload.get("recipient"), payload.get("refs"), payload.get("allowance"), payload.get("recipients"), row_version)
            return 200, view
        if action == "interview":
            return 200, runtime.interview(goal_id)
        if action == "propose":
            options = payload.get("options")
            result = runtime.propose(goal_id, options if isinstance(options, dict) else None)
            return 200, result
        if action == "approve":
            body = payload.get("payload")
            body_hash = payload.get("body_hash")
            if not isinstance(body_hash, str) or len(body_hash) != 64:
                raise ApiError(400, "body_hash", field="body_hash", message="the exact reviewed body hash is required")
            if not isinstance(body, dict):
                raise ApiError(400, "payload", field="payload")
            return 200, runtime.approve(goal_id, body, row_version, body_hash)
        if action == "preview":
            # Readonly canonicalization: parent Service.preview builds the exact
            # version a user-reviewed JSON would approve as, with its true hash.
            body = payload.get("payload")
            if not isinstance(body, dict):
                raise ApiError(400, "payload", field="payload")
            return 200, runtime.preview(goal_id, body, row_version)
        if action == "grant":
            limits = payload.get("limits")
            view = runtime.grant(goal_id, limits if isinstance(limits, dict) else None, row_version)
            return 200, view
        if action in {"start", "pause", "resume", "stop", "revise"}:
            grant_id = payload.get("grant_id")
            if grant_id is not None and (isinstance(grant_id, bool) or not isinstance(grant_id, int)):
                raise ApiError(400, "grant_id", field="grant_id")
            if action == "start":
                return 200, runtime.start(goal_id, row_version, grant_id)
            return 200, getattr(runtime, action)(goal_id, row_version)
        if action == "run":
            view = runtime.view(goal_id)
            if view["goal"]["state"] != "running":
                raise ApiError(409, "not_running", message="start the goal before running research")
            started = self._run_in_background(goal_id)
            if not started:
                raise ApiError(409, "run_in_progress", message="a research run is already active for this goal")
            return 202, {"goal": view["goal"], "run": "started"}
        raise ApiError(404, "unknown_action")


def map_nested_error(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, RuntimeError) and not hasattr(exc, "field"):
        return 500, "internal"
    if isinstance(exc, TypeError) and not hasattr(exc, "field"):
        # A programming/integration error, not user input.
        return 500, "internal"
    field = getattr(exc, "field", None)
    # Same units as generic_contract.LIMIT_UNITS; a plain literal avoids a
    # sibling-module import cycle in the error path.
    if isinstance(field, str) and field in {
        "expansion_tasks", "sources", "source_bytes", "model_calls",
        "tool_calls", "model_tokens", "elapsed_sec", "concurrency",
    }:
        return 409, "budget_exceeded"
    if field in {"row_version", "body_hash", "snapshot_hash"}:
        code = "stale_row_version" if field == "row_version" else "hash_mismatch"
        return 409, code
    if field == "goal_id":
        return 404, "goal_not_found"
    if field in {"grant", "grant_id"}:
        return 409, "grant_state"
    if field == "state":
        return 409, "invalid_state"
    if field == "recipient":
        return 409, "recipient"
    if field in {"clearance", "provenance", "reviewer"}:
        return 409, "privacy"
    if field in {"binding", "claim_id", "source_id", "context_limit", "criterion_id", "evidence_ids", "public_context_ids"}:
        return 409, "invalid_state"
    if field in {"quote", "source_kind", "question_id", "query", "query_text", "path", "max_waves", "proposal", "reviews", "claims", "tasks", "tool", "url"}:
        return 400, "invalid_input"
    if field == "model_tokens":
        return 409, "budget_exceeded"
    if hasattr(exc, "field"):
        return 400, "invalid_input"
    return 400, "invalid_request"


def map_runtime_error(exc: Exception) -> tuple[int, str]:
    field = getattr(exc, "field", None)
    if field == "goal_id":
        return 404, "goal_not_found"
    if field == "state":
        return 409, "invalid_state"
    return 500, "internal"


def serve(db_path: Path, token: str, origin: str, port: int = 0, *, runtime_factory=None) -> GenericServer:
    db_path = Path(db_path)
    conn = connect_auth(db_path)
    try:
        store_bootstrap_token(conn, token)
    finally:
        conn.close()
    server = GenericServer(("127.0.0.1", port), GenericHandler, db_path, token, origin, runtime_factory)
    return server
