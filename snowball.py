#!/usr/bin/env python3
"""Snowball CLI: research, local knowledge, delivery, and production diagnostics."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_cmd(args: list[str]) -> int:
    """Use the active Python environment and preserve the worker's exit status."""
    return subprocess.run([sys.executable, *args], cwd=ROOT).returncode


def add_storage(parser: argparse.ArgumentParser, *, wiki: bool = False) -> None:
    parser.add_argument("--db", type=Path, help="SQLite database (default: data/snowball.db)")
    if wiki:
        parser.add_argument("--wiki", type=Path, help="Markdown knowledge directory (default: wiki)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snowball", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    goal_parser = sub.add_parser("goal", help="Generic goal interview, review, approval, grant and bounded run (forwards to generic_cli)")
    goal_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    migration_parser = sub.add_parser("migrate", help="Explicit generic schema upgrade with a verified backup bundle (forwards to generic_cli)")
    migration_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    for name, flag, description in (
        ("run-once", "--run-once", "Run one wave and report its supervisor outcome"),
        ("seed", "--seed-only", "Seed the deterministic topic grid"),
    ):
        p = sub.add_parser(name, help=description)
        p.set_defaults(worker="orchestrator.py", worker_flag=flag)
        add_storage(p)
        p.add_argument("--json", action="store_true")
        if name != "status":
            p.add_argument("--topics", type=Path, help="Topic configuration YAML")
            p.add_argument("--dry-run", "--dryrun", action="store_true", help="In-memory run; no external calls or disk writes")
        if name in ("run", "run-once", "supervise"):
            p.add_argument("--topic", help="Target topic slug")
            p.add_argument("--max-topics", type=int, default=1 if name != "run" else None)
            p.add_argument("--mode", choices=["auto", "gemini-bulk"], help="Override configured extraction mode")

    ask_parser = sub.add_parser("ask", help="Query kept generic goal evidence (forwards to generic_cli)")
    ask_parser.add_argument("arguments", nargs=argparse.REMAINDER)

    p = sub.add_parser("serve", help="Serve the authenticated generic console API")
    p.set_defaults(worker=None, worker_flag=None)
    add_storage(p)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--origin", default=None, help="Expected Origin for authenticated writes (default loopback http origin)")
    p.add_argument("--token", default=None, help="Bootstrap API token; random one printed once when omitted")

    console_parser = sub.add_parser("console", help="Generic console controls; forwards to the generic CLI (server: snowball serve)")
    console_parser.add_argument("arguments", nargs=argparse.REMAINDER)

    p = sub.add_parser("visualize", help="Render a terminal view of the research database")
    p.set_defaults(worker="visualize.py", worker_flag=None)
    add_storage(p)
    p.add_argument("--port", type=int, default=8989)
    p.add_argument("--host", default="127.0.0.1", help="Bind address; loopback by default")
    p.add_argument("--terminal", action="store_true")

    for name in ("digest", "alerts"):
        p = sub.add_parser(name, help="Prepare evidence-backed delivery; acknowledge only confirmed sends")
        p.set_defaults(worker=f"{name}.py", worker_flag=None)
        add_storage(p)
        p.add_argument("--post", action="store_true")
        p.add_argument("--channel", help="Explicit authorized Discord channel")
        p.add_argument("--json", action="store_true")
        if name == "digest":
            p.add_argument("--day", help="IST date YYYY-MM-DD")
            p.add_argument("--weekly", action="store_true")
        else:
            p.add_argument("--as-of", help="Evaluation timestamp")

    p = sub.add_parser("compile", help="Incrementally compile authoritative Markdown")
    p.set_defaults(worker="wiki_compiler.py", worker_flag=None)
    add_storage(p)
    p.add_argument("--out", type=Path, default=ROOT / "wiki")
    p.add_argument("--full", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("reindex", help="Rebuild knowledge caches from Markdown, including human edits")
    p.set_defaults(worker="eval_gates.py", worker_flag="reindex")
    add_storage(p, wiki=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("retention", help="Prune expired raw/logs and create or restore a consistent backup")
    p.set_defaults(worker="retention.py", worker_flag=None)
    add_storage(p)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--restore", type=Path, help="Backup to restore into the selected isolated --db")
    p.add_argument("--keep", type=int, default=7)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("feedback", help="Ingest authorized, deduplicated reaction events")
    p.set_defaults(worker="feedback.py", worker_flag=None)
    add_storage(p)
    p.add_argument("--event-json", help="Reaction event JSON or a file containing it")
    p.add_argument("--item-id", help="Delivered item identifier")
    p.add_argument("--reaction", choices=["up", "down"])
    p.add_argument("--json", action="store_true")

    for name in ("diagnostics", "access-diagnose"):
        p = sub.add_parser(name, help="Check production readiness; blocked prerequisites return nonzero")
        p.set_defaults(worker="eval_gates.py", worker_flag="diagnostics")
        add_storage(p, wiki=True)
        p.add_argument("--json", action="store_true")
        p.add_argument("--live", action="store_true", help="Run authenticated provider diagnostics")

    p = sub.add_parser("eval", help="Evaluate production behavior; empty samples never pass")
    eval_sub = p.add_subparsers(dest="eval_cmd", required=True)
    for name in ("classify", "extract", "topics", "digest"):
        ep = eval_sub.add_parser(name)
        ep.set_defaults(worker="eval_gates.py", worker_flag=name)
        add_storage(ep)
        ep.add_argument("--json", action="store_true")
        if name == "digest":
            ep.add_argument("--since", default="7d", help="Audit window, e.g. 7d")
        else:
            ep.add_argument("fixture", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    if supplied and supplied[0] in ("goal", "console", "run", "migrate", "serve", "status", "supervise", "retention", "ask"):
        # Generic cutover: the default active surface is the generic harness.
        # `goal`/`console` forward to the generic CLI; `run` uses the generic
        # bounded runtime; `serve` serves the generic console API; `migrate`
        # uses the explicit generic migration; `status`, `supervise` and
        # `retention` are the generic read/supervision/backup paths; `ask`
        # queries kept local goal evidence without any model call.
        # No `generic` alias remains.
        return run_cmd([str(ROOT / "tools" / "generic_cli.py"), *supplied[1:]])
    args = build_parser().parse_args(argv)
    forwarded = [str(ROOT / "tools" / args.worker)]
    if args.worker_flag:
        forwarded.append(args.worker_flag)
    if args.command == "eval" and args.eval_cmd != "digest":
        forwarded.append(str(args.fixture))
    for field in (
        "db", "wiki", "topics", "topic", "max_topics", "mode", "port", "host",
        "channel", "day", "as_of", "out", "root", "restore", "keep", "event_json",
        "item_id", "reaction", "since",
    ):
        value = getattr(args, field, None)
        if value is not None:
            forwarded.extend(["--" + field.replace("_", "-"), str(value)])
    for field in ("json", "dry_run", "no_model", "serve", "terminal", "post", "weekly", "full", "live"):
        if getattr(args, field, False):
            forwarded.append("--" + field.replace("_", "-"))
    return run_cmd(forwarded)


if __name__ == "__main__":
    sys.exit(main())
