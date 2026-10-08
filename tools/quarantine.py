#!/usr/bin/env python3
"""Snowball Prompt Injection Quarantine & Adversarial Content Scanner.
Scans untrusted web text, reports, and wiki pages against a comprehensive suite of
instruction-hijacking, jailbreak, turn-spoofing, and exfiltration patterns.
Applies Unicode normalization and zero-width stripping prior to regex evaluation.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import importlib.util

_san_path = ROOT / "tools" / "sanitizer.py"
if _san_path.exists():
    _spec = importlib.util.spec_from_file_location("sanitizer", _san_path)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    normalize_for_scanning = _mod.normalize_for_scanning
else:
    import unicodedata

    def normalize_for_scanning(text: str) -> str:
        cleaned = re.sub(r"[\u200b-\u200f\ufeff\u202a-\u202e\u2066-\u2069]", "", text)
        return unicodedata.normalize("NFKC", cleaned)


# Comprehensive Adversarial Pattern Suite
PATTERNS = [
    # 1. Classical instruction override
    (r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?)", "ignore-previous"),
    (r"(system|admin|developer) (override|prompt|message)\s*:", "fake-system"),
    (r"you (now )?have (shell|tool|root) access", "fake-access"),
    (r"\b(run|execute) (this|the following) (command|script)", "run-this-command"),
    # 2. Shell and code execution pipes
    (r"curl[^\n|]{0,200}\|\s*(ba|z)?sh\b", "curl-pipe-shell"),
    (r"wget[^\n|]{0,200}\|\s*(ba|z)?sh\b", "wget-pipe-shell"),
    # 3. Secret and path exfiltration
    (r"(~|/home/\w+)/\.(secrets|ssh|config/qwen-proxy)", "secret-path"),
    (r"\b(api[_ -]?keys?|private key|password)s?\b[^\n]{0,40}\b(paste|send|reveal|print|exfiltrat)", "secret-request"),
    (r"[A-Za-z0-9+/]{120,}={0,2}", "base64-blob"),
    # 4. Delimiter and container escaping
    (r"</?\s*untrusted_source", "delimiter-spoof"),
    (r"<!\[CDATA\[|\]\]>", "cdata-spoof"),
    # 5. Chat template & instruction tokens
    (r"<\|im_start\|>|<\|im_end\|>|<\|user\|>|<\|assistant\|>|<\|system\|>", "chat-template-token"),
    (r"\[/?INST\]|<<SYS>>|<</SYS>>", "chat-template-token"),
    # 6. Turn spoofing & simulated conversation roles
    (r"</?turn(\s+role=|\s*>)|<turn\b", "turn-spoof"),
    (r"\b(Human|Assistant|User|System):\s*([A-Z]|ignore|return|you are)", "role-transcript"),
    # 7. Persona hijacking & jailbreak declarations
    (
        r"\b(you are (now )?DAN|do anything now|stay in character|unrestricted (mode|assistant|ai)|developer mode)\b",
        "persona-jailbreak",
    ),
    (r"\b(disregard (all )?(ethical|safety|prior) (guidelines|rules|constraints))\b", "persona-jailbreak"),
    # 8. Markdown image/link exfiltration attempts
    (r"!\[.*?\]\([^\)]*?\?(leak|exfil|token|key|secret)=.*?\)", "markdown-exfil"),
    (r"\[.*?\]\(\s*(javascript|data|vbscript):.*?\)", "dangerous-link-protocol"),
    # 9. Canary artifacts in public text
    (r"\bSNOWBALL_CANARY_[A-Fa-f0-9]{32,64}\b", "canary-artifact"),
]

RX = [(re.compile(p, re.IGNORECASE), name) for p, name in PATTERNS]


def scan_text(text: str) -> list[str]:
    """Scan raw text for prompt injection patterns with pre-scan normalization."""
    normalized = normalize_for_scanning(text)
    return sorted({name for rx, name in RX if rx.search(normalized)})


def scan(path: Path) -> list[str]:
    """Scan file for prompt injection patterns."""
    text = path.read_text(errors="replace", encoding="utf-8")
    return scan_text(text)


def record_quarantine(hits: list[tuple[Path, list[str]]]) -> None:
    """Append flagged files to QUARANTINE.md."""
    if not hits:
        return
    q = ROOT / "QUARANTINE.md"
    new = not q.exists()
    with q.open("a", encoding="utf-8") as fh:
        if new:
            fh.write(
                "# Quarantine\n\nPages flagged for instruction-like text. They were NOT committed. "
                "Review, then delete or commit by hand.\n\n"
            )
        for f, names in hits:
            fh.write(
                f"- {time.strftime('%F %T')} `{f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}`: {', '.join(names)}\n"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description="Snowball Prompt Injection Scanner")
    ap.add_argument("path", type=Path, help="File or directory to scan")
    ap.add_argument("--record", action="store_true", help="Record hits in QUARANTINE.md")
    args = ap.parse_args()

    hits = []
    if args.path.is_file():
        reasons = scan(args.path)
        if reasons:
            hits.append((args.path, reasons))
            print(f"{args.path}\t{','.join(reasons)}")
    elif args.path.is_dir():
        for f in sorted(args.path.rglob("*.md")):
            reasons = scan(f)
            if reasons:
                hits.append((f, reasons))
                print(f"{f}\t{','.join(reasons)}")

    if args.record and hits:
        record_quarantine(hits)


if __name__ == "__main__":
    main()
