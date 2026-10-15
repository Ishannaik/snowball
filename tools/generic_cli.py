#!/usr/bin/env python3
"""Generic Snowball CLI. Approval, grant, and start are separate operations."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_ERROR = 2
USAGE_HINT = "usage"


class CliError(Exception):
    """Field-scoped user error; messages never echo private values."""

    def __init__(self, error: str, field: str | None = None, message: str | None = None, exit_code: int = EXIT_ERROR):
        self.error = error
        self.field = field
        self.message = message
        self.exit_code = exit_code
        super().__init__(message or error)


def _load(name: str):
    """Module invoked directly still imports from repo root (contract style)."""
    key = f"snowball_generic_{name}_cli"
    existing = sys.modules.get(key)
    if existing is not None:
        return existing
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(key, ROOT / "tools" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise CliError("module_unavailable", field=name, message=f"{name} module is not installed")
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    except CliError:
        raise
    except (ImportError, OSError) as exc:
        raise CliError("module_unavailable", field=name, message=f"{name} module failed to load ({type(exc).__name__})") from exc
    return module


def _runtime():
    """Lazy import: Service pulls roles/acquisition only when actually used."""
    key = "snowball_generic_runtime_cli"
    return sys.modules.get(key) or _load("generic_runtime")


def _select(root: argparse.Namespace, key: str) -> Any:
    value = getattr(root, key)
    if value is None:
        raise CliError(USAGE_HINT, field=key, message="the selected command needs --goal-id")
    return value


def _read_json_path(path: Path | None, field: str, *, required: bool = False) -> Any:
    if path is None:
        if required:
            raise CliError(USAGE_HINT, field=field, message=f"this command needs --{field.replace('_', '-')}")
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError("io", field=field, message=f"{field} file unreadable ({type(exc).__name__})") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError("json", field=field, message="payload is not valid JSON") from exc


def _json_bytes(path: Path | None, field: str) -> Any:
    return _read_json_path(path, field, required=True)


def _service(db: Path, roles: Any = None, acquisition: Any = None) -> Any:
    return _runtime().Service(db, roles=roles, acquisition=acquisition)


def cmd_init(root: argparse.Namespace) -> dict:
    return _service(root.db).create(root.title, _select(root, "goal_id") or None)


def cmd_list(root: argparse.Namespace) -> dict:
    return {"goals": _service(root.db).list()}


def cmd_status(root: argparse.Namespace) -> dict:
    if root.goal_id:
        return _service(root.db).view(_select(root, "goal_id"))
    return {"goals": _service(root.db).list()}


def _translated(exc: Exception) -> CliError:
    """Upstream field errors surface with their field; nothing secret enters argv/stderr."""
    field = getattr(exc, "field", None)
    message = getattr(exc, "message", None)
    return CliError(
        type(exc).__name__,
        field=str(field) if field else None,
        message=message if isinstance(message, str) and message else None,
    )


def cmd_answer(root: argparse.Namespace) -> dict:
    value = _json_bytes(root.value, "value")
    raw_label = root.label if root.label else None
    if root.state == "confirmed" and value is None:
        raise CliError("json", field="value", message="a confirmed answer needs a JSON value")
    state = root.state
    label = raw_label
    inherited_public = False
    if not label:
        # Inherit the question's stored label exactly; a public question's [] must be
        # sent explicitly or validate_answer would default the answer to private.
        question_view = _service(root.db).view(_select(root, "goal_id"))
        question = next(
            (q for q in (question_view.get("draft") or {}).get("questions", []) if q["id"] == root.question_id),
            None,
        )
        if question is not None:
            stored = question.get("label")
            if isinstance(stored, list):
                label = list(stored)
                inherited_public = stored == []
    entry: dict[str, Any] = {"question_id": root.question_id, "state": state, "origin": "user_stated"}
    if state == "confirmed":
        entry["value"] = value
    if label or inherited_public:
        entry["label"] = label or []
    return _service(root.db).answer(root.goal_id, root.question_id, entry, root.row_version)


def cmd_consent(root: argparse.Namespace) -> dict:
    citations = _json_bytes(root.refs, "refs")
    if not isinstance(citations, list) or not citations or not all(isinstance(ref, str) for ref in citations):
        raise CliError("json", field="refs", message="refs must be a JSON list of question ids")
    if root.calls < 1 or root.tokens < 1:
        raise CliError("usage", field="calls", message="interview consent needs a finite positive allowance")
    allowance = {"model_calls": root.calls, "model_tokens": root.tokens}
    view = _service(root.db).view(_select(root, "goal_id"))
    recipients = (view.get("draft") or {}).get("recipients") or {}
    if root.recipients is not None:
        explicit = _json_bytes(root.recipients, "recipients")
        if not isinstance(explicit, dict) or not explicit:
            raise CliError("json", field="recipients", message="recipients must be a nonempty JSON object")
        recipients = explicit
    if not recipients:
        # Approved recipients are never defaulted; the operator names them explicitly.
        raise CliError(
            "usage",
            field="recipients",
            message="the draft has no approved recipients; pass --recipients PATH with the explicit JSON map",
        )
    return _service(root.db).consent(
        root.goal_id, root.recipient, citations, allowance, recipients, root.row_version,
    )


def cmd_interview(root: argparse.Namespace) -> dict:
    return _service(root.db).interview(_select(root, "goal_id"))


def cmd_propose(root: argparse.Namespace) -> dict:
    options = _read_json_path(root.options, "options") or None
    if options is not None and not isinstance(options, dict):
        raise CliError("json", field="options", message="options must be a JSON object")
    return _service(root.db).propose(root.goal_id, options)


def cmd_approve(root: argparse.Namespace) -> dict:
    payload = _json_bytes(root.payload, "payload")
    if not isinstance(payload, dict):
        raise CliError("json", field="payload", message="payload must be a JSON contract object")
    if root.row_version is None:
        raise CliError(USAGE_HINT, field="row-version", message="approve requires the reviewed --row-version")
    view = _service(root.db).approve(root.goal_id, payload, root.row_version, root.body_hash)
    return view


def cmd_grant(root: argparse.Namespace) -> dict:
    limits = _read_json_path(root.limits, "limits")
    if limits is not None and not isinstance(limits, dict):
        raise CliError("json", field="limits", message="limits must be a JSON object")
    return _service(root.db).grant(root.goal_id, limits, root.row_version)


def _simple(name: str):
    def command(root: argparse.Namespace) -> dict:
        service = _service(root.db)
        method = getattr(service, name)
        if name == "start":
            return method(root.goal_id, root.row_version, root.grant_id)
        return method(root.goal_id, root.row_version)
    return command


def cmd_run(root: argparse.Namespace) -> dict:
    if root.max_waves < 1 or root.max_waves > 1000:
        raise CliError("usage", field="max-waves", message="max-waves must be between 1 and 1000")
    return _service(root.db).run(root.goal_id, root.max_waves)


def _write_private(path: Path, text: str) -> None:
    """Write a secret with directory 0700 / file 0600; POSIX semantics elsewhere."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def cmd_serve(root: argparse.Namespace) -> int:
    api = _load("generic_api")
    origin = root.origin or f"http://127.0.0.1:{root.port or 8989}"
    interactive = sys.stdout.isatty()
    env_token = os.environ.get("SNOWBALL_CONSOLE_BOOTSTRAP_TOKEN")
    token = root.token or env_token or secrets.token_urlsafe(24)
    if not root.token and not env_token:
        if interactive:
            # Random bootstrap: printed once to the terminal, never logged again.
            print(token, file=sys.stdout, flush=True)
        else:
            _write_private(ROOT / "state" / "bootstrap-token", token + "\n")
            print("bootstrap token written to state/bootstrap-token (0600)", file=sys.stderr, flush=True)
    server = api.serve(root.db, token, origin, root.port or 0)
    port = server.server_address[1]
    if root.port and port != root.port:
        server.server_close()
        raise CliError("io", field="port", message="selected port is unavailable")
    print(f"listening on http://127.0.0.1:{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK


def cmd_diagnostics(root: argparse.Namespace) -> dict:
    # Read-only health: no migrations, no schema changes.
    return _runtime().Service(root.db).diagnostics()


def cmd_ask(root: argparse.Namespace) -> dict:
    return _runtime().Service(root.db).ask(_select(root, "goal_id"), root.query)


def cmd_export(root: argparse.Namespace) -> dict:
    return _runtime().Service(root.db).export(root.goal_id, root.out)


def cmd_migrate(root: argparse.Namespace) -> dict:
    migration = _load("generic_migration")
    try:
        return migration.migrate(root.db, root.root, root.backup_dir)
    except (ValueError, OSError, RuntimeError, AttributeError) as exc:
        # Includes absent store exports: schema upgrade is unavailable, not silently skipped.
        raise CliError("migration_blocked", message=f"migration refused ({type(exc).__name__})") from exc


def cmd_restore(root: argparse.Namespace) -> dict:
    migration = _load("generic_migration")
    if root.destination.exists() and any(root.destination.iterdir()):
        raise CliError("usage", field="destination", message="restore needs a new empty storage root")
    try:
        return migration.restore(root.bundle, root.destination)
    except (ValueError, OSError, RuntimeError) as exc:
        raise CliError("restore_blocked", message=f"restore refused ({type(exc).__name__})") from exc


def cmd_retention(root: argparse.Namespace) -> dict:
    migration = _load("generic_migration")
    try:
        return migration.retain(root.root, root.keep)
    except (ValueError, OSError, RuntimeError) as exc:
        raise CliError("retention_blocked", message=f"retention refused ({type(exc).__name__})") from exc


def cmd_supervise(root: argparse.Namespace) -> dict:
    supervisor = _load("generic_supervisor")
    try:
        return supervisor.supervise(
            root.db, root.goal_id, max_waves=root.max_waves, poll_sec=root.poll_sec,
        )
    except (ValueError, OSError, RuntimeError) as exc:
        raise CliError("supervisor_failed", message=f"supervisor stopped ({type(exc).__name__})") from exc


def _add_goal(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--goal-id", required=True)


def _add_cas(parser: argparse.ArgumentParser, required: bool = False) -> None:
    parser.add_argument("--row-version", type=int, default=None, required=required)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snowball generic", description="Generic goal interviews, approvals, bounded research runs.")
    parser.add_argument("--db", type=Path, default=None, help="SQLite database (documented position: before or after the subcommand)")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, **kwargs):
        child = sub.add_parser(name, **kwargs)
        # Common --db works in the documented position and after the verb.
        child.add_argument("--db", type=Path, default=None)
        return child

    p = command("init", help="Create a goal and its opening interview draft")
    p.add_argument("--goal-id")
    p.add_argument("--title", required=True)
    p.set_defaults(func=cmd_init)

    p = command("list", help="List goals")
    p.set_defaults(func=cmd_list)

    p = command("status", help="Show one goal or list all")
    p.add_argument("--goal-id")
    p.set_defaults(func=cmd_status)

    p = command("answer", help="Answer, confirm, decline, or mark unknown a draft question")
    _add_goal(p)
    _add_cas(p)
    p.add_argument("--question-id", required=True)
    p.add_argument("--value", type=Path, help="Local JSON value file; values never appear in argv")
    p.add_argument("--state", default="confirmed", choices=["confirmed", "unknown", "declined", "unconfirmed"])
    p.add_argument("--label", action="append", help="Disclosure class; defaults to the question's stored label")
    p.set_defaults(func=cmd_answer)

    p = command("consent", help="Authorize a named recipient for exact question references")
    _add_goal(p)
    _add_cas(p)
    p.add_argument("--recipient", required=True)
    p.add_argument("--refs", type=Path, required=True, help="JSON list of question ids")
    p.add_argument("--recipients", type=Path, help="Explicit approved recipient map JSON; required when the draft has none")
    p.add_argument("--calls", type=int, required=True, help="Finite interview model-call allowance")
    p.add_argument("--tokens", type=int, required=True, help="Finite interview token allowance")
    p.set_defaults(func=cmd_consent)

    p = command("interview", help="Run one adaptive interview call within consent")
    _add_goal(p)
    p.set_defaults(func=cmd_interview)

    p = command("propose", help="Propose the rubric and public brief without approving")
    _add_goal(p)
    p.add_argument("--options", type=Path, help="JSON object: rubric/brief/ceilings/recipients/seeds overrides")
    p.set_defaults(func=cmd_propose)

    p = command("approve", help="Approve the exact reviewed payload and body hash")
    _add_goal(p)
    _add_cas(p, required=True)
    p.add_argument("--payload", type=Path, required=True)
    p.add_argument("--body-hash", required=True, help="The reviewed 64-hex hash; approval never uses stored-only data")
    p.set_defaults(func=cmd_approve)

    p = command("grant", help="Grant finite execution ceilings (no start)")
    _add_goal(p)
    _add_cas(p)
    p.add_argument("--limits", type=Path, help="JSON limits; defaults to approved ceilings")
    p.set_defaults(func=cmd_grant)

    p = command("start", help="Start the granted execution")
    _add_goal(p)
    _add_cas(p)
    p.add_argument("--grant-id", required=True)
    p.set_defaults(func=_simple("start"))

    p = command("pause", help="Pause: stop admissions, drain dispatched work")
    _add_goal(p)
    _add_cas(p)
    p.set_defaults(func=_simple("pause"))

    p = command("resume", help="Resume without re-approving or re-granting")
    _add_goal(p)
    _add_cas(p)
    p.set_defaults(func=_simple("resume"))

    p = command("stop", help="Stop the goal; process control never rewrites it")
    _add_goal(p)
    _add_cas(p)
    p.set_defaults(func=_simple("stop"))

    p = command("revise", help="Open a revision; fences in-flight results")
    _add_goal(p)
    _add_cas(p)
    p.set_defaults(func=_simple("revise"))

    p = command("run", help="Run bounded research waves on a started goal")
    _add_goal(p)
    p.add_argument("--max-waves", type=int, required=True)
    p.set_defaults(func=cmd_run)

    p = command("serve", help="Serve the loopback console API; bootstrap token only via tty, SNOWBALL_CONSOLE_BOOTSTRAP_TOKEN, or --token")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--origin", default=None)
    p.add_argument("--token", default=None)
    p.set_defaults(func=cmd_serve)

    p = command("diagnostics", help="Read-only health; never migrates")
    p.set_defaults(func=cmd_diagnostics)

    p = command("ask", help="Query kept local evidence without any model call")
    _add_goal(p)
    p.add_argument("query")
    p.set_defaults(func=cmd_ask)

    p = command("export", help="Export goal and results to Markdown")
    _add_goal(p)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=cmd_export)

    p = command("migrate", help="Explicit verified backup then generic schema upgrade")
    p.add_argument("--root", type=Path, required=True, help="Storage root for the verified backup bundle")
    p.add_argument("--backup-dir", type=Path, default=None)
    p.set_defaults(func=cmd_migrate)

    p = command("restore", help="Restore a verified bundle into a new isolated root")
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--destination", type=Path, required=True)
    p.set_defaults(func=cmd_restore)

    p = command("retention", help="Delete oldest verified generic backup bundles, keeping --keep")
    p.add_argument("--root", type=Path, required=True, help="Backup root whose generic-v* bundles may be pruned")
    p.add_argument("--keep", type=int, default=7)
    p.set_defaults(func=cmd_retention)

    p = command("supervise", help="Supervise running goals; never creates, approves, grants or starts")
    p.add_argument("--goal-id", default=None, help="Supervise one goal; default all actually-running goals")
    p.add_argument("--max-waves", type=int, default=5)
    p.add_argument("--poll-sec", type=int, default=60)
    p.set_defaults(func=cmd_supervise)

    return parser


