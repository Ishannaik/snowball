#!/usr/bin/env python3
"""Snowball Unified CLI Dispatcher.
Commands:
  snowball run [--topic SLUG] [--max-topics N] [--dry-run] [--mode MODE]
  snowball status [--json]
  snowball ask "<question>" [--json] [--no-model]
  snowball serve [--port PORT]
  snowball visualize [--terminal] [--port PORT]
  snowball digest [--post] [--channel ID]
  snowball compile [--out DIR]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable


def run_cmd(args: list[str]) -> int:
    """Execute python subprocess forwarding stdout/stderr."""
    res = subprocess.run([PYTHON] + args, cwd=ROOT)
    return res.returncode


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="snowball",
        description="Snowball: Autonomous 24/7 Deep Research Agent That Never Repeats Itself",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    # 1. Run
    p_run = sub.add_parser("run", help="Execute research crawl & extraction cycle")
    p_run.add_argument("--topic", help="Target topic slug")
    p_run.add_argument("--max-topics", type=int, default=2, help="Max topics per batch")
    p_run.add_argument("--mode", choices=["auto", "gemini-bulk", "grok-xhigh", "local"], default="auto")
    p_run.add_argument("--dry-run", action="store_true", help="Simulate without network calls")

    # 2. Status
    p_status = sub.add_parser("status", help="Print frontier health, token spend, and yield")
    p_status.add_argument("--json", action="store_true", help="Output JSON")

    # 3. Ask
    p_ask = sub.add_parser("ask", help="Query local knowledge base with verified citations")
    p_ask.add_argument("query", help="Question or search query")
    p_ask.add_argument("--json", action="store_true", help="Output raw facts JSON")
    p_ask.add_argument("--no-model", action="store_true", help="Deterministic synthesis only")

    # 4. Serve (Web Dashboard)
    p_serve = sub.add_parser("serve", help="Launch interactive web dashboard & knowledge graph")
    p_serve.add_argument("--port", type=int, default=8989, help="HTTP port (default: 8989)")

    # 5. Visualize
    p_vis = sub.add_parser("visualize", help="Render terminal or web knowledge graph")
    p_vis.add_argument("--terminal", action="store_true", help="ASCII terminal radar view")
    p_vis.add_argument("--serve", action="store_true", help="Launch web server")
    p_vis.add_argument("--port", type=int, default=8989, help="HTTP port (default: 8989)")

    # 6. Digest
    p_digest = sub.add_parser("digest", help="Compile daily opportunity digest")
    p_digest.add_argument("--post", action="store_true", help="Send to Discord webhook/bot")
    p_digest.add_argument("--channel", type=str, default="", help="Override Discord channel")

    # 7. Compile
    p_compile = sub.add_parser("compile", help="Compile Markdown knowledge base & entities")
    p_compile.add_argument("--out", type=Path, default=ROOT / "wiki", help="Output wiki path")

    parsed, remaining = ap.parse_known_args()

    cmd = parsed.command
    if cmd == "run":
        argv = ["tools/orchestrator.py"]
        if parsed.topic:
            argv.extend(["--topic", parsed.topic])
        if parsed.max_topics:
            argv.extend(["--max-topics", str(parsed.max_topics)])
        if parsed.mode:
            argv.extend(["--mode", parsed.mode])
        if parsed.dry_run:
            argv.append("--dry-run")
        argv.extend(remaining)
        sys.exit(run_cmd(argv))

    elif cmd == "status":
        argv = ["tools/orchestrator.py", "--status"]
        if parsed.json:
            argv.append("--json")
        sys.exit(run_cmd(argv))

    elif cmd == "ask":
        argv = ["tools/ask.py", parsed.query]
        if parsed.json:
            argv.append("--json")
        if parsed.no_model:
            argv.append("--no-model")
        argv.extend(remaining)
        sys.exit(run_cmd(argv))

    elif cmd == "serve":
        argv = ["tools/visualize.py", "--serve", "--port", str(parsed.port)]
        sys.exit(run_cmd(argv))

    elif cmd == "visualize":
        argv = ["tools/visualize.py"]
        if parsed.serve:
            argv.extend(["--serve", "--port", str(parsed.port)])
        elif parsed.terminal:
            argv.append("--terminal")
        else:
            argv.append("--terminal")
        sys.exit(run_cmd(argv))

    elif cmd == "digest":
        argv = ["tools/digest.py"]
        if parsed.post:
            argv.append("--post")
        if parsed.channel:
            argv.extend(["--channel", parsed.channel])
        sys.exit(run_cmd(argv))

    elif cmd == "compile":
        argv = ["tools/wiki_compiler.py", "--out", str(parsed.out)]
        sys.exit(run_cmd(argv))


if __name__ == "__main__":
    main()
