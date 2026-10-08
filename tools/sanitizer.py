#!/usr/bin/env python3
"""Snowball Untrusted Content Sanitizer & Defense Layer.
Protects model ingestion pipelines against prompt injection, delimiter escaping,
markdown data exfiltration, and homoglyph/zero-width evasion.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

# 1. Zero-width, formatting, and bidirectional override characters
ZERO_WIDTH_CHARS = [
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\ufeff",  # zero-width no-break space / byte order mark
    "\u200e",  # left-to-right mark
    "\u200f",  # right-to-left mark
    "\u202a",  # left-to-right embedding
    "\u202b",  # right-to-left embedding
    "\u202c",  # pop directional formatting
    "\u202d",  # left-to-right override
    "\u202e",  # right-to-left override
    "\u2066",  # left-to-right isolate
    "\u2067",  # right-to-left isolate
    "\u2068",  # first strong isolate
    "\u2069",  # pop directional isolate
]
ZERO_WIDTH_RX = re.compile("[" + "".join(ZERO_WIDTH_CHARS) + "]")

# 2. Cyrillic and Greek confusables frequently used to bypass ASCII regexes
HOMOGLYPH_MAP = str.maketrans(
    {
        "а": "a",
        "А": "A",
        "с": "c",
        "С": "C",
        "е": "e",
        "Е": "E",
        "о": "o",
        "О": "O",
        "р": "p",
        "Р": "P",
        "ѕ": "s",
        "Ѕ": "S",
        "і": "i",
        "І": "I",
        "ј": "j",
        "Ј": "J",
        "у": "y",
        "У": "Y",
        "х": "x",
        "Х": "X",
        "ԁ": "d",
        "Ԃ": "D",
        "ԛ": "q",
        "Ԛ": "Q",
        "ԝ": "w",
        "Ԝ": "W",
    }
)

# 3. Markdown image exfiltration pattern: ![alt](url)
MARKDOWN_IMAGE_RX = re.compile(r"!\[(.*?)\]\((.*?)\)")

# 4. Dangerous link protocols in markdown: [text](javascript:...)
DANGEROUS_URI_RX = re.compile(r"\[(.*?)\]\(\s*(javascript|data|vbscript):.*?\)", re.IGNORECASE)


def strip_zero_width(text: str) -> str:
    """Remove invisible zero-width and bidirectional override code points."""
    return ZERO_WIDTH_RX.sub("", text)


def normalize_for_scanning(text: str) -> str:
    """Normalize text with Unicode NFKC and homoglyph folding for regex scanning."""
    cleaned = strip_zero_width(text)
    nfkc = unicodedata.normalize("NFKC", cleaned)
    return nfkc.translate(HOMOGLYPH_MAP)


def sanitize_delimiters(text: str) -> str:
    """Neutralize delimiter escaping sequences in untrusted text."""
    # Replace literal or entity-encoded source tags and XML structural constructs
    t = re.sub(r"<\s*/?\s*untrusted_source[^>]*>", "[untrusted_source]", text, flags=re.IGNORECASE)
    t = re.sub(r"<!\[CDATA\[|\]\]>", "[cdata]", t, flags=re.IGNORECASE)
    t = re.sub(r"<!--|-->", "[comment]", t)
    return t


def strip_markdown_exfiltration(text: str) -> str:
    """Strip markdown image tags and unsafe protocols to prevent data exfiltration."""
    # Replace ![image](url) with [image: alt] (disarms image loading)
    t = MARKDOWN_IMAGE_RX.sub(r"[\1]", text)
    # Neutralize dangerous link protocols
    t = DANGEROUS_URI_RX.sub(r"[\1](#blocked-protocol)", t)
    return t


def sanitize_untrusted_text(text: str) -> str:
    """Full sanitization pipeline for any untrusted text before prompt insertion."""
    t = strip_zero_width(text)
    t = unicodedata.normalize("NFKC", t)
    t = sanitize_delimiters(t)
    t = strip_markdown_exfiltration(t)
    return t


def fence_untrusted_source(
    content: str,
    nonce: str,
    url: str | None = None,
    title: str | None = None,
) -> str:
    """Wrap untrusted source text in a dynamically nonce-authenticated fence.
    Both opening and closing tags are bound to the random nonce to prevent tag spoofing.
    """
    clean_body = sanitize_untrusted_text(content)
    clean_title = sanitize_untrusted_text(title or "Untitled")
    clean_url = html.escape(url or "Unknown", quote=True)

    lines = [
        f"Source URL: {clean_url}",
        f"Source Title: {clean_title}",
        f'<untrusted_source_{nonce} id="{nonce}">',
        clean_body,
        f"</untrusted_source_{nonce}>",
    ]
    return "\n".join(lines)


class SecurityIncident(Exception):
    """Raised when an active prompt injection or canary leak is detected."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"SecurityIncident: {reason}")
        self.reason = reason
        self.details = details or {}