def _error_payload(exc: CliError | Exception) -> dict[str, Any]:
    if isinstance(exc, CliError):
        payload: dict[str, Any] = {"error": exc.error}
        if exc.field:
            payload["field"] = exc.field
        if exc.message:
            payload["message"] = exc.message
        return payload
    field = getattr(exc, "field", None)
    payload = {"error": type(exc).__name__}
    if field:
        payload["field"] = str(field)
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        payload["message"] = message
    return payload


def _argv_db(argv: list[str]) -> Path | None:
    """Last explicit --db wins; argparse subparser defaults would shadow the root value."""
    selected: Path | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--db":
            if index + 1 < len(argv):
                selected = Path(argv[index + 1])
            index += 2
            continue
        if token.startswith("--db="):
            selected = Path(token.split("=", 1)[1])
        index += 1
    return selected


def main(argv: list[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(supplied)
        args.db = _argv_db(supplied)
        if args.db is None:
            raise CliError(USAGE_HINT, field="db", message="pass --db before or after the subcommand")
        result = args.func(args)
        if isinstance(result, int) and args.command == "serve":
            return result
        print(json.dumps(result, ensure_ascii=False, default=dict))
        return EXIT_OK
    except CliError as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    except (ValueError, TypeError, KeyError, OSError) as exc:
        # Upstream modules raise field-scoped errors; keep exit 2 for structured input/state errors.
        translated = _translated(exc)
        print(json.dumps(_error_payload(translated), ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    finally:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
