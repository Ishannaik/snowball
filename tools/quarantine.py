#!/usr/bin/env python3
"""Flag wiki pages or reports that contain instruction-like or payload-like text.

  quarantine.py DIR [--record]   print flagged files; with --record, append them to QUARANTINE.md

Wiki text is built from untrusted forum posts. A page that carries an injection attempt must not
be committed silently: run.sh unstages flagged files and lists them in QUARANTINE.md for review.
Exit code 0 always; the caller reads stdout.
"""
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = [
    (r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?)", "ignore-previous"),
    (r"(system|admin|developer) (override|prompt|message)\s*:", "fake-system"),
    (r"you (now )?have (shell|tool|root) access", "fake-access"),
    (r"\b(run|execute) (this|the following) (command|script)", "run-this-command"),
    (r"curl[^\n|]{0,200}\|\s*(ba|z)?sh\b", "curl-pipe-shell"),
    (r"wget[^\n|]{0,200}\|\s*(ba|z)?sh\b", "wget-pipe-shell"),
    (r"(~|/home/\w+)/\.(secrets|ssh|config/qwen-proxy)", "secret-path"),
    (r"\b(api[_ -]?keys?|private key|password)s?\b[^\n]{0,40}\b(paste|send|reveal|print|exfiltrat)", "secret-request"),
    (r"[A-Za-z0-9+/]{120,}={0,2}", "base64-blob"),
    (r"</?\s*untrusted_source", "delimiter-spoof"),
]
RX = [(re.compile(p, re.I), name) for p, name in PATTERNS]


def scan(path: Path):
    text = path.read_text(errors="replace")
    return sorted({name for rx, name in RX if rx.search(text)})


def main():
    base = Path(sys.argv[1])
    record = "--record" in sys.argv
    hits = []
    for f in sorted(base.rglob("*.md")):
        names = scan(f)
        if names:
            hits.append((f, names))
            print(f"{f}\t{','.join(names)}")
    if record and hits:
        q = ROOT / "QUARANTINE.md"
        new = not q.exists()
        with q.open("a") as fh:
            if new:
                fh.write("# Quarantine\n\nPages flagged for instruction-like text. They were NOT committed. "
                         "Review, then delete or commit by hand.\n\n")
            for f, names in hits:
                fh.write(f"- {time.strftime('%F %T')} `{f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}`: {', '.join(names)}\n")


if __name__ == "__main__":
    main()
