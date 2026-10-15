#!/usr/bin/env python3
"""Snowball orchestrator: goal-gated research waves over the retained fetch and lock engine.

Public extraction and review come from tools/engine_transport.py. X posts use GROK_X_* only.
A wave runs only for an approved goal with an explicit current grant. Dry-run and status
do not call the network and do not write the selected database.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Running `python3 tools/orchestrator.py` puts tools/ first on sys.path, where tools/queue.py shadows the
# stdlib queue module that concurrent.futures needs. Drop tools/ and any already-shadowed module first.
_TOOLS_DIR = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _TOOLS_DIR]
if (
    getattr(sys.modules.get("queue"), "__file__", "")
    and Path(sys.modules["queue"].__file__).resolve().parent == _TOOLS_DIR
):
    del sys.modules["queue"]

import argparse
import contextlib

try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any


def _flock_ex(fh: Any) -> None:
    if fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_EX)
    else:
        import msvcrt

        with contextlib.suppress(Exception):
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)


def _flock_nb(fh: Any) -> None:
    if fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        import msvcrt

        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise BlockingIOError("locked") from e


from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent


def _sibling(name: str) -> Any:
    """Load tools/<name>.py by path. Putting tools/ on sys.path would let tools/queue.py shadow the
    stdlib queue module, which concurrent.futures needs (AttributeError: no SimpleQueue)."""
    import importlib.util

    mod_name = f"snowball_{name}"
    existing = sys.modules.get(mod_name)
    if existing is not None and Path(existing.__file__).resolve() == (ROOT / "tools" / f"{name}.py").resolve():
        return existing
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass processing can see the module (Python 3.13).
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


quarantine = _sibling("quarantine")
sanitizer = _sibling("sanitizer")
process_control = _sibling("process_control")
access_policy = _sibling("access_policy")
paid_fetch = _sibling("paid_fetch")
fetch_types = _sibling("fetch_types")
FetchResult = fetch_types.FetchResult
SearchHit = fetch_types.SearchHit
AccessRequest = fetch_types.AccessRequest
SecurityIncident = getattr(sanitizer, "SecurityIncident", Exception)

DB_PATH = ROOT / "data/snowball.db"
RAW_DIR = ROOT / "raw"
STOP_FILE = ROOT / "STOP"
ORNITH_LOCK = ROOT / "ornith.lock"
IST = timezone(timedelta(hours=5, minutes=30))
SCHEMA_VERSION = "4"
ORCH_SCHEMA_VERSION = "2"


def now() -> int:
    return int(time.time())


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def log(msg: str) -> None:
    print(f"{datetime.now(IST).strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------
# Configuration: config.env (committed, no secrets) then .env (secrets), then the process env.
# ---------------------------------------------------------------------------------------------


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class Config:
    mode: str = "auto"
    gemini_endpoint: str = "https://generativelanguage.googleapis.com/v1beta/models"
    gemini_model: str = "gemini-3.8-flash"
    gemini_pro_model: str = "gemini-3.1-pro"
    gemini_max_input_chars: int = 3_000_000
    gemini_api_key: str | None = None
    gemini_transport: str = "api"
    ssh_model_host: str = "oracle"
    ssh_omp_bin: str = "/usr/local/bin/omp"
    ssh_timeout_sec: int = 120
    exa_rotator_url: str = "http://127.0.0.1:8791"
    grok_reasoning_effort: str = "xhigh"
    grok_x_bin: str = str(Path.home() / ".local/bin/grok")
    grok_x_model: str = ""
    grok_x_poll_sec: int = 180
    grok_x_window_days: int = 30
    omp_bin: str = "/usr/local/bin/omp"
    review_model: str = ""
    review_transport: str = ""
    review_timeout_sec: int = 180
    review_tokens_per_day: int = 200_000
    plan_model: str = ""
    plan_transport: str = ""
    plan_tokens_per_wave: int = 8_000
    plan_max_hypotheses: int = 6
    plan_exploration_share: float = 0.3
    private_model_endpoint: str | None = None
    private_model_id: str = ""
    private_model_attested: bool = False
    private_model_isolation: str = ""
    console_host: str = "127.0.0.1"
    console_port: int = 8989
    console_allowed_origins: tuple[str, ...] = ()
    console_cookie_secure: bool = False
    llm_tokens_per_day: int = 32_000_000
    caps: dict[str, int] = field(default_factory=lambda: {"gemini": 20_000_000, "review": 200_000})
    extract_budget: dict[str, tuple[int, int]] = field(
        default_factory=lambda: {"gemini": (800_000, 16_000), "review": (60_000, 8_000)}
    )
    review_budget: tuple[int, int] = (60_000, 8_000)
    max_topics: int = 2
    pages_per_topic: int = 12
    queries_per_topic: int = 6
    collector_concurrency: int = 6
    domain_rate_per_min: int = 6
    max_body_bytes: int = 5 * 1024 * 1024
    kb_answer_min_claims: int = 5
    gh_token: str | None = None
    paid_provider: str = ""
    zenrows_mode: str = "auto"
    zenrows_api_key: str | None = None
    residential_proxy_url: str | None = None
    paid_min_yield: float = 0.30
    max_proxy_bytes: int = 20_971_520
    max_paid_retrievals: int = 2
    challenge_fixture: bool = False
    captcha_api_key: str | None = None
    captcha_deadline_sec: int = 120
    discord_channel_id: str | None = None


def load_config() -> Config:
    ext_env_path = os.environ.get("SNOWBALL_ENV_FILE")
    ext_env = _read_env_file(Path(ext_env_path)) if ext_env_path else {}
    env = {**_read_env_file(ROOT / "config.env"), **_read_env_file(ROOT / ".env"), **ext_env, **os.environ}
    env.pop("OPENROUTER_API_KEY", None)  # standing rule: never a model backend (spent cap, every call 429s)

    def gi(k: str, d: int) -> int:
        try:
            return int(env.get(k, d))
        except (TypeError, ValueError):
            return d

    c = Config()
    c.mode = env.get("ORCH_DEFAULT_MODE", c.mode)
    c.gemini_endpoint = env.get("GEMINI_ENDPOINT", c.gemini_endpoint).rstrip("/")
    c.gemini_model = env.get("GEMINI_MODEL", c.gemini_model)
    c.gemini_pro_model = env.get("GEMINI_PRO_MODEL", c.gemini_pro_model)
    c.gemini_max_input_chars = gi("GEMINI_MAX_INPUT_CHARS", c.gemini_max_input_chars)
    c.gemini_api_key = env.get("GEMINI_API_KEY") or None
    c.grok_reasoning_effort = env.get("GROK_REASONING_EFFORT", c.grok_reasoning_effort)
    c.grok_x_bin = env.get("GROK_X_BIN", c.grok_x_bin)
    if not Path(c.grok_x_bin).exists():
        c.grok_x_bin = shutil.which("grok") or c.grok_x_bin
    c.grok_x_model = env.get("GROK_X_MODEL", c.grok_x_model)
    c.grok_x_poll_sec = gi("GROK_X_POLL_SEC", c.grok_x_poll_sec)
    c.grok_x_window_days = gi("GROK_X_WINDOW_DAYS", c.grok_x_window_days)
    c.gemini_transport = env.get("GEMINI_TRANSPORT", c.gemini_transport).strip().lower()
    c.ssh_model_host = env.get("SSH_MODEL_HOST", c.ssh_model_host)
    c.ssh_omp_bin = env.get("SSH_OMP_BIN", c.ssh_omp_bin)
    c.ssh_timeout_sec = gi("SSH_TIMEOUT_SEC", c.ssh_timeout_sec)
    c.exa_rotator_url = env.get("EXA_ROTATOR_URL", c.exa_rotator_url).rstrip("/")
    c.omp_bin = env.get("OMP_BIN", c.omp_bin)
    if not Path(c.omp_bin).exists():
        c.omp_bin = shutil.which("omp") or (str(Path.home() / ".bun/bin/omp") if (Path.home() / ".bun/bin/omp").exists() else c.omp_bin)
    c.review_model = env.get("REVIEW_MODEL", c.review_model).strip()
    c.review_transport = env.get("REVIEW_TRANSPORT", c.review_transport).strip().lower()
    c.review_timeout_sec = gi("REVIEW_TIMEOUT_SEC", c.review_timeout_sec)
    c.review_tokens_per_day = gi("REVIEW_TOKENS_PER_DAY", c.review_tokens_per_day)
    c.plan_model = env.get("PLAN_MODEL", c.plan_model).strip() or c.review_model
    c.plan_transport = env.get("PLAN_TRANSPORT", c.plan_transport).strip().lower() or c.review_transport
    c.plan_tokens_per_wave = gi("PLAN_TOKENS_PER_WAVE", c.plan_tokens_per_wave)
    c.plan_max_hypotheses = gi("PLAN_MAX_HYPOTHESES_PER_WAVE", c.plan_max_hypotheses)
    try:
        c.plan_exploration_share = float(env.get("PLAN_EXPLORATION_SHARE", c.plan_exploration_share))
    except (TypeError, ValueError):
        c.plan_exploration_share = 0.3
    c.private_model_endpoint = (env.get("PRIVATE_MODEL_ENDPOINT") or "").strip() or None
    c.private_model_id = (env.get("PRIVATE_MODEL_ID") or "").strip()
    c.private_model_attested = (env.get("PRIVATE_MODEL_ATTESTED") or "0").strip() == "1"
    c.private_model_isolation = (env.get("PRIVATE_MODEL_ISOLATION") or "").strip()
    c.console_host = env.get("CONSOLE_HOST", c.console_host)
    c.console_port = gi("CONSOLE_PORT", c.console_port)
    origins = env.get("CONSOLE_ALLOWED_ORIGINS", "")
    c.console_allowed_origins = tuple(part.strip() for part in origins.split(",") if part.strip())
    c.console_cookie_secure = (env.get("CONSOLE_COOKIE_SECURE") or "0").strip() == "1"
    c.llm_tokens_per_day = gi("LLM_TOKENS_PER_DAY", c.llm_tokens_per_day)
    c.caps = {
        "gemini": gi("GEMINI_TOKENS_PER_DAY", c.caps["gemini"]),
        "review": gi("REVIEW_TOKENS_PER_DAY", c.review_tokens_per_day),
    }
    c.extract_budget = {
        "gemini": (gi("GEMINI_EXTRACT_BUDGET_IN", 800_000), gi("GEMINI_EXTRACT_BUDGET_OUT", 16_000)),
        "review": (gi("REVIEW_BUDGET_IN", 60_000), gi("REVIEW_BUDGET_OUT", 8_000)),
    }
    c.review_budget = c.extract_budget["review"]
    c.max_topics = gi("ORCH_MAX_TOPICS", c.max_topics)
    c.pages_per_topic = gi("ORCH_PAGES_PER_TOPIC", c.pages_per_topic)
    c.queries_per_topic = gi("ORCH_QUERIES_PER_TOPIC", c.queries_per_topic)
    c.collector_concurrency = gi("COLLECTOR_CONCURRENCY", c.collector_concurrency)
    c.domain_rate_per_min = gi("DOMAIN_RATE_PER_MIN", c.domain_rate_per_min)
    c.max_body_bytes = gi("MAX_BODY_BYTES", c.max_body_bytes)
    c.paid_provider = (env.get("ACCESS_PAID_PROVIDER") or "").strip().lower()
    c.zenrows_mode = (env.get("ZENROWS_MODE") or "auto").strip().lower()
    c.zenrows_api_key = env.get("ZENROWS_API_KEY") or None
    c.residential_proxy_url = env.get("RESIDENTIAL_PROXY_URL") or None
    try:
        c.paid_min_yield = float(env.get("PAID_FETCH_MIN_YIELD_SCORE", c.paid_min_yield))
    except (TypeError, ValueError):
        c.paid_min_yield = 0.30
    c.max_proxy_bytes = gi("MAX_PROXY_BYTES_PER_ACQUISITION", c.max_proxy_bytes)
    c.max_paid_retrievals = gi("MAX_PAID_RETRIEVALS_PER_ACQUISITION", c.max_paid_retrievals)
    c.challenge_fixture = (env.get("SNOWBALL_CHALLENGE_FIXTURE") or "0").strip() == "1"
    c.captcha_api_key = env.get("CAPTCHA_API_KEY") or None
    c.captcha_deadline_sec = gi("CAPTCHA_DEADLINE_SEC", c.captcha_deadline_sec)
    c.kb_answer_min_claims = gi("KB_ANSWER_MIN_CLAIMS", c.kb_answer_min_claims)
    c.gh_token = env.get("GH_TOKEN") or None
    for model in (c.gemini_model, c.gemini_pro_model, c.review_model, c.plan_model, c.private_model_id, c.grok_x_model):
        if model and model.lower().startswith("openrouter"):
            raise SystemExit(f"refusing model {model}: OpenRouter is never a backend here")
    return c


def _role_diagnostic(cfg: Config, role: str, model: str, transport: str, live: bool) -> dict[str, Any]:
    """Role availability without secrets. Opaque public transports stay estimated."""
    del live  # a live completion belongs to the engine, and status must not start one
    usage = "estimated" if transport in {"omp", "ssh-omp", "agy"} else "unknown"
    if role == "private":
        endpoint = (cfg.private_model_endpoint or "").lower()
        if any(token in endpoint for token in ("agy", "generativelanguage.googleapis.com", ":8088", "openrouter")):
            return {
                "status": "blocked",
                "role": "private_eval",
                "model": model,
                "usage_kind": "unavailable",
                "error": "private endpoint matches a public transport",
            }
        return {
            "status": "configured",
            "role": "private_eval",
            "model": model,
            "usage_kind": "unknown",
            "attested": True,
        }
    if not model or not transport:
        return {
            "status": "blocked",
            "role": role,
            "model": model or None,
            "transport": transport or None,
            "usage_kind": "unavailable",
            "error": f"{role} model is not configured",
        }
    if transport in {"omp", "ssh-omp"}:
        binary = cfg.omp_bin if transport == "omp" else cfg.ssh_omp_bin
        resolved = shutil.which(binary) or (binary if Path(binary).exists() else None)
        if transport == "ssh-omp":
            resolved = shutil.which("ssh") and resolved
        if not resolved:
            return {
                "status": "blocked",
                "role": role,
                "model": model,
                "transport": transport,
                "usage_kind": usage,
                "error": "transport binary not found",
            }
    try:
        mod = _sibling("engine_transport")
        cls = mod.LocalPrivateEngine if role == "private" else mod.ReviewEngine
        ok, why = cls(cfg, dry=True).available()
    except Exception as exc:  # noqa: BLE001 - diagnostics report the miss; they do not invent an engine
        return {
            "status": "blocked",
            "role": role,
            "model": model,
            "transport": transport,
            "usage_kind": usage,
            "error": type(exc).__name__,
        }
    return {
        "status": "ok" if ok else "blocked",
        "role": role,
        "model": model,
        "transport": transport,
        "usage_kind": usage,
        "error": None if ok else why,
    }


def diagnose_providers(cfg: Config, live: bool = False) -> dict[str, Any]:
    """Perform authenticated availability checks and bounded completions for configured model IDs and Exa.
    Missing credentials, unavailable tunnels, or floor drops are explicit blockers or failures.
    """
    res: dict[str, Any] = {}

    # 1. Gemini
    transport = getattr(cfg, "gemini_transport", "api")
    if transport == "ssh-omp":
        ssh_bin = shutil.which("ssh")
        if not ssh_bin:
            res["gemini"] = {"status": "blocked", "transport": "ssh-omp", "error": "ssh binary not found"}
        else:
            try:
                test_cmd = [
                    "ssh",
                    cfg.ssh_model_host,
                    "systemd-run",
                    "--user",
                    "--scope",
                    "-p",
                    "MemoryMax=3G",
                    cfg.ssh_omp_bin,
                    "-p",
                    "--model",
                    cfg.gemini_model,
                    "--no-tools",
                    "--no-session",
                    "--no-extensions",
                    "--no-title",
                ]
                if live:
                    p = subprocess.run(
                        test_cmd,
                        input="Respond with exactly 'GEMINI_OK'.",
                        capture_output=True,
                        text=True,
                        timeout=cfg.ssh_timeout_sec,
                    )
                    if p.returncode == 0 and "GEMINI_OK" in p.stdout:
                        res["gemini"] = {"status": "ok", "transport": "ssh-omp", "model": cfg.gemini_model, "host": cfg.ssh_model_host}
                    else:
                        res["gemini"] = {"status": "error", "transport": "ssh-omp", "error": (p.stderr or p.stdout)[-300:]}
                else:
                    p = subprocess.run(["ssh", cfg.ssh_model_host, "true"], capture_output=True, timeout=10)
                    if p.returncode == 0:
                        res["gemini"] = {"status": "ok", "transport": "ssh-omp", "model": cfg.gemini_model, "host": cfg.ssh_model_host}
                    else:
                        res["gemini"] = {"status": "error", "transport": "ssh-omp", "error": (p.stderr or "")[-300:]}
            except Exception as e:
                res["gemini"] = {"status": "error", "transport": "ssh-omp", "error": str(e)[:300]}
    elif transport == "omp":
        resolved_omp = shutil.which(cfg.omp_bin) or (cfg.omp_bin if Path(cfg.omp_bin).exists() else None)
        if not resolved_omp:
            res["gemini"] = {"status": "blocked", "transport": "omp", "error": f"omp binary not found at {cfg.omp_bin}"}
        else:
            res["gemini"] = {"status": "ok", "transport": "omp", "binary": resolved_omp, "model": cfg.gemini_model}
    else:  # "api"
        if not cfg.gemini_api_key:
            res["gemini"] = {"status": "blocked", "transport": "api", "model": cfg.gemini_model, "error": "GEMINI_API_KEY not configured"}
        else:
            try:
                url = f"{cfg.gemini_endpoint}/{cfg.gemini_model}:generateContent"
                body = {
                    "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                    "generationConfig": {"maxOutputTokens": 5},
                }
                d = _http_json(url, body, {"x-goog-api-key": cfg.gemini_api_key}, timeout=15)
                cands = d.get("candidates") or []
                if cands:
                    res["gemini"] = {"status": "ok", "transport": "api", "model": cfg.gemini_model, "endpoint": cfg.gemini_endpoint}
                else:
                    res["gemini"] = {"status": "error", "transport": "api", "model": cfg.gemini_model, "error": f"no candidates: {d}"}
            except Exception as e:
                res["gemini"] = {"status": "error", "transport": "api", "model": cfg.gemini_model, "error": str(e)[:300]}

    # 2. Review and plan. Availability is configuration plus the engine probe, never a renamed verifier.
    for role, model, transport in (
        ("review", cfg.review_model, cfg.review_transport),
        ("plan", cfg.plan_model, cfg.plan_transport),
    ):
        res[role] = _role_diagnostic(cfg, role, model, transport, live)

    # 3. Private interpretation stays unset until attestation and isolation are both explicit.
    if not cfg.private_model_endpoint or not cfg.private_model_id:
        res["private"] = {
            "status": "blocked",
            "role": "private_eval",
            "model": cfg.private_model_id or None,
            "usage_kind": "unavailable",
            "error": "PRIVATE_MODEL_ENDPOINT is not configured",
        }
    elif not cfg.private_model_attested or not cfg.private_model_isolation:
        res["private"] = {
            "status": "blocked",
            "role": "private_eval",
            "model": cfg.private_model_id,
            "usage_kind": "unavailable",
            "error": "private endpoint is not attested with documented egress isolation",
        }
    else:
        res["private"] = _role_diagnostic(cfg, "private", cfg.private_model_id, "private", live)

    # 4. Exa
    try:
        exa_budget = _sibling("exa_budget")
        live_keys = exa_budget.rotator_live()
        reserve = exa_budget.RESERVE
        if live_keys <= reserve:
            res["exa"] = {"status": "blocked", "live_keys": live_keys, "reserve": reserve, "error": f"live keys ({live_keys}) <= reserve floor ({reserve})"}
        else:
            rotator_url = getattr(cfg, "exa_rotator_url", EXA_ROTATOR)
            req = urllib.request.Request(
                f"{rotator_url}/search",
                data=json.dumps({"query": "test", "numResults": 1}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                res["exa"] = {"status": "ok", "live_keys": live_keys}
    except Exception as e:
        res["exa"] = {"status": "error", "error": str(e)[:300]}

    if "gemini" in res:
        res["gemini"]["role"] = "extract"
        res["gemini"]["usage_kind"] = "estimated" if res["gemini"].get("transport") in {"omp", "ssh-omp"} else "unknown"
        res["extract"] = dict(res["gemini"])
    res["all_ok"] = all(v.get("status") == "ok" for v in res.values() if isinstance(v, dict))
    return res


def classify_page(
    title: str,
    text: str,
    url: str,
    links: list[str] | list[tuple[str, str]] | list[dict] | None = None,
    engine: Any | None = None,
) -> str:
    """Classify page as 'hub', 'item', 'article', or 'junk'.
    Clean hubs use code-defined row extraction; ambiguous pages use bounded classification.
    """
    del engine
    lower_text = text.lower()
    lower_title = title.lower()
    url_path = urlsplit(url).path.lower()

    # 1. Junk detection
    if len(text.strip()) < 100:
        return "junk"
    if any(m in lower_text for m in ["domain for sale", "buy this domain", "page not found", "error 404", "parked domain"]):
        return "junk"

    link_count = len(links) if links is not None else len(re.findall(r"https?://", text))
    apply_count = len(re.findall(r"\b(apply|deadline|grant|scholarship|fellowship|hackathon|bounty|program|cohort)\b", lower_text))
    date_count = len(
        re.findall(
            r"\b202\d[-/]\d\d[-/]\d\d\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2},? 202\d\b",
            lower_text,
        )
    )

    # 2. Hub detection
    hub_keywords = ["list of grants", "directory", "all opportunities", "active hackathons", "grant directory", "active grants"]
    if any(h in lower_text or h in lower_title for h in hub_keywords):
        return "hub"
    if any(p in url_path for p in ["/grants-list", "/directory", "/opportunities", "/all-grants"]):
        return "hub"
    if link_count >= 10 and (apply_count >= 5 or date_count >= 3):
        return "hub"

    # 3. Item detection
    has_amount = bool(
        re.search(r"(\$|€|£|₹|rs\.?|usd|inr)\s*[\d,]+|\b[\d,]+\s*(usd|inr|eur|gbp|credits)\b", lower_text)
    )
    has_deadline = bool(re.search(r"\b(deadline|closes on|apply by|due date)\b", lower_text))
    has_eligibility = bool(re.search(r"\b(eligibility|who can apply|requirements|open to)\b", lower_text))

    if (has_amount or has_deadline) and (has_eligibility or "application" in lower_text or "fellowship" in lower_title or "award" in lower_title or "grant" in lower_title):
        return "item"

    return "article"


def compute_family_relevance(title: str, text: str, family_focus: str) -> float:
    """TF-IDF cosine relevance of page text against family focus."""
    if not family_focus or not text:
        return 0.5
    page_terms = topic_terms(title or "", text[:5000])
    focus_terms = topic_terms("", family_focus)
    dot = sum(page_terms[t] * focus_terms[t] for t in page_terms.keys() & focus_terms.keys())
    na = math.sqrt(sum(v * v for v in page_terms.values()))
    nb = math.sqrt(sum(v * v for v in focus_terms.values()))
    return dot / (na * nb) if na and nb else 0.0


def validate_extraction(data: dict, source_text: str, fallback_url: str = "") -> dict:
    """Validate extracted entities and claims against source text.
    Ensures verbatim quotes, valid numbers/dates, and sets fallback_url when entity link is missing.
    """
    hay = norm_text(source_text).casefold()
    rows, claims, names, links, dropped = [], [], [], [], 0

    for r in data.get("rows") or []:
        if not isinstance(r, dict):
            dropped += 1
            continue
        name = str(r.get("name") or "").strip()
        quote = str(r.get("evidence_quote") or "").strip()
        if not (name and _present(name, hay) and 40 <= len(quote) <= 400 and _present(quote, hay)):
            dropped += 1
            continue

        ok, why = mechanical_check(r, source_text)
        if not ok:
            dropped += 1
            continue

        raw_link = r.get("link")
        link = None
        if isinstance(raw_link, str) and raw_link.startswith(("http://", "https://")):
            if raw_link in source_text or _present(raw_link, hay):
                link = raw_link
        if not link and fallback_url:
            link = fallback_url

        amount = r.get("amount")
        try:
            amount = float(amount) if amount not in (None, "") else None
        except (TypeError, ValueError):
            amount = None

        dl = r.get("deadline")
        dl = dl if isinstance(dl, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", dl) else None

        rows.append({
            "name": norm_text(name)[:200],
            "org": (norm_text(str(r["org"]))[:200] if r.get("org") else None),
            "kind": r.get("kind") if r.get("kind") in ENTITY_KINDS else "other",
            "amount": amount,
            "currency": (str(r["currency"]).upper()[:3] if r.get("currency") else None),
            "deadline": dl,
            "eligibility": (norm_text(str(r["eligibility"]))[:500] if r.get("eligibility") else None),
            "location": (norm_text(str(r["location"]))[:200] if r.get("location") else None),
            "link": link,
            "evidence_quote": norm_text(quote),
        })

    for c in data.get("claims") or []:
        if isinstance(c, dict) and c.get("text"):
            cq = str(c.get("evidence_quote") or "").strip()
            if 40 <= len(cq) <= 400 and _present(cq, hay):
                if not (numbers_in(str(c["text"])) - numbers_in(source_text)):
                    claims.append({
                        "text": norm_text(str(c["text"]))[:500],
                        "evidence_quote": norm_text(cq),
                    })
                else:
                    dropped += 1
            else:
                dropped += 1
        else:
            dropped += 1

    for n in data.get("names") or []:
        if isinstance(n, str) and 2 < len(n) <= 120 and _present(n, hay):
            names.append(norm_text(n))

    for u in data.get("links") or []:
        if isinstance(u, str) and u.startswith(("http://", "https://")) and u in source_text:
            links.append(u)

    return {"rows": rows, "claims": claims, "names": sorted(set(names)), "links": links, "dropped": dropped}


# ---------------------------------------------------------------------------------------------
# Lock primitives, ported verbatim from docs/SYSTEM_DESIGN.md section 3. No LLM anywhere here.
# ---------------------------------------------------------------------------------------------


def norm_query(q: str) -> str:
    q = unicodedata.normalize("NFKC", q).casefold()
    q = re.sub(r"[^\w\s\-]", " ", q)
    return " ".join(sorted(set(q.split())))


def window_key(recheck_days: int, ts: int) -> int:
    return ts // (recheck_days * 86400)


def query_key(raw: str, source: str, recheck_days: int, ts: int) -> str:
    return hashlib.sha1(f"{norm_query(raw)}|{source}|{window_key(recheck_days, ts)}".encode()).hexdigest()


TRACKING_EXACT = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "si",
    "ref",
    "ref_src",
    "source",
    "spm",
    "trk",
    "_hsenc",
    "_hsmi",
    "oly_enc_id",
    "oly_anon_id",
    "vero_id",
    "wickedid",
    "feature",
    "utm",
}
HOST_PREFIXES = ("www.", "m.", "mobile.", "old.", "amp.", "new.")
INDEX_FILES = ("/index.html", "/index.htm", "/index.php", "/default.aspx")


def canonical_url(raw: str) -> str | None:
    try:
        u = urlsplit(raw.strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            return None
        host = u.hostname.encode("idna").decode().lower().rstrip(".")
    except (ValueError, UnicodeError):
        return None
    for p in HOST_PREFIXES:
        if host.startswith(p) and host.count(".") >= 2:
            host = host[len(p) :]
            break
    path = re.sub(r"/{2,}", "/", u.path or "/")
    m = re.match(r"^/r/[^/]+/comments/([a-z0-9]+)", path) if host in ("reddit.com", "redd.it") else None
    if m:
        return f"https://reddit.com/comments/{m.group(1)}"
    if host == "news.ycombinator.com":
        item = dict(parse_qsl(u.query)).get("id")
        if item:
            return f"https://news.ycombinator.com/item?id={item}"
    if host == "github.com":
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            return f"https://github.com/{parts[0].lower()}/{parts[1].lower()}"
    if host in ("x.com", "twitter.com"):
        m = re.match(r"^/([^/]+)/status/(\d+)", path)
        if m:
            return f"https://x.com/{m.group(1).lower()}/status/{m.group(2)}"
    for f in INDEX_FILES:
        if path.endswith(f):
            path = path[: -len(f)] or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    q = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_EXACT
    ]
    q.sort()
    return urlunsplit(("https", host, path, urlencode(q), ""))


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}))
    return re.sub(r"\s+", " ", s).strip()


def content_hash(main_text: str) -> str:
    return hashlib.sha256(norm_text(main_text).casefold()[:200_000].encode()).hexdigest()


MH_P = (1 << 61) - 1
MH_SEEDS = list(zip(range(1, 129), range(1000, 1128), strict=False))
BANDS, ROWS = 16, 8


def shingles(text: str, k: int = 3) -> set[int]:
    toks = norm_text(text).casefold().split()
    return {
        int.from_bytes(hashlib.blake2b(" ".join(toks[i : i + k]).encode(), digest_size=8).digest(), "big")
        for i in range(max(0, len(toks) - k + 1))
    }


def minhash(text: str) -> list[int]:
    sh = shingles(text)
    if not sh:
        return [0] * 128
    return [min(((a * s + b) % MH_P) & 0xFFFFFFFF for s in sh) for a, b in MH_SEEDS]


def band_keys(sig: list[int]) -> list[tuple[int, str]]:
    return [
        (b, hashlib.sha1(bytes(str(sig[b * ROWS : (b + 1) * ROWS]), "ascii")).hexdigest()[:16]) for b in range(BANDS)
    ]


def est_jaccard(a: list[int], b: list[int]) -> float:
    return sum(x == y for x, y in zip(a, b, strict=False)) / 128


def mh_pack(sig: list[int]) -> bytes:
    return b"".join(v.to_bytes(4, "little") for v in sig)


def mh_unpack(blob: bytes) -> list[int]:
    return [int.from_bytes(blob[i : i + 4], "little") for i in range(0, len(blob), 4)]


LEGAL_SUFFIX = {
    "inc",
    "inc.",
    "llc",
    "ltd",
    "ltd.",
    "limited",
    "corp",
    "corp.",
    "corporation",
    "co",
    "co.",
    "gmbh",
    "pvt",
    "plc",
    "sa",
    "ag",
}
NAME_STOP = {"the", "a", "an", "of", "for", "and", "&"}
YEAR_RX = re.compile(r"\b(20[2-3]\d)\b")


def norm_name(s: str) -> tuple[str, str | None]:
    s = unicodedata.normalize("NFKC", s).casefold()
    year = YEAR_RX.search(s)
    s = YEAR_RX.sub(" ", s)
    toks = [t for t in re.sub(r"[^\w\s]", " ", s).split() if t not in NAME_STOP and t not in LEGAL_SUFFIX]
    return " ".join(toks), (year.group(1) if year else None)


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.casefold()).strip("-")
    return s[:80].rstrip("-")


def entity_key(name: str, org: str | None, deadline_iso: str | None) -> str:
    n, y_in_name = norm_name(name)
    o, _ = norm_name(org) if org else ("", None)
    year = (deadline_iso or "")[:4] or y_in_name or "-"
    return f"{slug(n)}|{slug(o) or '-'}|{year}"


def char_trigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", " ", norm_text(s).casefold())
    return {s[i : i + 3] for i in range(len(s) - 2)}


def tokens(s: str) -> set[str]:
    return set(re.sub(r"[^\w\s]", " ", norm_text(s).casefold()).split())


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


def hybrid_similarity(a: str, b: str) -> float:
    return 0.5 * jaccard(char_trigrams(a), char_trigrams(b)) + 0.5 * jaccard(tokens(a), tokens(b))


MATERIAL = ("deadline", "amount", "currency", "status", "eligibility", "link", "location")


def entity_diff(old: dict, new: dict) -> list[tuple[str, str, str]]:
    out = []
    for f in MATERIAL:
        a, b = old.get(f), new.get(f)
        if f == "eligibility":
            changed = (a and b and hybrid_similarity(a, b) < 0.70) or bool(a) != bool(b)
        elif f == "link":
            changed = canonical_url(a or "") != canonical_url(b or "")
        elif f == "amount":
            changed = (float(a) if a is not None else None) != (float(b) if b is not None else None)
        else:
            changed = (a or None) != (b or None)
        if changed and b is not None:
            out.append((f, str(a), str(b)))
    return out


def stem(t: str) -> str:
    for suf in ("ings", "ing", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


def topic_terms(title: str, focus: str) -> Counter:
    return Counter(
        stem(t) for t in re.sub(r"[^\w\s]", " ", norm_text(f"{title} {focus}").casefold()).split() if t not in NAME_STOP
    )


def tfidf_cosine(a: Counter, b: Counter, df: dict[str, int], n_docs: int) -> float:
    def w(c: Counter) -> dict[str, float]:
        return {t: (1 + math.log(f)) * math.log((n_docs + 1) / (df.get(t, 0) + 1)) for t, f in c.items()}

    wa, wb = w(a), w(b)
    dot = sum(wa[t] * wb[t] for t in wa.keys() & wb.keys())
    na, nb = math.sqrt(sum(v * v for v in wa.values())), math.sqrt(sum(v * v for v in wb.values()))
    return dot / (na * nb) if na and nb else 0.0


QUERY_STOP = NAME_STOP | {
    "to",
    "in",
    "on",
    "or",
    "with",
    "by",
    "from",
    "now",
    "open",
    "our",
    "their",
    "that",
    "this",
    "are",
    "is",
    "be",
    "at",
    "as",
    "early",
    "stage",
    "new",
    "widely",
    "known",
    "before",
}


def keyword_query(text: str, n: int = 4) -> str:
    """Short AND-style query for keyword engines (Algolia, Arctic Shift, GitHub): first n content words."""
    words = [
        w for w in re.sub(r"[^\w\s\-]", " ", norm_text(text).casefold()).split() if w not in QUERY_STOP and len(w) > 2
    ]
    return " ".join(list(dict.fromkeys(words))[:n])


def fts_terms(text: str, n: int = 10) -> str:
    """An FTS5 OR query from plain words. Quoting each term keeps page text from becoming FTS syntax."""
    words = [w for w in re.sub(r"[^\w\s]", " ", text.casefold()).split() if len(w) > 2 and w not in NAME_STOP]
    seen: list[str] = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return " OR ".join(f'"{w}"' for w in seen[:n])


# ---------------------------------------------------------------------------------------------
# Safety (section 7): SSRF pinning, wall detection, BM25 filter, quote check, Snowflake dates.
# ---------------------------------------------------------------------------------------------

DENY_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
]
ALLOWED_PORTS = {80, 443}


class SSRFBlocked(Exception):
    pass


def resolve_and_pin(host: str, port: int) -> str:
    if port not in ALLOWED_PORTS:
        raise SSRFBlocked(f"port {port}")
    try:
        ipaddress.ip_address(host)
        raise SSRFBlocked("ip literal")
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFBlocked(f"dns: {e}") from e
    ips = {ipaddress.ip_address(i[4][0]) for i in infos}
    if not ips:
        raise SSRFBlocked("no A/AAAA")
    for ip in ips:  # ALL answers must be public: a mixed answer is a rebinding attempt
        if any(ip in n for n in DENY_NETS):
            raise SSRFBlocked(f"{ip} denied")
    return str(min(ips, key=lambda i: (i.version, str(i))))


class _Stripper(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "nav", "footer", "header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._anchor).split())[:200]))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)


def parse_html(html: str) -> tuple[str, str, list[tuple[str, str]]]:
    """(title, main text, links). trafilatura when installed, the stdlib stripper otherwise."""
    p = _Stripper()
    with contextlib.suppress(Exception):
        p.feed(html)
    text = "\n".join(norm_text(x) for x in "".join(p.parts).split("\n") if x.strip())
    try:
        import trafilatura  # optional: spec 3.2 prefers it for hash stability

        t = trafilatura.extract(html, include_links=False, include_comments=False)
        if t:
            text = t
    except ImportError:
        pass
    return norm_text(p.title)[:300], text, p.links


def strip_tags(body: str) -> str:
    return parse_html(body)[1]


WALL_STATUS = {401, 403, 407, 451}
CHALLENGE_RX = re.compile(
    r"(cf-chl|__cf_chl_|challenge-platform|Just a moment\.\.\.|Attention Required!|cf-turnstile|"
    r"Please verify you are a human|hcaptcha\.com|recaptcha/api|g-recaptcha|px-captcha|perimeterx|"
    r"datadome|Access Denied.{0,40}akamai|Checking your browser|enable JavaScript and cookies to continue)",
    re.IGNORECASE,
)
LOGIN_RX = re.compile(r'<input[^>]+type="password"', re.IGNORECASE)


def classify_response(status: int, body: str) -> tuple[str, str | None]:
    """(outcome, wall_kind). Outcome is ok | walled | outage | failure."""
    if status in WALL_STATUS:
        return "walled", "http"
    if status == 200 and CHALLENGE_RX.search(body[:20000]):
        return "walled", "challenge"
    if status == 200 and LOGIN_RX.search(body) and len(norm_text(strip_tags(body))) < 600:
        return "walled", "login"
    if status in (429, 500, 502, 503, 504):
        return "outage", None
    if status >= 400:
        return "failure", None
    if len(norm_text(strip_tags(body))) < 200:
        return "failure", None
    return "ok", None


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b, self.docs = k1, b, docs
        self.avgdl = sum(map(len, docs)) / max(1, len(docs))
        self.df = Counter(t for d in docs for t in set(d))
        self.N = len(docs)

    def idf(self, t: str) -> float:
        return math.log(1 + (self.N - self.df.get(t, 0) + 0.5) / (self.df.get(t, 0) + 0.5))

    def score(self, q: list[str], i: int) -> float:
        d, tf = self.docs[i], Counter(self.docs[i])
        return sum(
            self.idf(t) * tf[t] * (self.k1 + 1) / (tf[t] + self.k1 * (1 - self.b + self.b * len(d) / self.avgdl))
            for t in q
            if t in tf
        )


def filter_chunks(text: str, query: str, chunk_chars: int = 800, keep_ratio: float = 0.5, top: int = 25) -> list[str]:
    chunks = [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]
    toks = [re.sub(r"[^\w\s]", " ", c.casefold()).split() for c in chunks]
    bm = BM25(toks)
    q = re.sub(r"[^\w\s]", " ", query.casefold()).split()
    scored = sorted(((bm.score(q, i), i) for i in range(len(chunks))), reverse=True)
    if not scored or scored[0][0] <= 0:
        return chunks[:top]
    mx = scored[0][0]
    keep = sorted(i for s, i in scored[:top] if s >= keep_ratio * mx)
    return [chunks[i] for i in keep]


def quote_ok(quote: str, page_text: str) -> tuple[bool, int, int]:
    q, t = norm_text(quote).casefold(), norm_text(page_text).casefold()
    i = t.find(q)
    if i >= 0:
        return True, i, i + len(q)
    qt, tt = q.split(), t.split()
    if len(qt) < 6:
        return False, -1, -1
    qs = set(qt)
    best = (0.0, -1)
    for start in range(max(1, len(tt) - len(qt) + 1)):
        j = jaccard(qs, set(tt[start : start + len(qt)]))
        if j > best[0]:
            best = (j, start)
    if best[0] >= 0.90:
        s = len(" ".join(tt[: best[1]])) + (1 if best[1] else 0)
        return True, s, s + len(" ".join(tt[best[1] : best[1] + len(qt)]))
    return False, -1, -1


SNOWFLAKE_EPOCH_MS = 1288834974657


def snowflake_utc(post_id: str) -> datetime | None:
    try:
        v = int(post_id)
    except (TypeError, ValueError):
        return None
    if v < (1 << 22):
        return None
    return datetime.fromtimestamp(((v >> 22) + SNOWFLAKE_EPOCH_MS) / 1000, tz=timezone.utc)


SECRET_RX = re.compile(r"(sk-qwen-[A-Za-z0-9]+|AQ\.[A-Za-z0-9_-]{20}|AIza[0-9A-Za-z_-]{30,}|[A-Za-z0-9+/]{120,}={0,2})")


def injection_hits(text: str) -> list[str]:
    if hasattr(quarantine, "scan_text"):
        return quarantine.scan_text(text)
    return sorted({name for rx, name in quarantine.RX if rx.search(text)})


def numbers_in(s: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*\d|\d{2,}", s or "")}


def material_numbers(s: str) -> set[str]:
    """Amounts and other figures. Four-digit years are not material by themselves."""
    return {n for n in numbers_in(s) if not re.fullmatch(r"20[0-3]\d", n)}


def numbers_compatible(a: str, b: str) -> bool:
    """Same amounts may merge. A year mentioned on only one side is not a different fact.
    Two different amounts, or two different years that both appear, must stay apart.
    """
    if material_numbers(a) != material_numbers(b):
        return False
    years_a = {n for n in numbers_in(a) if re.fullmatch(r"20[0-3]\d", n)}
    years_b = {n for n in numbers_in(b) if re.fullmatch(r"20[0-3]\d", n)}
    if years_a and years_b and years_a != years_b:
        return False
    return True


# ---------------------------------------------------------------------------------------------
# Schema: section 5.1 verbatim, plus the orchestrator extension (private flag, per-engine spend).
# ---------------------------------------------------------------------------------------------

PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 268435456;
"""

DDL = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta VALUES ('schema_version', '4');
CREATE TABLE families (
  name TEXT PRIMARY KEY, recheck_days INTEGER NOT NULL CHECK (recheck_days > 0),
  weight REAL NOT NULL DEFAULT 1.0, relevance_floor REAL NOT NULL DEFAULT 0.08, focus TEXT NOT NULL);
CREATE TABLE topics (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, family TEXT NOT NULL REFERENCES families(name),
  title TEXT NOT NULL, focus TEXT NOT NULL, focus_variants TEXT NOT NULL DEFAULT '[]',
  parent_id INTEGER REFERENCES topics(id), depth INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','dormant','dropped')),
  discovered_via TEXT NOT NULL CHECK (discovered_via IN ('seed','hub','name','backlink','similar')),
  hops_dry INTEGER NOT NULL DEFAULT 0, run_count INTEGER NOT NULL DEFAULT 0,
  new_items_total INTEGER NOT NULL DEFAULT 0, fetch_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, last_run_at INTEGER);
CREATE VIRTUAL TABLE topics_fts USING fts5(title, focus, content='topics', content_rowid='id', tokenize='unicode61');
CREATE TRIGGER topics_ai AFTER INSERT ON topics BEGIN
  INSERT INTO topics_fts(rowid, title, focus) VALUES (new.id, new.title, new.focus); END;
CREATE TRIGGER topics_ad AFTER DELETE ON topics BEGIN
  INSERT INTO topics_fts(topics_fts, rowid, title, focus) VALUES ('delete', old.id, old.title, old.focus); END;
CREATE TRIGGER topics_au AFTER UPDATE OF title, focus ON topics BEGIN
  INSERT INTO topics_fts(topics_fts, rowid, title, focus) VALUES ('delete', old.id, old.title, old.focus);
  INSERT INTO topics_fts(rowid, title, focus) VALUES (new.id, new.title, new.focus); END;
CREATE TABLE topic_terms_df (term TEXT PRIMARY KEY, df INTEGER NOT NULL);
CREATE TABLE queries (
  id INTEGER PRIMARY KEY, query_key TEXT NOT NULL UNIQUE, norm TEXT NOT NULL, raw TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('exa','arctic','hn','github','grok')), window_key INTEGER NOT NULL,
  topic_id INTEGER REFERENCES topics(id), name_id INTEGER REFERENCES names(id),
  discovered_via TEXT NOT NULL CHECK (discovered_via IN ('seed','hub','name','backlink','similar','gap','hypothesis')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
  priority REAL NOT NULL DEFAULT 0.5, scheduled_at INTEGER NOT NULL, ran_at INTEGER, result_count INTEGER,
  new_pages INTEGER NOT NULL DEFAULT 0, dup_requests INTEGER NOT NULL DEFAULT 0);
CREATE INDEX queries_norm_source ON queries(norm, source, window_key);
CREATE INDEX queries_pending ON queries(status, priority DESC) WHERE status='pending';
CREATE TABLE domains (
  domain TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','paused','blocked')),
  junk_count INTEGER NOT NULL DEFAULT 0, walled_count INTEGER NOT NULL DEFAULT 0,
  outage_streak INTEGER NOT NULL DEFAULT 0, paused_until INTEGER, rate_per_min INTEGER NOT NULL DEFAULT 6,
  fetch_count INTEGER NOT NULL DEFAULT 0, new_items_total INTEGER NOT NULL DEFAULT 0,
  paid_failures INTEGER NOT NULL DEFAULT 0, robots_txt TEXT, robots_fetched_at INTEGER,
  feedback_score REAL NOT NULL DEFAULT 0.0);
CREATE TABLE pages (
  id INTEGER PRIMARY KEY, canon_url TEXT NOT NULL UNIQUE, raw_urls TEXT NOT NULL DEFAULT '[]',
  domain TEXT NOT NULL REFERENCES domains(domain), content_hash TEXT, minhash BLOB,
  alias_of INTEGER REFERENCES pages(id), reprint_of INTEGER REFERENCES pages(id),
  kind TEXT CHECK (kind IN ('hub','item','article','junk','unsure')), predicted_kind TEXT,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','fetched','classified','scheduled',
    'walled','dead','quarantined','alias','reprint','dropped')),
  wall_kind TEXT CHECK (wall_kind IN ('http','challenge','login','robots','budget')),
  walled_count INTEGER NOT NULL DEFAULT 0, http_status INTEGER, route TEXT, title TEXT, text_len INTEGER,
  truncated INTEGER NOT NULL DEFAULT 0, raw_path TEXT, fetched_at INTEGER, fetch_count INTEGER NOT NULL DEFAULT 0,
  next_check_at INTEGER NOT NULL, next_attempt_at INTEGER, interval_sec INTEGER NOT NULL,
  strikes INTEGER NOT NULL DEFAULT 0, depth INTEGER NOT NULL DEFAULT 0, hops_dry INTEGER NOT NULL DEFAULT 0,
  parent_page_id INTEGER REFERENCES pages(id), topic_id INTEGER REFERENCES topics(id),
  discovered_via TEXT NOT NULL CHECK (discovered_via IN ('seed','hub','name','backlink','similar','redirect','feed','gap','hypothesis')),
  novelty REAL NOT NULL DEFAULT 1.0, value REAL NOT NULL DEFAULT 0.5, relevance REAL,
  new_items_total INTEGER NOT NULL DEFAULT 0, noise_count INTEGER NOT NULL DEFAULT 0,
  quarantined INTEGER NOT NULL DEFAULT 0, quarantine_reasons TEXT, dup_requests INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL);
CREATE INDEX pages_frontier ON pages(status, next_check_at) WHERE status IN ('queued','scheduled');
CREATE INDEX pages_domain ON pages(domain, status);
CREATE INDEX pages_hash ON pages(content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX pages_parent ON pages(parent_page_id);
CREATE TABLE minhash_bands (band INTEGER NOT NULL, bucket TEXT NOT NULL,
  page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE, PRIMARY KEY (band, bucket, page_id));
CREATE TABLE page_links (
  from_page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE, to_canon_url TEXT NOT NULL,
  anchor TEXT, position INTEGER NOT NULL, PRIMARY KEY (from_page_id, to_canon_url));
CREATE INDEX page_links_to ON page_links(to_canon_url);
CREATE TABLE hubs (
  page_id INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE, item_count INTEGER NOT NULL DEFAULT 0,
  rows_added_total INTEGER NOT NULL DEFAULT 0, last_diff_at INTEGER, feed_url TEXT, link_template TEXT);
CREATE TABLE hub_rows (
  hub_page_id INTEGER NOT NULL REFERENCES hubs(page_id) ON DELETE CASCADE, row_hash TEXT NOT NULL,
  to_canon_url TEXT NOT NULL, anchor TEXT, first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
  entity_key TEXT REFERENCES entities(key), PRIMARY KEY (hub_page_id, row_hash));
CREATE TABLE fetches (
  id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE, at INTEGER NOT NULL,
  route TEXT NOT NULL CHECK (route IN ('direct','exa_contents','zenrows','resi_proxy','arctic','hn','github','grok','feed','browser')),
  http_status INTEGER, bytes INTEGER, ms INTEGER,
  outcome TEXT NOT NULL CHECK (outcome IN ('ok','walled','outage','failure','ssrf_blocked','oversize','deadline')),
  pinned_ip TEXT, due INTEGER NOT NULL);
CREATE INDEX fetches_page_at ON fetches(page_id, at);
CREATE TABLE paid_fetches (
  id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES pages(id),
  provider TEXT NOT NULL CHECK (provider IN ('zenrows','resi_proxy','2captcha')), cost_units INTEGER NOT NULL,
  at INTEGER NOT NULL, outcome TEXT,
  reservation_id INTEGER, reserved_micro_usd INTEGER DEFAULT 0, actual_micro_usd INTEGER,
  task_ref TEXT, reservation_state TEXT);
CREATE INDEX paid_fetches_day ON paid_fetches(at);
CREATE TABLE access_attempts (
  id INTEGER PRIMARY KEY,
  fetch_id INTEGER REFERENCES fetches(id) ON DELETE CASCADE,
  page_id INTEGER REFERENCES pages(id) ON DELETE CASCADE,
  mode TEXT NOT NULL, route TEXT, status TEXT, http_status INTEGER,
  bytes INTEGER DEFAULT 0, ms INTEGER DEFAULT 0, error TEXT, at INTEGER NOT NULL);
CREATE INDEX access_attempts_fetch ON access_attempts(fetch_id);
CREATE INDEX access_attempts_page ON access_attempts(page_id);
CREATE TABLE paid_budget_reservations (
  id INTEGER PRIMARY KEY,
  acquisition_id TEXT NOT NULL CHECK (length(acquisition_id) > 0),
  provider TEXT NOT NULL CHECK (length(provider) > 0),
  reserved_micro_usd INTEGER NOT NULL CHECK (reserved_micro_usd >= 0),
  actual_micro_usd INTEGER CHECK (actual_micro_usd IS NULL OR actual_micro_usd >= 0),
  settled INTEGER NOT NULL DEFAULT 0 CHECK (settled IN (0, 1)),
  outcome TEXT, task_ref TEXT,
  is_solver INTEGER NOT NULL DEFAULT 0 CHECK (is_solver IN (0, 1)),
  created_at_utc TEXT NOT NULL,
  window_day_ist TEXT NOT NULL CHECK (
    window_day_ist GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  window_month_ist TEXT NOT NULL CHECK (window_month_ist GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'));
CREATE INDEX paid_budget_reservations_day ON paid_budget_reservations(window_day_ist, provider, settled);
CREATE INDEX paid_budget_reservations_month ON paid_budget_reservations(window_month_ist, provider, is_solver);
CREATE TABLE names (
  id INTEGER PRIMARY KEY, norm_name TEXT NOT NULL UNIQUE, raw TEXT NOT NULL,
  first_page_id INTEGER NOT NULL REFERENCES pages(id), topic_id INTEGER REFERENCES topics(id),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','searched','resolved','dropped')),
  searched_at INTEGER, entity_key TEXT REFERENCES entities(key), created_at INTEGER NOT NULL);
CREATE TABLE entities (
  key TEXT PRIMARY KEY, name TEXT NOT NULL, norm_name TEXT NOT NULL, org TEXT, norm_org TEXT, year TEXT,
  aliases TEXT NOT NULL DEFAULT '[]',
  kind TEXT NOT NULL CHECK (kind IN ('grant','hackathon','fellowship','scholarship','credits','accelerator',
    'competitor','tool','model','other')),
  family TEXT NOT NULL REFERENCES families(name), amount REAL, currency TEXT, deadline TEXT, eligibility TEXT,
  location TEXT, link TEXT,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','known','changed','expired')),
  relevance REAL NOT NULL DEFAULT 0.5, urgency REAL NOT NULL DEFAULT 0.0, evidence_quote TEXT NOT NULL,
  evidence_page_id INTEGER NOT NULL REFERENCES pages(id), first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL, last_changed_at INTEGER, reported_at INTEGER, alerted_at INTEGER, wiki_path TEXT);
CREATE INDEX entities_org_year ON entities(norm_org, year);
CREATE INDEX entities_link ON entities(link);
CREATE INDEX entities_unreported ON entities(reported_at) WHERE reported_at IS NULL;
CREATE INDEX entities_deadline ON entities(deadline) WHERE deadline IS NOT NULL;
CREATE TABLE entity_sources (
  entity_key TEXT NOT NULL REFERENCES entities(key) ON DELETE CASCADE, page_id INTEGER NOT NULL REFERENCES pages(id),
  seen_at INTEGER NOT NULL, PRIMARY KEY (entity_key, page_id));
CREATE TABLE entity_changes (
  id INTEGER PRIMARY KEY, entity_key TEXT NOT NULL REFERENCES entities(key) ON DELETE CASCADE,
  field TEXT NOT NULL, old TEXT, new TEXT NOT NULL, seen_at INTEGER NOT NULL,
  page_id INTEGER NOT NULL REFERENCES pages(id), evidence_quote TEXT NOT NULL, reported_at INTEGER);
CREATE INDEX entity_changes_unreported ON entity_changes(reported_at) WHERE reported_at IS NULL;
CREATE TABLE claims (
  hash TEXT PRIMARY KEY, text TEXT NOT NULL, norm_text TEXT NOT NULL, n_sources INTEGER NOT NULL DEFAULT 1,
  topic_id INTEGER REFERENCES topics(id), entity_key TEXT REFERENCES entities(key) ON DELETE SET NULL,
  first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL);
CREATE VIRTUAL TABLE claims_fts USING fts5(norm_text, content='claims', content_rowid='rowid', tokenize='unicode61');
CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
  INSERT INTO claims_fts(rowid, norm_text) VALUES (new.rowid, new.norm_text); END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, norm_text) VALUES ('delete', old.rowid, old.norm_text); END;
CREATE TABLE claim_sources (
  claim_hash TEXT NOT NULL REFERENCES claims(hash) ON DELETE CASCADE, page_id INTEGER NOT NULL REFERENCES pages(id),
  quote_start INTEGER NOT NULL, quote_end INTEGER NOT NULL, PRIMARY KEY (claim_hash, page_id));
CREATE TABLE claim_variants (claim_hash TEXT NOT NULL REFERENCES claims(hash) ON DELETE CASCADE,
  text TEXT NOT NULL, PRIMARY KEY (claim_hash, text));
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('fetch','search','extract','verify','evaluate','compile','deepen','digest')),
  payload TEXT NOT NULL, priority REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','done','failed','paused')),
  worker_id TEXT, occurrence_seq INTEGER NOT NULL DEFAULT 0, claimed_at INTEGER, heartbeat_at INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, next_attempt_at INTEGER NOT NULL,
  outage_pauses INTEGER NOT NULL DEFAULT 0, wave_id INTEGER REFERENCES waves(id), result TEXT, error TEXT,
  created_at INTEGER NOT NULL);
CREATE INDEX tasks_pending ON tasks(kind, status, next_attempt_at, priority DESC) WHERE status='pending';
CREATE INDEX tasks_claimed ON tasks(heartbeat_at) WHERE status='claimed';
CREATE TABLE waves (
  id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL, ended_at INTEGER,
  status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','aborted','budget_cut')),
  fetches INTEGER NOT NULL DEFAULT 0, exa_calls INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0, new_entities INTEGER NOT NULL DEFAULT 0,
  changed_entities INTEGER NOT NULL DEFAULT 0, new_claims INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE budgets (
  day TEXT PRIMARY KEY, exa_calls INTEGER NOT NULL DEFAULT 0, llm_tokens_in INTEGER NOT NULL DEFAULT 0,
  llm_tokens_out INTEGER NOT NULL DEFAULT 0, fetches INTEGER NOT NULL DEFAULT 0,
  paid_fetches INTEGER NOT NULL DEFAULT 0, paid_cost_units INTEGER NOT NULL DEFAULT 0);
CREATE TABLE wiki_pages (
  path TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('entity','topic','hub','index')), ref_key TEXT,
  title TEXT NOT NULL, body_hash TEXT NOT NULL, frontmatter TEXT NOT NULL, compiled_at INTEGER NOT NULL,
  dirty INTEGER NOT NULL DEFAULT 0, stale INTEGER NOT NULL DEFAULT 0, stale_reason TEXT, stale_origin TEXT,
  stale_marked_at INTEGER);
CREATE INDEX wiki_pages_dirty ON wiki_pages(dirty, stale);
CREATE TABLE wiki_links (from_path TEXT NOT NULL, to_path TEXT NOT NULL, PRIMARY KEY (from_path, to_path));
CREATE INDEX wiki_links_to ON wiki_links(to_path);
CREATE VIRTUAL TABLE wiki_fts USING fts5(title, body, path UNINDEXED, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE digest_items (
  id INTEGER PRIMARY KEY, day TEXT NOT NULL, ref_kind TEXT NOT NULL CHECK (ref_kind IN ('entity','change')),
  ref_id TEXT NOT NULL, rank INTEGER NOT NULL, reason TEXT NOT NULL, sent_at INTEGER, message_id TEXT,
  UNIQUE (ref_kind, ref_id));
CREATE TABLE feedback (digest_item_id INTEGER NOT NULL REFERENCES digest_items(id),
  reaction TEXT NOT NULL CHECK (reaction IN ('up','down')), at INTEGER NOT NULL);
"""

# Orchestrator extension. Additive only, so the section 5.1 tables stay exactly as specified.
ORCH_DDL = """
CREATE TABLE IF NOT EXISTS engine_spend (
  day TEXT NOT NULL, engine TEXT NOT NULL CHECK (engine IN ('gemini','grok','local','review','plan')),
  calls INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
  outages INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, engine));
CREATE TABLE IF NOT EXISTS engine_state (
  engine TEXT PRIMARY KEY, paused_until INTEGER, outage_streak INTEGER NOT NULL DEFAULT 0,
  failure_strikes INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS verify_log (
  id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES pages(id), engine TEXT NOT NULL,
  item TEXT NOT NULL, verdict TEXT NOT NULL CHECK (verdict IN ('keep','drop')), tier INTEGER NOT NULL,
  reason TEXT, india_eligible TEXT, at INTEGER NOT NULL);
"""


def _guarded_db_path(path: Path, *, must_exist: bool) -> Path:
    """Refuse a reparse ancestor or DB leaf before resolve or SQLite.

    ``guard_directory`` is lexical (abspath, no follow) and refuses an ancestor
    reparse. ``is_reparse`` lstats the DB leaf itself. ``Path.resolve`` and
    ``sqlite3.connect`` both follow, so they run only after those checks.
    """
    migration = _sibling("migration")
    leaf = Path(path)
    parent = migration.guard_directory(leaf.parent, must_exist=must_exist)
    if not leaf.name or leaf.name in (".", ".."):
        raise RuntimeError("Directory path is not a directory.")
    absolute = parent / leaf.name
    if migration.is_reparse(absolute):
        raise RuntimeError("Symlink or Windows reparse traversal refused.")
    return absolute.resolve()


class Store:
    """One SQLite connection. Every multi-statement write goes through tx(): BEGIN IMMEDIATE, busy retry."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.db = conn
        self.db.row_factory = sqlite3.Row
        self.readonly = False
        self.path = None

    @classmethod
    def open(cls, path: Path) -> Store:
        """Create a fresh v4 database, or connect an existing v4 without migrations."""
        path = _guarded_db_path(path, must_exist=False)
        if path.exists():
            return cls.connect_existing(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
        conn.executescript(PRAGMAS)
        store = cls(conn)
        store.path = path
        store.storage_root = path.parent.parent if path.parent.name == "data" else path.parent
        try:
            store.init_schema()
        except BaseException:
            conn.close()
            raise
        return store

    @classmethod
    def connect_existing(cls, path: Path, readonly: bool = False) -> Store:
        path = _guarded_db_path(path, must_exist=True)
        conn = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                               uri=True, isolation_level=None, timeout=5.0)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            _sibling("migration").assert_current(conn)
        except BaseException:
            conn.close()
            raise
        store = cls(conn)
        store.path = path
        store.readonly = bool(readonly)
        store.storage_root = path.parent.parent if path.parent.name == "data" else path.parent
        return store

    @classmethod
    def migrate(cls, path: Path, bundle_root: Path | None = None) -> dict[str, Any]:
        path = _guarded_db_path(path, must_exist=False)
        if not path.exists():
            store = cls.open(path)
            store.db.close()
            return {"schema_version": 4, "changed": True, "bundle": None, "fresh": True}
        conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5.0)
        try:
            root = path.parent.parent if path.parent.name == "data" else path.parent
            return _sibling("migration").migrate(conn, root, DDL, ORCH_DDL, _sibling("goal_schema").DDL, bundle_root)
        finally:
            conn.close()

    def snapshot(self) -> Store:
        """An in-memory copy for --dry-run. Writes land in the copy and vanish with the process."""
        mem = sqlite3.connect(":memory:", isolation_level=None)
        self.db.backup(mem)
        mem.execute("PRAGMA foreign_keys = ON")
        copy = Store(mem)
        copy.readonly = False
        copy.path = getattr(self, "path", None)
        copy.storage_root = getattr(self, "storage_root", ROOT)
        return copy

    @classmethod
    def in_memory(cls) -> Store:
        """A fresh in-memory database with full schema initialized."""
        mem = sqlite3.connect(":memory:", isolation_level=None, timeout=5.0)
        mem.executescript(PRAGMAS)
        s = cls(mem)
        s.init_schema()
        return s

    def init_schema(self) -> None:
        """Initialization only. Existing databases require the explicit migrate command."""
        _sibling("migration").initialize(self.db, DDL, ORCH_DDL, _sibling("goal_schema").DDL)
    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self.db.in_transaction:
            savepoint = "snowball_" + secrets.token_hex(8)
            self.db.execute(f"SAVEPOINT {savepoint}")
            try:
                yield self.db
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            return
        for n in range(6):
            try:
                self.db.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e) or n == 5:
                    raise
                time.sleep(0.05 * 2**n)
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()


# ---------------------------------------------------------------------------------------------
# Token budget middleware (section 6.5, extended per engine) and engine outage bookkeeping.
# ---------------------------------------------------------------------------------------------


class EngineOutage(Exception):
    """429, 5xx, timeout. Pauses the engine, refunds the task attempt."""

    def __init__(self, msg: str, retry_after: int | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


class EngineFailure(Exception):
    """404, 400, auth, malformed output. Counts a strike against the task."""


class BudgetExhausted(Exception):
    pass


def est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


@dataclass
class TaskBudget:
    engine: str
    max_in: int
    max_out: int
    used_in: int = 0
    used_out: int = 0
    partial: bool = False

    @property
    def frac_in(self) -> float:
        return self.used_in / self.max_in if self.max_in else 1.0


class TokenBudgetMiddleware:
    """Daily caps per engine plus the global LLM cap, and per-task budgets with the 80%/100% rule.

    Spend is reserved from an estimate before the call and reconciled with real usage after it, so a
    crash mid-call still counts. Daily rows live in engine_spend (per engine) and budgets (global).
    """

    def __init__(self, store: Store, cfg: Config) -> None:
        self.store, self.cfg = store, cfg
        self._lock = threading.Lock()

    def spent(self, engine: str | None = None) -> tuple[int, int]:
        day = today_ist()
        if engine:
            r = self.store.one("SELECT tokens_in, tokens_out FROM engine_spend WHERE day=? AND engine=?", (day, engine))
        else:
            r = self.store.one(
                "SELECT llm_tokens_in AS tokens_in, llm_tokens_out AS tokens_out FROM budgets WHERE day=?", (day,)
            )
        return (r["tokens_in"], r["tokens_out"]) if r else (0, 0)

    def tokens_today(self, engine: str) -> int:
        ti, to = self.spent(engine)
        return ti + to

    def remaining(self, engine: str) -> int:
        ti, to = self.spent(engine)
        gi, go = self.spent()
        return max(0, min(self.cfg.caps[engine] - ti - to, self.cfg.llm_tokens_per_day - gi - go))

    def engine_available(self, engine: str) -> tuple[bool, str]:
        if self.remaining(engine) <= 0:
            return False, "daily token cap reached"
        st = self.store.one("SELECT paused_until FROM engine_state WHERE engine=?", (engine,))
        if st and st["paused_until"] and st["paused_until"] > now():
            return False, f"paused for {st['paused_until'] - now()}s after an outage"
        return True, "ok"

    def task(self, engine: str, max_in: int, max_out: int) -> TaskBudget:
        return TaskBudget(engine, max_in, max_out)

    def reserve(self, tb: TaskBudget, prompt_tokens: int, out_tokens: int) -> None:
        if tb.used_in >= tb.max_in:
            tb.partial = True
            raise BudgetExhausted(f"{tb.engine} task input budget spent ({tb.used_in}/{tb.max_in})")
        if prompt_tokens < 0 or out_tokens < 0:
            raise BudgetExhausted("negative reservation")
        # Cap check and the spend insert share one immediate transaction.
        self._add(tb.engine, prompt_tokens, out_tokens, calls=1, enforce_cap=True)
        tb.used_in += prompt_tokens
        tb.used_out += out_tokens

    def reconcile(self, tb: TaskBudget, est_in: int, est_out: int, real_in: int | None, real_out: int | None) -> None:
        di = (real_in - est_in) if real_in is not None else 0
        do = (real_out - est_out) if real_out is not None else 0
        if di or do:
            self._add(tb.engine, di, do, calls=0)
            tb.used_in = max(0, tb.used_in + di)
            tb.used_out = max(0, tb.used_out + do)

    def _spent_in(self, db: sqlite3.Connection, engine: str | None, day: str) -> tuple[int, int]:
        if engine:
            row = db.execute(
                "SELECT tokens_in, tokens_out FROM engine_spend WHERE day=? AND engine=?",
                (day, engine),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT llm_tokens_in AS tokens_in, llm_tokens_out AS tokens_out FROM budgets WHERE day=?",
                (day,),
            ).fetchone()
        if not row:
            return (0, 0)
        return (int(row["tokens_in"] or 0), int(row["tokens_out"] or 0))

    def _add(self, engine: str, tin: int, tout: int, calls: int, enforce_cap: bool = False) -> None:
        day = today_ist()
        with self._lock, self.store.tx() as db:
            if enforce_cap:
                eti, eto = self._spent_in(db, engine, day)
                gti, gto = self._spent_in(db, None, day)
                left = max(0, min(self.cfg.caps[engine] - eti - eto, self.cfg.llm_tokens_per_day - gti - gto))
                if left < tin + tout:
                    raise BudgetExhausted(f"{engine} daily cap: {left} tokens left")
            db.execute(
                "INSERT INTO engine_spend(day, engine, calls, tokens_in, tokens_out) VALUES (?,?,?,?,?) "
                "ON CONFLICT(day, engine) DO UPDATE SET "
                "calls=MAX(0, engine_spend.calls + ?), "
                "tokens_in=MAX(0, engine_spend.tokens_in + ?), "
                "tokens_out=MAX(0, engine_spend.tokens_out + ?)",
                (day, engine, max(0, calls), max(0, tin), max(0, tout), calls, tin, tout),
            )
            db.execute(
                "INSERT INTO budgets(day, llm_tokens_in, llm_tokens_out) VALUES (?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "llm_tokens_in=MAX(0, budgets.llm_tokens_in + ?), "
                "llm_tokens_out=MAX(0, budgets.llm_tokens_out + ?)",
                (day, max(0, tin), max(0, tout), tin, tout),
            )

    def on_outage(self, engine: str, retry_after: int | None, err: str) -> int:
        """Outage: pause the engine with 15 min x 2^n backoff (cap 24 h). Strikes are not touched."""
        with self._lock, self.store.tx() as db:
            r = db.execute("SELECT outage_streak FROM engine_state WHERE engine=?", (engine,)).fetchone()
            n = (r["outage_streak"] if r else 0) + 1
            pause = retry_after or min(900 * 2 ** (n - 1), 86400)
            db.execute(
                "INSERT INTO engine_state(engine, paused_until, outage_streak, last_error) VALUES (?,?,?,?) "
                "ON CONFLICT(engine) DO UPDATE SET paused_until=excluded.paused_until, "
                "outage_streak=excluded.outage_streak, last_error=excluded.last_error",
                (engine, now() + pause, n, err[:300]),
            )
            db.execute(
                "INSERT INTO engine_spend(day, engine, outages) VALUES (?,?,1) ON CONFLICT(day, engine) "
                "DO UPDATE SET outages=outages+1",
                (today_ist(), engine),
            )
        return pause

    def on_failure(self, engine: str, err: str) -> None:
        with self._lock, self.store.tx() as db:
            db.execute(
                "INSERT INTO engine_state(engine, failure_strikes, last_error) VALUES (?,1,?) "
                "ON CONFLICT(engine) DO UPDATE SET failure_strikes=failure_strikes+1, last_error=excluded.last_error",
                (engine, err[:300]),
            )
            db.execute(
                "INSERT INTO engine_spend(day, engine, failures) VALUES (?,?,1) ON CONFLICT(day, engine) "
                "DO UPDATE SET failures=failures+1",
                (today_ist(), engine),
            )

    def on_success(self, engine: str) -> None:
        with self._lock, self.store.tx() as db:
            db.execute(
                "INSERT INTO engine_state(engine, outage_streak, failure_strikes) VALUES (?,0,0) "
                "ON CONFLICT(engine) DO UPDATE SET outage_streak=0, failure_strikes=0",
                (engine,),
            )


# ---------------------------------------------------------------------------------------------
# Public extract/review/plan engines live in tools/engine_transport.py.
# GROK_X_* remains the only X acquisition path. It never reviews evidence.
# ---------------------------------------------------------------------------------------------


def transport_module() -> Any:
    return _sibling("engine_transport")


class XAcquisition:
    """Live X search through the grok CLI. Model text is never stored as a post body."""

    def __init__(self, cfg: Config, budget: TokenBudgetMiddleware | None, dry: bool) -> None:
        self.cfg, self.budget, self.dry = cfg, budget, dry
        self.planned: list[str] = []

    def x_search(self, query: str, window_days: int) -> list[dict]:
        if self.dry:
            self.planned.append(f"grok-x: search X for {query!r} over {window_days}d")
            return []
        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).date().isoformat()
        prompt = (
            f"Search X (Twitter) for posts since {since} about: {query}\n"
            "Focus on grants, funding calls, credit programs, giveaway codes and launches. "
            "Return ONLY a JSON array, at most 15 items: "
            '[{"url":"https://x.com/<handle>/status/<id>","author":"handle","created_at":"ISO-8601","text":"full post text"}]. '
            "Only posts you actually retrieved. Never construct a status URL."
        )
        cmd = [self.cfg.grok_x_bin, "-p", prompt, "--reasoning-effort", self.cfg.grok_reasoning_effort]
        if self.cfg.grok_x_model:
            cmd += ["-m", self.cfg.grok_x_model]
        td = tempfile.mkdtemp(prefix="snowball-grokx-")
        logf = Path(td) / "out.log"
        proc = process_control.launch_owned_process(cmd, cwd=td, stdout_path=logf)
        items: list[dict] | None = None
        deadline = time.time() + self.cfg.grok_x_poll_sec
        try:
            while time.time() < deadline:
                time.sleep(3)
                try:
                    items = _last_json_array(logf.read_text(errors="replace"))
                except Exception:
                    items = None
                    raise
                if items is not None or proc.poll() is not None:
                    break
        finally:
            process_control.stop_owned_process_tree(proc)
        if self.budget is not None:
            self.budget._add("x", est_tokens(prompt), est_tokens(logf.read_text(errors="replace")), calls=1)
        with contextlib.suppress(OSError):
            logf.unlink()
            os.rmdir(td)
        if items is None:
            raise EngineOutage("grok X search produced no JSON before the poll deadline")
        return validate_x_posts(items, window_days)


def _http_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        ra = e.headers.get("Retry-After")
        if e.code == 429 or e.code >= 500:
            raise EngineOutage(f"HTTP {e.code}: {detail}", int(ra) if ra and ra.isdigit() else None) from e
        raise EngineFailure(f"HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise EngineOutage(f"transport: {type(e).__name__}: {e}") from e
    except ValueError as e:
        raise EngineFailure(f"non-JSON response: {e}") from e


def _last_json_array(s: str) -> list[dict] | None:
    for m in reversed(list(re.finditer(r"\[\s*\{", s))):
        with contextlib.suppress(ValueError):
            v, _ = json.JSONDecoder().raw_decode(s[m.start() :])
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return None


def status_id_in_window(url: str, window_days: int, *, now_utc: datetime | None = None) -> bool:
    """Negative filter only. A decodable in-window id is not a retrieved post."""
    match = re.search(r"(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/(\d+)", url)
    if not match or window_days < 0:
        return False
    ts = snowflake_utc(match.group(1))
    if ts is None:
        return False
    current = now_utc or datetime.now(timezone.utc)
    lo = current - timedelta(days=window_days + 1)
    hi = current + timedelta(minutes=5)
    return lo <= ts <= hi


def validate_x_posts(items: list[dict], window_days: int) -> list[dict]:
    """Return no quotable posts.

    Ids outside the window fail status_id_in_window. Ids inside the window are
    still not posts: the grok CLI returns model text, and X Search does not
    document a field that carries the fetched post body. A citation URL is not
    stored as that body.
    """
    if not isinstance(items, list) or isinstance(window_days, bool) or not isinstance(window_days, int):
        raise TypeError("x posts")
    if window_days < 0:
        raise ValueError("window_days")
    return []


_PROGRAM_META_KEYS = frozenset({
    "program_key", "name", "institution", "official_domain", "degree_level", "modality", "field_tags",
})
_ENGINE_BINDING_KEYS = ("goal_id", "contract_version", "strategy_version", "epoch", "task_id")
_SOURCE_CYCLE_RE = re.compile(r"\b((?:19|20|21)\d{2}-(?:fall|spring|summer|winter))\b")
_APPLICABILITY_PUBLIC_KEYS = frozenset({
    "cohort", "cycle_label", "effective_scope", "campus", "program_key", "award_key",
    "supersedes", "label", "degree_level", "modality", "original_sha256", "text_span",
})
_REVIEW_PERIODS = frozenset({"per_year", "per_semester", "per_month", "one_time", "total"})
_REVIEW_ASSURANCE = frozenset({
    "code_checked", "reviewed_kept", "reviewed_dropped", "review_pending", "review_unavailable",
})
_PROGRAM_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_DROP = object()


def _producer_record(reply) -> dict | None:
    """EngineReply.model_call.as_record() is the only usage source. Reply fields are not aliases."""
    if reply is None:
        return None
    call = getattr(reply, "model_call", None)
    as_record = getattr(call, "as_record", None)
    if not callable(as_record):
        return None
    raw = as_record()
    if not isinstance(raw, Mapping):
        return None
    return dict(raw)


def _record_text(record: Mapping, key: str) -> str | None:
    value = record.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _record_count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _model_actual(record: Mapping | None, reserved: int) -> int:
    """Counted producer tokens settle as reported. Unknown or incomplete usage keeps the reservation."""
    ceiling = reserved if isinstance(reserved, int) and not isinstance(reserved, bool) and reserved >= 0 else 0
    if not record or record.get("usage_kind") not in {"actual", "estimated", "legacy"}:
        return ceiling
    tokens_in = _record_count(record.get("tokens_in"))
    tokens_out = _record_count(record.get("tokens_out"))
    if tokens_in is None or tokens_out is None:
        return ceiling
    return tokens_in + tokens_out


def _reviewer_name(record: Mapping | None) -> str | None:
    if not record:
        return None
    return _record_text(record, "reported_model") or _record_text(record, "requested_model")


def _evidenced_source_cycle(text: str) -> str | None:
    """One verbatim YYYY-season in the page. The goal's requested intake is not a source cycle."""
    found: list[str] = []
    for match in _SOURCE_CYCLE_RE.finditer(text or ""):
        label = match.group(1)
        if label not in found:
            found.append(label)
    if len(found) == 1 and found[0] in text:
        return found[0]
    return None


def _observations_from_reply(text: str, source: str) -> tuple[dict, list]:
    """Project a compiler-shaped extract envelope. Offsets are Unicode code points; mismatches are dropped."""
    try:
        data = parse_json_object(text)
    except EngineFailure:
        return {}, []
    raw_program = data.get("program") if isinstance(data.get("program"), dict) else {}
    program: dict = {}
    name = raw_program.get("name")
    if isinstance(name, str) and name and name in source and not any(ord(ch) < 32 for ch in name) and len(name) <= 200:
        program["name"] = name
    if not program:
        return {}, []
    observations = data.get("observations")
    if not isinstance(observations, list):
        return program, []
    rows = []
    for item in observations:
        if not isinstance(item, dict) or not isinstance(item.get("field_key"), str):
            continue
        start, end = item.get("quote_start"), item.get("quote_end")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            continue
        if start < 0 or end < start or end > len(source):
            continue
        quote = source[start:end]
        if not quote.strip():
            continue
        supplied = item.get("quote")
        if supplied is not None and supplied != quote:
            continue
        row: dict = {"field_key": item["field_key"], "quote_start": start, "quote_end": end}
        if "value" in item:
            row["value"] = item["value"]
        if item.get("currency") is not None:
            row["currency"] = item["currency"]
        if item.get("period") is not None:
            row["period"] = item["period"]
        subject = item.get("subject")
        if isinstance(subject, dict):
            cleaned = {}
            for key in ("program_key", "award_key", "campus", "cycle_label"):
                value = subject.get(key)
                if isinstance(value, str) and value and value in quote:
                    cleaned[key] = value
            row["subject"] = cleaned
        conditions = item.get("conditions")
        if isinstance(conditions, list) and conditions and all(isinstance(part, str) and part in quote for part in conditions):
            row["conditions"] = conditions
        rows.append(row)
    return program, rows


def _verdicts_from_reply(text: str):
    try:
        data = parse_json_object(text)
    except EngineFailure:
        return None
    verdicts = data.get("verdicts")
    if isinstance(verdicts, dict):
        return verdicts
    if not isinstance(verdicts, list):
        return None
    keyed = {}
    for item in verdicts:
        if not isinstance(item, dict) or "id" not in item:
            return verdicts
        ident = item["id"]
        if isinstance(ident, str) and ident.isdigit():
            ident = int(ident)
        if isinstance(ident, bool) or not isinstance(ident, int):
            return verdicts
        decision = item.get("verdict", item.get("decision"))
        if not isinstance(decision, str):
            return verdicts
        payload = {"decision": decision}
        if isinstance(item.get("reason"), str):
            payload["reason"] = item["reason"]
        keyed[ident] = payload
    return keyed if keyed else verdicts


def _verbatim_label(value, text: str, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value in text
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    )


def _public_applicability(raw, text: str):
    """Compiler-legal applicability, or None when the stored object is not public."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or any(key not in _APPLICABILITY_PUBLIC_KEYS for key in raw):
        return None
    cleaned = {}
    for key, value in raw.items():
        if key == "original_sha256":
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                return None
            cleaned[key] = value
            continue
        if key == "text_span":
            if not isinstance(value, dict) or set(value) != {"byte_start", "byte_end"}:
                return None
            start, end = value.get("byte_start"), value.get("byte_end")
            if type(start) is not int or type(end) is not int or start < 0 or end < start:
                return None
            cleaned[key] = {"byte_start": start, "byte_end": end}
            continue
        if isinstance(value, str):
            if not _verbatim_label(value, text, 80) or value.casefold() in {"human_note", "private", "unknown", "interview"}:
                return None
            cleaned[key] = value
            continue
        if (
            isinstance(value, list)
            and 0 < len(value) <= 16
            and all(_verbatim_label(item, text, 80) for item in value)
        ):
            cleaned[key] = list(value)
            continue
        return None
    return cleaned


def _project_review_value(value):
    if value is None or type(value) is bool or type(value) is int:
        return value
    if isinstance(value, str):
        if len(value) > 2000 or any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
            return _DROP
        return value
    if isinstance(value, dict):
        extra = set(value) - {"amount", "currency", "period"}
        if extra or "amount" not in value or "currency" not in value:
            return _DROP
        amount, currency = value["amount"], value["currency"]
        if not isinstance(amount, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?", amount) is None:
            return _DROP
        if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
            return _DROP
        return {"amount": amount, "currency": currency}
    if isinstance(value, list) and len(value) <= 32 and all(isinstance(item, str) and len(item) <= 80 for item in value):
        return list(value)
    return _DROP


def _project_subject(raw, quote: str):
    if raw in (None, ""):
        return {}
    try:
        subject = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return _DROP
    if not isinstance(subject, dict):
        return _DROP
    cleaned = {}
    for key, value in subject.items():
        if key not in {"program_key", "award_key", "campus", "cycle_label"}:
            return _DROP
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            return _DROP
        if key == "program_key":
            if _PROGRAM_KEY_RE.fullmatch(value) is None:
                return _DROP
            cleaned[key] = value
            continue
        if value not in quote or len(value) > 80:
            return _DROP
        cleaned[key] = value
    return cleaned


def _project_conditions(raw, quote: str):
    if raw in (None, ""):
        return None
    try:
        conditions = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return _DROP
    if not isinstance(conditions, list) or not conditions or len(conditions) > 20:
        return _DROP
    if any(not isinstance(part, str) or not part or part not in quote or len(part) > 300 for part in conditions):
        return _DROP
    return list(conditions)


def _review_observation(row, source) -> dict | None:
    try:
        snapshot_id = int(row["snapshot_id"])
        obs_id = int(row["id"])
    except (TypeError, ValueError, KeyError):
        return None
    if snapshot_id != int(source.id):
        return None
    quote = row["quote"]
    field_key = row["field_key"]
    if not isinstance(quote, str) or not quote or quote not in source.text:
        return None
    if not isinstance(field_key, str) or not field_key:
        return None
    item = {"id": obs_id, "field_key": field_key, "quote": quote, "source_snapshot_id": source.id}
    assurance = row["assurance"]
    if assurance in _REVIEW_ASSURANCE:
        item["assurance"] = assurance
    currency = row["currency"]
    if currency:
        if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
            return None
        item["currency"] = currency
    period = row["period"]
    if period:
        if period not in _REVIEW_PERIODS:
            return None
        item["period"] = period
    try:
        value = json.loads(row["value_json"]) if row["value_json"] is not None else None
    except (json.JSONDecodeError, TypeError):
        return None
    projected = _project_review_value(value)
    if projected is _DROP:
        return None
    item["value"] = projected
    subject = _project_subject(row["subject_json"], quote)
    if subject is _DROP:
        return None
    if subject:
        item["subject"] = subject
    conditions = _project_conditions(row["conditions_json"], quote)
    if conditions is _DROP:
        return None
    if conditions:
        item["conditions"] = conditions
    return item


def parse_json_object(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL)
    for m in re.finditer(r"\{", t):
        with contextlib.suppress(ValueError):
            v, _ = json.JSONDecoder().raw_decode(t[m.start() :])
            if isinstance(v, dict):
                return v
    raise EngineFailure("no JSON object in reply")


# ---------------------------------------------------------------------------------------------
# Fetch router (section 4): API routes by host, then tier 1 direct with pinning. Tier 2 is
# deliberately absent here: paid offload stays opt-in and lives in the collector (M4).
# ---------------------------------------------------------------------------------------------

UA = "Mozilla/5.0 SnowballBot/1.0 (+https://github.com/Ishannaik/snowball; research crawler)"
API_HOSTS = {"arctic-shift.photon-reddit.com", "hn.algolia.com", "api.github.com", "127.0.0.1"}
EXA_ROTATOR = "http://127.0.0.1:8791"
# Arctic Shift refuses a full-text search without a subreddit or author ("'query' query parameter requires
# one of: author, subreddit", checked 2026-10-07), so Reddit search walks a short list per family.
ARCTIC_SUBS = {
    "opportunities": ("startups", "indianstartups", "SideProject", "learnmachinelearning"),
    "competitors": ("SaaS", "startups", "selfhosted"),
    "alpha": ("LocalLLaMA", "MachineLearning", "singularity"),
}


def _require_public_frame(frame: Any):
    public = _sibling("interview").PublicFrame
    if not isinstance(frame, public):
        raise TypeError("a typed PublicFrame is required")
    return frame


def _engine_binding(binding: dict) -> dict:
    """Five occurrence fields. Lease tokens, roles, and profile data stay off the public engine."""
    if not isinstance(binding, dict) or any(key not in binding for key in _ENGINE_BINDING_KEYS):
        raise TypeError("binding")
    clean = {}
    for key in _ENGINE_BINDING_KEYS:
        value = binding[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("binding")
        clean[key] = value
    return clean


def extract_page_entities(frame, program, field_keys, source, binding, operation_key: str, engine, max_out: int = 4000):
    """Extract through the fixed public engine. Raw page text is not an entrypoint."""
    _require_public_frame(frame)
    snapshot = _sibling("prompts").PublicSourceSnapshot
    if type(source) is not snapshot:
        raise TypeError("a typed prompts.PublicSourceSnapshot is required")
    if source.visibility != "public" or source.provenance != "public_fetch":
        raise TypeError("snapshot text must be a public source snapshot")
    if not isinstance(program, dict) or set(program) - _PROGRAM_META_KEYS:
        raise TypeError("program")
    if not program.get("name") and not program.get("program_key"):
        raise TypeError("program")
    if isinstance(field_keys, (str, bytes)) or not isinstance(field_keys, (list, tuple)) or not field_keys:
        raise TypeError("field_keys")
    if any(not isinstance(item, str) or not item for item in field_keys):
        raise TypeError("field_keys")
    if engine is None or not callable(getattr(engine, "extract", None)):
        raise TypeError("extract requires the configured extract engine")
    if not isinstance(operation_key, str) or not operation_key or any(ch.isspace() for ch in operation_key):
        raise TypeError("operation_key is required")
    frozen = _engine_binding(binding)
    return engine.extract(frame, program, field_keys, source, frozen, operation_key, int(max_out))


class DomainBuckets:
    """Per-domain token bucket, burst 3 (section 4.3). In memory; paused_until lives in the DB."""

    def __init__(self, per_min: int) -> None:
        self.rate = per_min / 60.0
        self.state: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def wait(self, domain: str) -> None:
        while True:
            with self.lock:
                tokens, ts = self.state.get(domain, (3.0, time.time()))
                tokens = min(3.0, tokens + (time.time() - ts) * self.rate)
                if tokens >= 1:
                    self.state[domain] = (tokens - 1, time.time())
                    return
                self.state[domain] = (tokens, time.time())
                sleep = (1 - tokens) / self.rate
            time.sleep(min(sleep, 15))


def _get_json(url: str, headers: dict | None = None, timeout: int = 60) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _page_id(page: Any) -> int:
    if page is None:
        return 0
    try:
        return int(page["id"])
    except (KeyError, TypeError, ValueError):
        return 0


def _captcha_tariff(caps: dict) -> int:
    tariffs = caps.get("tariffs")
    if not isinstance(tariffs, dict):
        return 0
    value = tariffs.get("captcha")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return value


def _policy_wall(result: FetchResult) -> str | None:
    """Map a fetch result onto the access-policy wall names. Database wall kinds stay separate."""
    if result.wall_kind == "robots":
        return "robots"
    if result.wall_kind == "login":
        return "login"
    if result.status == 429:
        return "429"
    kind = paid_fetch.challenge_kind(result.text or "")
    if result.status in {401, 403, 407, 451}:
        if kind in {"challenge_turnstile", "challenge_recaptcha"}:
            return kind
        return {401: "generic_401", 403: "generic_403", 407: "generic_407", 451: "generic_451"}[result.status]
    if kind:
        return kind
    return None


class FetchRouter:
    def __init__(self, cfg: Config, store: Store | None, x: XAcquisition | None = None, dry: bool = False) -> None:
        self.cfg, self.store, self.x, self.dry = cfg, store, x, dry
        self.buckets = DomainBuckets(cfg.domain_rate_per_min)
        self.planned: list[str] = []
        self.robots_cache: dict[str, str | None] = {}

    # ---- search (wave 1a) ----
    def exa_allowed(self) -> bool:
        try:
            exa_budget = _sibling("exa_budget")
            return exa_budget.rotator_live() > exa_budget.RESERVE
        except Exception:  # noqa: BLE001 - no rotator means no Exa, not a crash
            return False

    def check_paid_access(self, url: str, private: bool = False, wall_kind: str | int | None = None) -> bool:
        """Call access_policy.decide before any paid route. Unknown domains and terminal walls
        never call a paid function. Do not add a live paid call."""
        try:
            ap_mod = _sibling("access_policy")
            policy_path = ROOT / "access.yml"
            if not policy_path.exists():
                policy_path = ROOT / "access.example.yml"
            policy = ap_mod.load_access_policy(policy_path)
            decision = policy.decide(url, private=private, wall_kind=wall_kind)
            return "paid" in decision.allowed_modes
        except Exception:  # noqa: BLE001 - policy failure denies paid; it never authorizes it
            return False

    def search(self, raw: str, source: str, window_days: int, family: str = "opportunities") -> list[SearchHit]:
        if self.dry:
            self.planned.append(f"search {source}: {raw!r}")
            return []
        if source == "exa":
            body = json.dumps({"query": raw, "numResults": 8, "contents": {"text": {"maxCharacters": 4000}}}).encode()
            req = urllib.request.Request(
                f"{EXA_ROTATOR}/search", data=body, method="POST", headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read()).get("results", [])
            return [SearchHit(x.get("url", ""), x.get("title") or "", x.get("text") or "", "exa_contents") for x in res]
        if source == "arctic":
            after = (datetime.now(timezone.utc) - timedelta(days=max(window_days, 90))).date().isoformat()
            title = " ".join(raw.split()[:2])  # title search ANDs words; two keep recall up
            hits: list[SearchHit] = []
            for sub in ARCTIC_SUBS.get(family, ARCTIC_SUBS["opportunities"]):
                q = urllib.parse.urlencode(
                    {"subreddit": sub, "title": title, "limit": 10, "sort": "desc", "after": after}
                )
                for wait in (3, 8, 15):  # it 422s or times out on busy subs now and then (v0 research.py)
                    try:
                        d = _get_json(f"https://arctic-shift.photon-reddit.com/api/posts/search?{q}")
                    except urllib.error.HTTPError as e:
                        if e.code not in (422, 429, 500, 502, 503, 504):
                            raise
                        time.sleep(wait)
                        continue
                    if d.get("data") is not None:
                        hits += [
                            SearchHit(
                                f"https://reddit.com/comments/{p['id']}",
                                p.get("title") or "",
                                f"{p.get('title') or ''}\n{p.get('selftext') or ''}",
                                "arctic",
                            )
                            for p in d["data"]
                            if p.get("id")
                        ]
                        break
                    time.sleep(wait)
            return hits
        if source == "hn":
            since = now() - max(window_days, 90) * 86400
            q = urllib.parse.urlencode(
                {"query": raw, "tags": "story", "hitsPerPage": 10, "numericFilters": f"created_at_i>{since}"}
            )
            d = _get_json(f"https://hn.algolia.com/api/v1/search?{q}")
            hits = []
            for h in d.get("hits", []):
                hits.append(
                    SearchHit(f"https://news.ycombinator.com/item?id={h['objectID']}", h.get("title") or "", "", "hn")
                )
                if h.get("url"):
                    hits.append(SearchHit(h["url"], h.get("title") or "", "", "direct"))
            return hits
        if source == "github":
            q = urllib.parse.urlencode({"q": raw, "sort": "updated", "per_page": 10})
            hdr = {"Authorization": f"Bearer {self.cfg.gh_token}"} if self.cfg.gh_token else {}
            d = _get_json(f"https://api.github.com/search/repositories?{q}", hdr)
            return [
                SearchHit(r["html_url"], r.get("full_name") or "", r.get("description") or "", "github")
                for r in d.get("items", [])
            ]
        if source == "grok":
            posts = self.x.x_search(raw, window_days) if self.x is not None else []
            # quotable is never set: model text must not become a page body.
            return [
                SearchHit(p["url"], f"@{p.get('author', '')}", p["text"], "grok")
                for p in posts
                if p.get("quotable") is True and str(p.get("text") or "").strip()
            ]
        raise ValueError(f"unknown source {source}")
    def find_similar(self, url: str) -> list[SearchHit]:
        if self.dry:
            self.planned.append(f"findSimilar: {url}")
            return []
        if not self.exa_allowed():
            return []
        try:
            body = json.dumps({"url": url, "numResults": 5}).encode()
            req = urllib.request.Request(
                f"{EXA_ROTATOR}/findSimilar", data=body, method="POST", headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read()).get("results", [])
            return [SearchHit(x.get("url", ""), x.get("title") or "", x.get("text") or "", "exa_contents") for x in res]
        except Exception as e:
            log(f"findSimilar error: {e}")
            return []


    # ---- fetch (wave 1b) ----
    def fetch(
        self,
        raw_url: str,
        prefilled: SearchHit | None = None,
        private: bool = False,
        page: Any = None,
    ) -> FetchResult:
        canon = canonical_url(raw_url) or raw_url
        host = urlsplit(canon).hostname or ""
        if self.dry:
            self.planned.append(f"fetch {self._route_name(host)}: {canon}")
            return FetchResult(canon, self._route_name(host), "failure")
        t0 = time.time()
        try:
            if prefilled and prefilled.route in ("grok", "arctic") and prefilled.text.strip():
                r = FetchResult(canon, prefilled.route, "ok", 200, prefilled.title, prefilled.text)
            elif host in ("reddit.com", "redd.it"):
                r = self._fetch_reddit(canon)
            elif host == "news.ycombinator.com":
                r = self._fetch_hn(canon)
            elif host == "github.com":
                r = self._fetch_github(canon)
            elif host in ("x.com", "twitter.com"):
                # x.com serves its SPA shell with 200 for any path; only grok search reaches X content
                r = FetchResult(canon, "grok", "walled", wall_kind="login")
            else:
                r = self._fetch_direct(raw_url, canon)
                # Search snippets are not page bodies. Exa contents is a separate lookup.
                r = self._escalate(r, canon, private, page)
        except SSRFBlocked as e:
            r = FetchResult(canon, "direct", "ssrf_blocked", wall_kind=None)
            log(f"ssrf blocked {canon}: {e}")
        except urllib.error.HTTPError as e:
            kind = "outage" if e.code == 429 or e.code >= 500 else "failure"
            ra = e.headers.get("Retry-After") if e.headers else None
            r = FetchResult(
                canon, self._route_name(host), kind, e.code, retry_after=int(ra) if ra and ra.isdigit() else None
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            r = FetchResult(canon, self._route_name(host), "outage")
            log(f"fetch transport error {canon}: {type(e).__name__}")
        r.ms = int((time.time() - t0) * 1000)
        return r

    def _escalate(self, result: FetchResult, canon: str, private: bool, page: Any) -> FetchResult:
        """Exa contents, then one configured paid route. Terminal walls and 429 stay where they are."""
        if private or result.outcome in {"ok", "outage", "ssrf_blocked"}:
            return result
        if result.status == 429 or result.wall_kind in {"robots", "login"}:
            return result
        policy_wall = _policy_wall(result)
        if policy_wall in {
            "generic_401",
            "generic_403",
            "generic_407",
            "generic_451",
            "paywall",
            "ban",
            "429",
            "unsupported_challenge",
        }:
            return result
        challenge = policy_wall in {"challenge_turnstile", "challenge_recaptcha"}
        js_shell = result.outcome == "failure" and result.status in {None, 200} and policy_wall is None
        if not challenge and not js_shell:
            return result
        source_html = result.text or ""
        exa = self._try_exa(canon)
        if exa is not None and exa.outcome == "ok":
            return exa
        attempt = self._try_paid(canon, private, page, policy_wall)
        if attempt is None or not attempt.called:
            if attempt is not None and attempt.reason == "budget":
                result.outcome = "walled"
                result.wall_kind = "budget"
            return self._finish_escalation(result, canon, private, page, source_html)
        wall = attempt.wall_kind if attempt.wall_kind in {"http", "challenge", "login", "robots", "budget"} else None
        paid_result = FetchResult(
            canon,
            attempt.route,
            attempt.outcome,
            attempt.status,
            attempt.title,
            attempt.text,
            wall_kind=wall,
            nbytes=attempt.nbytes,
            final_url=attempt.final_url,
        )
        return self._finish_escalation(paid_result, canon, private, page, source_html)

    def _finish_escalation(
        self,
        result: FetchResult,
        canon: str,
        private: bool,
        page: Any,
        source_html: str = "",
    ) -> FetchResult:
        """Solve only when the fixture is on and a browser exists. Otherwise keep the fetch result."""
        if self.store is None or not self.cfg.challenge_fixture or private or result.outcome == "ok":
            return result
        kind = self._challenge_name(result, source_html)
        if kind is None:
            return result
        try:
            browser = _sibling("browser_worker")
        except Exception as exc:  # noqa: BLE001 - a missing worker stays on the free result
            log(f"challenge worker unavailable: {type(exc).__name__}")
            return result
        if browser.playwright_session_factory() is None:
            return result
        return self._run_solver(result, canon, page, kind, source_html, browser)

    def _challenge_name(self, result: FetchResult, source_html: str) -> str | None:
        for text in (source_html, result.text or ""):
            kind = paid_fetch.challenge_kind(text or "")
            if kind in {"challenge_turnstile", "challenge_recaptcha"}:
                return kind
        if result.wall_kind in {"challenge_turnstile", "challenge_recaptcha"}:
            return result.wall_kind
        return None

    def _run_solver(self, result, canon: str, page: Any, kind: str, source_html: str, browser) -> FetchResult:
        """Wire one solver attempt. Returns the original result unless the browser actually got the page."""
        del source_html
        decision = self._load_solver_decision(canon, kind)
        if decision is None or "solver" not in decision["modes"]:
            return result
        block = browser.activation_block_reason(
            fixture_mode=True,
            api_key=self.cfg.captcha_api_key,
            allowed_modes=decision["modes"],
            caps=decision["caps"],
        )
        if block:
            log(f"challenge worker skipped: {block}")
            return result
        tariff = _captcha_tariff(decision["caps"])
        if tariff <= 0:
            return result
        return self._run_solver_acquire(result, canon, page, kind, browser, decision, tariff)

    def _run_solver_acquire(self, result, canon, page, kind, browser, decision, tariff: int) -> FetchResult:
        captcha_mod = _sibling("captcha")
        types = _sibling("fetch_types")
        page_id = _page_id(page)
        acquisition = f"solve-{page_id}-{hashlib.sha1(canon.encode()).hexdigest()[:12]}"
        deadline = time.time() + float(self.cfg.captcha_deadline_sec)
        request = types.AccessRequest(
            acquisition_id=acquisition,
            page_id=page_id,
            url=canon,
            private=False,
            allowed_modes=decision["modes"],
            approved_resource_origins=decision["origins"],
            allowed_path_prefixes=decision["prefixes"],
            session_id=acquisition,
            deadline_at=deadline,
        )
        cfg = captcha_mod.SolverConfig(
            client_key=self.cfg.captcha_api_key,
            proxy_url=self.cfg.residential_proxy_url,
        )
        budget = sys.modules["snowball_paid_budget"]

        def reservation_factory(_challenge):
            return budget.reserve(
                self.store,
                acquisition,
                "captcha",
                tariff,
                is_solver=True,
                caps=decision["caps"],
            )

        solve = browser.solve_with_captcha(captcha_mod, self.store, cfg, reservation_factory)
        egress = _sibling("egress_guard")
        gateway = (
            getattr(self, "egress_gateway", None)
            or getattr(self, "gateway", None)
            or getattr(self.cfg, "egress_gateway", None)
        )
        if gateway is None and hasattr(egress, "EgressGateway"):
            try:
                gateway = egress.EgressGateway(approved_origins=decision["origins"])
            except Exception:
                gateway = None
        fetched = browser.acquire(
            request,
            cfg,
            deadline,
            session_factory=browser.playwright_session_factory(
                approved_origins=decision["origins"],
                gateway=gateway,
            ),
            solve=solve,
        )
        return self._adopt_browser_result(result, fetched)

    def _load_solver_decision(self, canon: str, kind: str):
        path = ROOT / "access.yml"
        if not path.exists():
            path = ROOT / "access.example.yml"
        try:
            loaded = access_policy.load_access_policy(path)
            decision = loaded.decide(canon, private=False, wall_kind=kind)
        except Exception as exc:  # noqa: BLE001 - policy failure denies the solver
            log(f"challenge policy skipped: {type(exc).__name__}")
            return None
        return {
            "modes": decision.allowed_modes,
            "prefixes": decision.allowed_path_prefixes,
            "origins": decision.approved_resource_origins,
            "caps": loaded.caps(),
        }

    def _adopt_browser_result(self, original: FetchResult, fetched) -> FetchResult:
        text = getattr(fetched, "text", "") or ""
        if getattr(fetched, "outcome", None) != "ok" or len(text) < 200:
            return original
        return FetchResult(
            original.canon,
            "browser",
            "ok",
            getattr(fetched, "status", None) or 200,
            getattr(fetched, "title", "") or "",
            text,
            nbytes=len(text.encode("utf-8")),
            final_url=getattr(fetched, "final_url", None) or original.canon,
        )

    def _try_exa(self, canon: str) -> FetchResult | None:
        if not self.exa_allowed():
            return None
        try:
            body = json.dumps({"urls": [canon], "text": {"maxCharacters": 4000}}).encode()
            request = urllib.request.Request(
                f"{EXA_ROTATOR}/contents",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read(self.cfg.max_body_bytes))
        except Exception as exc:  # noqa: BLE001 - a contents miss stays on the free result
            log(f"exa contents skipped: {type(exc).__name__}")
            return None
        parsed = paid_fetch.read_exa_contents(payload)
        if parsed is None:
            return None
        title, text = parsed
        return FetchResult(canon, "exa_contents", "ok", 200, title, text, final_url=canon)

    def _try_paid(self, canon: str, private: bool, page: Any, policy_wall: str | None):
        if self.store is None:
            return None
        provider = (self.cfg.paid_provider or "").strip().lower()
        if provider not in {"zenrows", "residential"}:
            return None
        policy_path = ROOT / "access.yml"
        if not policy_path.exists():
            policy_path = ROOT / "access.example.yml"
        try:
            loaded = access_policy.load_access_policy(policy_path)
            context = self._paid_page(page)
            return paid_fetch.maybe_escalate(
                url=canon,
                private=private,
                wall_kind=policy_wall,
                policy=loaded,
                caps=loaded.caps(),
                store=self.store,
                cfg=paid_fetch.PaidConfig(
                    provider=provider,
                    zenrows_mode=self.cfg.zenrows_mode,
                    zenrows_api_key=self.cfg.zenrows_api_key,
                    residential_proxy_url=self.cfg.residential_proxy_url,
                    min_yield=self.cfg.paid_min_yield,
                    max_body_bytes=self.cfg.max_body_bytes,
                    max_proxy_bytes=self.cfg.max_proxy_bytes,
                    max_retrievals=self.cfg.max_paid_retrievals,
                ),
                page=context,
                environ=os.environ,
            )
        except Exception as exc:  # noqa: BLE001 - paid failure must not crash the free collector
            log(f"paid fetch skipped: {type(exc).__name__}")
            return None

    def _paid_page(self, page: Any):
        if self.store is None or page is None:
            return None
        try:
            page_id = int(page["id"])
            domain = str(page["domain"])
            kind = page["kind"] or page["predicted_kind"]
        except (KeyError, TypeError, ValueError):
            return None
        stats = self.store.one(
            "SELECT new_items_total, fetch_count, paid_failures FROM domains WHERE domain=?",
            (domain,),
        )
        if stats is None:
            return None
        attempts = self.store.one("SELECT COUNT(*) AS n FROM paid_fetches WHERE page_id=?", (page_id,))
        score = (int(stats["new_items_total"]) + 1) / (int(stats["fetch_count"]) + 2)
        return paid_fetch.PageContext(
            page_id=page_id,
            domain=domain,
            predicted_kind=str(kind) if kind else None,
            yield_score=score,
            paid_failures=int(stats["paid_failures"]),
            paid_attempts=int(attempts["n"]) if attempts else 0,
        )

    @staticmethod
    def _route_name(host: str) -> str:
        return {
            "reddit.com": "arctic",
            "redd.it": "arctic",
            "news.ycombinator.com": "hn",
            "github.com": "github",
            "x.com": "grok",
            "twitter.com": "grok",
        }.get(host, "direct")

    def _fetch_reddit(self, canon: str) -> FetchResult:
        pid = canon.rsplit("/", 1)[-1]
        base = "https://arctic-shift.photon-reddit.com/api"
        post = ((_get_json(f"{base}/posts/ids?ids={pid}").get("data")) or [{}])[0]
        tree = _get_json(f"{base}/comments/tree?link_id=t3_{pid}&limit=40", timeout=90)
        lines = [f"{post.get('title') or ''}\n{post.get('selftext') or ''}"]

        def walk(nodes: Any, depth: int) -> None:
            for n in nodes or []:
                d = n.get("data", n) if isinstance(n, dict) else {}
                if d.get("body"):
                    lines.append(f"- comment u/{d.get('author')}: {d['body'][:1200]}")
                walk(
                    (d.get("replies") or {}).get("data", {}).get("children")
                    if isinstance(d.get("replies"), dict)
                    else d.get("children"),
                    depth + 1,
                )

        walk(tree.get("data"), 0)
        text = norm_text("\n".join(lines))
        return FetchResult(canon, "arctic", "ok" if len(text) > 50 else "failure", 200, post.get("title") or "", text)

    def _fetch_hn(self, canon: str) -> FetchResult:
        item = dict(parse_qsl(urlsplit(canon).query)).get("id", "")
        d = _get_json(f"https://hn.algolia.com/api/v1/items/{item}")
        parts = [f"{d.get('title') or ''} {strip_tags(d.get('text') or '')}"]
        parts += [
            f"- comment {c.get('author')}: {strip_tags(c['text'])[:1200]}"
            for c in d.get("children", [])
            if c.get("text")
        ][:30]
        links = [(d["url"], d.get("title") or "")] if d.get("url") else []
        text = norm_text("\n".join(parts))
        return FetchResult(canon, "hn", "ok" if len(text) > 50 else "failure", 200, d.get("title") or "", text, links)

    def _fetch_github(self, canon: str) -> FetchResult:
        owner, repo = urlsplit(canon).path.strip("/").split("/")[:2]
        hdr = {"Accept": "application/vnd.github.raw"}
        if self.cfg.gh_token:
            hdr["Authorization"] = f"Bearer {self.cfg.gh_token}"
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/readme", headers={"User-Agent": UA, **hdr}
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            md = r.read(self.cfg.max_body_bytes).decode("utf-8", "replace")
        links = [(u, a) for a, u in re.findall(r"\[([^\]]{1,200})\]\((https?://[^)\s]+)\)", md)]
        return FetchResult(canon, "github", "ok", 200, f"{owner}/{repo}", norm_text(md), links)

    def _robots_allows(self, scheme_host: str, path: str) -> bool:
        if scheme_host not in self.robots_cache:
            txt = None
            with contextlib.suppress(Exception):
                r = self._direct_get(f"{scheme_host}/robots.txt", hops=0)
                if r.status == 200:
                    txt = r.text
            self.robots_cache[scheme_host] = txt
        txt = self.robots_cache[scheme_host]
        if not txt:
            return True
        agent_ok, disallow = False, []
        for line in txt.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            if k.lower() == "user-agent":
                agent_ok = v == "*" or "snowballbot" in v.lower()
            elif k.lower() == "disallow" and agent_ok and v:
                disallow.append(v)
        return not any(path.startswith(d) for d in disallow)

    def _fetch_direct(self, raw_url: str, canon: str) -> FetchResult:
        u = urlsplit(raw_url)
        if not self._robots_allows(f"{u.scheme}://{u.netloc}", u.path or "/"):
            return FetchResult(canon, "direct", "walled", wall_kind="robots")
        self.buckets.wait(u.hostname or "")
        r = self._direct_get(raw_url, hops=5)
        if r.outcome == "ok":
            outcome, wall = classify_response(r.status or 0, r.text)
            if outcome != "ok":
                r.outcome, r.wall_kind = outcome, wall
                return r
            r.title, r.text, r.links = parse_html(r.text)
        return r

    def _direct_get(self, url: str, hops: int) -> FetchResult:
        """curl_cffi with Chrome impersonation, DNS pinned via CURLOPT_RESOLVE, manual redirects."""
        from curl_cffi import CurlOpt
        from curl_cffi import requests as cffi

        for _ in range(hops + 1):
            u = urlsplit(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                return FetchResult(canonical_url(url) or url, "direct", "failure")
            port = u.port or (443 if u.scheme == "https" else 80)
            ip = resolve_and_pin(u.hostname, port)
            s = cffi.Session(impersonate="chrome")
            s.curl.setopt(CurlOpt.RESOLVE, [f"{u.hostname}:{port}:{ip}"])
            headers = {"Accept-Language": "en-US,en;q=0.9"}
            try:
                resp = s.get(url, allow_redirects=False, timeout=30, headers=headers, stream=True)
                buf = bytearray()
                truncated = False
                for chunk in resp.iter_content():
                    buf += chunk
                    if len(buf) >= self.cfg.max_body_bytes:
                        truncated = True
                        break
                resp.close()
            finally:
                s.close()
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])  # the next hop is re-resolved and re-pinned
                continue
            ra = resp.headers.get("retry-after")
            text = bytes(buf).decode(resp.encoding or "utf-8", "replace")
            res = FetchResult(
                canonical_url(url) or url,
                "direct",
                "ok",
                resp.status_code,
                text=text,
                pinned_ip=ip,
                nbytes=len(buf),
                final_url=url,
                truncated=truncated,
                retry_after=int(ra) if ra and ra.isdigit() else None,
            )
            if resp.status_code >= 400:
                res.outcome, res.wall_kind = classify_response(resp.status_code, text)
            return res
        return FetchResult(canonical_url(url) or url, "direct", "failure")  # more than 5 hops


# ---------------------------------------------------------------------------------------------
# Lock engine writes (section 3 action tables). Every method runs inside the caller's tx.
# ---------------------------------------------------------------------------------------------


class LockEngine:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.metrics: Counter = Counter()

    # Layer 1
    def query_lock(
        self,
        db: sqlite3.Connection,
        raw: str,
        source: str,
        recheck_days: int,
        topic_id: int,
        via: str = "seed",
        priority: float = 0.5,
    ) -> int | None:
        ts = now()
        key = query_key(raw, source, recheck_days, ts)
        row = db.execute("SELECT id, status FROM queries WHERE query_key=?", (key,)).fetchone()
        if row:
            db.execute("UPDATE queries SET dup_requests=dup_requests+1 WHERE id=?", (row["id"],))
            self.metrics["query_dup"] += 1
            return row["id"] if row["status"] == "pending" else None
        cur = db.execute(
            "INSERT INTO queries(query_key, norm, raw, source, window_key, topic_id, discovered_via, "
            "priority, scheduled_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, norm_query(raw), raw[:500], source, window_key(recheck_days, ts), topic_id, via, priority, ts),
        )
        self.metrics["query_new"] += 1
        return cur.lastrowid

    # Layer 2a: insert or touch a frontier URL
    def enqueue_url(
        self,
        db: sqlite3.Connection,
        raw: str,
        topic_id: int | None,
        via: str,
        recheck_days: int,
        parent: int | None = None,
        depth: int = 0,
        predicted: str | None = None,
        hops_dry: int = 0,
    ) -> int | None:
        canon = canonical_url(raw)
        if not canon:
            return None
        row = db.execute("SELECT id, raw_urls, status FROM pages WHERE canon_url=?", (canon,)).fetchone()
        if row:
            if row["status"] == "dropped":
                return None  # drift exclusion: dropped off-topic page cannot re-enter
            urls = json.loads(row["raw_urls"])
            if raw not in urls and len(urls) < 20:
                urls.append(raw)
            db.execute(
                "UPDATE pages SET dup_requests=dup_requests+1, raw_urls=? WHERE id=?", (json.dumps(urls), row["id"])
            )
            self.metrics["page_dup"] += 1
            return None
        domain = urlsplit(canon).hostname or ""
        db.execute("INSERT INTO domains(domain) VALUES (?) ON CONFLICT(domain) DO NOTHING", (domain,))
        ts = now()
        cur = db.execute(
            "INSERT INTO pages(canon_url, raw_urls, domain, predicted_kind, next_check_at, interval_sec, "
            "depth, hops_dry, parent_page_id, topic_id, discovered_via, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                canon,
                json.dumps([raw]),
                domain,
                predicted,
                ts,
                recheck_days * 86400,
                depth,
                hops_dry,
                parent,
                topic_id,
                via,
                ts,
            ),
        )
        self.metrics["page_new"] += 1
        return cur.lastrowid

    # Layers 2b + 2c: returns ('new'|'alias'|'reprint'|'same', other_page_id)
    def content_lock(self, db: sqlite3.Connection, page_id: int, text: str) -> tuple[str, int | None]:
        h = content_hash(text)
        prev = db.execute("SELECT content_hash FROM pages WHERE id=?", (page_id,)).fetchone()
        if prev and prev["content_hash"] == h:
            return "same", None
        other = db.execute(
            "SELECT id FROM pages WHERE content_hash=? AND id<>? AND alias_of IS NULL LIMIT 1", (h, page_id)
        ).fetchone()
        sig = minhash(text)
        db.execute("UPDATE pages SET content_hash=?, minhash=? WHERE id=?", (h, mh_pack(sig), page_id))
        if other:
            self.metrics["alias"] += 1
            return "alias", other["id"]
        bands = band_keys(sig)
        cands: set[int] = set()
        for b, bucket in bands:
            cands |= {
                r["page_id"]
                for r in db.execute(
                    "SELECT page_id FROM minhash_bands WHERE band=? AND bucket=? AND page_id<>?", (b, bucket, page_id)
                )
            }
        db.execute("DELETE FROM minhash_bands WHERE page_id=?", (page_id,))
        db.executemany(
            "INSERT OR IGNORE INTO minhash_bands(band, bucket, page_id) VALUES (?,?,?)",
            [(b, k, page_id) for b, k in bands],
        )
        for cid in sorted(cands):
            r = db.execute("SELECT minhash FROM pages WHERE id=?", (cid,)).fetchone()
            if r and r["minhash"] and est_jaccard(sig, mh_unpack(r["minhash"])) >= 0.80:
                self.metrics["reprint"] += 1
                return "reprint", cid
        return "new", None

    # Layer 3
    def entity_lock(self, db: sqlite3.Connection, row: dict, family: str, page_id: int, reprint: bool) -> str:
        """Returns new | same | changed. Writes entities, entity_sources, entity_changes."""
        ts = now()
        key = entity_key(row["name"], row.get("org"), row.get("deadline"))
        n, _ = norm_name(row["name"])
        o = norm_name(row["org"])[0] if row.get("org") else None
        year = key.rsplit("|", 1)[1]
        year = None if year == "-" else year
        existing = db.execute("SELECT * FROM entities WHERE key=?", (key,)).fetchone()
        page_row = db.execute("SELECT canon_url, raw_path FROM pages WHERE id=?", (page_id,)).fetchone()
        page_canon = page_row["canon_url"] if page_row else None
        raw_link = row.get("link")
        if not raw_link:
            link = page_canon
        else:
            cand = canonical_url(raw_link)
            # keep it only when that URL appears in the fetched page text or links
            page_text = ""
            if page_row and page_row["raw_path"]:
                rp = ROOT / page_row["raw_path"]
                if rp.exists():
                    with contextlib.suppress(OSError):
                        page_text = rp.read_text(errors="replace")
            link_in_links = False
            if cand:
                found = db.execute(
                    "SELECT 1 FROM page_links WHERE from_page_id=? AND to_canon_url=? LIMIT 1",
                    (page_id, cand),
                ).fetchone()
                link_in_links = found is not None
            if (cand and cand in page_text) or (raw_link in page_text) or link_in_links or (cand == page_canon):
                link = cand
            else:
                link = None
        if not existing and link:
            is_hub = db.execute(
                "SELECT 1 FROM pages p JOIN hubs h ON h.page_id=p.id WHERE p.canon_url=?", (link,)
            ).fetchone()
            if not is_hub:
                existing = db.execute("SELECT * FROM entities WHERE link=? LIMIT 1", (link,)).fetchone()
        if not existing:
            tri = char_trigrams(n)
            for e in db.execute(
                "SELECT * FROM entities WHERE (norm_org IS ? OR norm_org IS NULL OR ? IS NULL) AND year IS ?",
                (o, o, year),
            ):
                if jaccard(tri, char_trigrams(e["norm_name"])) >= 0.85:
                    existing = e
                    break
        if existing:
            k = existing["key"]
            aliases = sorted(set(json.loads(existing["aliases"])) | ({row["name"]} - {existing["name"]}))
            new_vals = {
                "deadline": row.get("deadline"),
                "amount": row.get("amount"),
                "currency": row.get("currency"),
                "status": None,
                "eligibility": row.get("eligibility"),
                "link": link,
                "location": row.get("location"),
            }
            changes = entity_diff(dict(existing), new_vals)
            db.execute(
                "INSERT OR IGNORE INTO entity_sources(entity_key, page_id, seen_at) VALUES (?,?,?)", (k, page_id, ts)
            )
            if not changes:
                db.execute("UPDATE entities SET last_seen_at=?, aliases=? WHERE key=?", (ts, json.dumps(aliases), k))
                self.metrics["entity_same"] += 1
                return "same"
            for f, old, new in changes:
                db.execute(
                    "INSERT INTO entity_changes(entity_key, field, old, new, seen_at, page_id, evidence_quote) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (k, f, old, new, ts, page_id, row["evidence_quote"]),
                )
                db.execute(f"UPDATE entities SET {f}=? WHERE key=?", (new_vals[f], k))  # f is from MATERIAL
            db.execute(
                "UPDATE entities SET status='changed', last_seen_at=?, last_changed_at=?, aliases=? WHERE key=?",
                (ts, ts, json.dumps(aliases), k),
            )
            self.metrics["entity_changed"] += 1
            return "changed"
        kind = row.get("kind") if row.get("kind") in ENTITY_KINDS else "other"
        db.execute(
            "INSERT INTO entities(key, name, norm_name, org, norm_org, year, kind, family, amount, currency, "
            "deadline, eligibility, location, link, evidence_quote, evidence_page_id, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                row["name"],
                n,
                row.get("org"),
                o,
                year,
                kind,
                family,
                row.get("amount"),
                row.get("currency"),
                row.get("deadline"),
                row.get("eligibility"),
                row.get("location"),
                link,
                row["evidence_quote"],
                page_id,
                ts,
                ts,
            ),
        )
        db.execute(
            "INSERT OR IGNORE INTO entity_sources(entity_key, page_id, seen_at) VALUES (?,?,?)", (key, page_id, ts)
        )
        self.metrics["entity_new"] += 1
        return "new"

    # Layer 4
    def claim_lock(
        self,
        db: sqlite3.Connection,
        text: str,
        page_id: int,
        topic_id: int,
        domain: str,
        span: tuple[int, int],
        reprint: bool,
        entity: str | None = None,
    ) -> str:
        ts = now()
        nt = norm_text(text).casefold()
        h = hashlib.sha256(nt.encode()).hexdigest()
        match = db.execute("SELECT hash, text FROM claims WHERE hash=?", (h,)).fetchone()
        verdict = "dup"
        if not match:
            candidates = []
            q = self._rare_terms(db, nt)
            if q:
                try:
                    candidates.extend(
                        db.execute(
                            "SELECT c.hash, c.text FROM claims_fts f JOIN claims c ON c.rowid=f.rowid "
                            "WHERE claims_fts MATCH ? ORDER BY bm25(claims_fts) LIMIT 50",
                            (q,),
                        ).fetchall()
                    )
                except sqlite3.OperationalError:
                    candidates = []
            if not candidates:
                candidates = db.execute(
                    "SELECT hash, text FROM claims WHERE topic_id=? ORDER BY rowid DESC LIMIT 50",
                    (topic_id,),
                ).fetchall()
            seen_hashes: set[str] = set()
            for c in candidates:
                if c["hash"] in seen_hashes:
                    continue
                seen_hashes.add(c["hash"])
                if not numbers_compatible(text, c["text"]):
                    continue  # conflicting amounts or years are material facts; never merge
                sim = hybrid_similarity(text, c["text"])
                # Shared amounts are the anchor; wording can drift below the general 0.70 bar.
                threshold = 0.45 if material_numbers(text) else 0.70
                if sim >= threshold:
                    match = c
                    if text != c["text"]:
                        db.execute(
                            "INSERT OR IGNORE INTO claim_variants(claim_hash, text) VALUES (?,?)", (c["hash"], text)
                        )
                    verdict = "near_dup"
                    break
        if match:
            mh = match["hash"]
            seen_domain = db.execute(
                "SELECT 1 FROM claim_sources cs JOIN pages p ON p.id=cs.page_id WHERE cs.claim_hash=? AND p.domain=?",
                (mh, domain),
            ).fetchone()
            if not reprint and not seen_domain:
                db.execute("UPDATE claims SET n_sources=n_sources+1 WHERE hash=?", (mh,))
            db.execute("UPDATE claims SET last_seen_at=? WHERE hash=?", (ts, mh))
            db.execute(
                "INSERT OR IGNORE INTO claim_sources(claim_hash, page_id, quote_start, quote_end) VALUES (?,?,?,?)",
                (mh, page_id, span[0], span[1]),
            )
            self.metrics["claim_dup"] += 1
            return verdict
        init_sources = 0 if reprint else 1
        db.execute(
            "INSERT INTO claims(hash, text, norm_text, topic_id, entity_key, first_seen_at, last_seen_at, n_sources) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (h, text, nt, topic_id, entity, ts, ts, init_sources),
        )
        db.execute(
            "INSERT OR IGNORE INTO claim_sources(claim_hash, page_id, quote_start, quote_end) VALUES (?,?,?,?)",
            (h, page_id, span[0], span[1]),
        )
        self.metrics["claim_new"] += 1
        return "new"

    @staticmethod
    def _rare_terms(db: sqlite3.Connection, nt: str) -> str:
        """The six rarest tokens of the claim, OR-ed into one FTS5 MATCH (section 3.4)."""
        toks = [t for t in tokens(nt) if len(t) > 2]
        if not toks:
            return ""
        scored = []
        for t in toks:
            try:
                n = db.execute("SELECT count(*) FROM claims_fts WHERE claims_fts MATCH ?", (f'"{t}"',)).fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            scored.append((n, t))
        return " OR ".join(f'"{t}"' for _, t in sorted(scored)[:6])

    # Layer 5
    def topic_lock(
        self,
        db: sqlite3.Connection,
        title: str,
        focus: str,
        family: str,
        via: str,
        parent_id: int | None = None,
        private: bool = False,
        key: str | None = None,
    ) -> tuple[str, int]:
        """Returns (same|deepen|new, topic_id)."""
        key = key or slug(norm_name(title)[0] or title)
        row = db.execute("SELECT id, private FROM topics WHERE key=?", (key,)).fetchone()
        if row:
            if bool(row["private"]) == bool(private):
                return "same", row["id"]
            # Same slug on the other side of the privacy boundary is a different topic.
            key = f"{key}-private" if private else f"{key}-public"
            row = db.execute("SELECT id, private FROM topics WHERE key=?", (key,)).fetchone()
            if row and bool(row["private"]) == bool(private):
                return "same", row["id"]
        terms = topic_terms(title, focus)
        n_docs = db.execute("SELECT count(*) FROM topics").fetchone()[0]
        df = (
            {
                r["term"]: r["df"]
                for r in db.execute(
                    f"SELECT term, df FROM topic_terms_df WHERE term IN ({','.join('?' * len(terms))})", tuple(terms)
                )
            }
            if terms
            else {}
        )
        best: tuple[float, int | None] = (0.0, None)
        q = fts_terms(" ".join(sorted(terms, key=lambda t: df.get(t, 0))), 10)
        if q and n_docs:
            for t in db.execute(
                "SELECT t.id, t.title, t.focus, t.private FROM topics_fts f JOIN topics t ON t.id=f.rowid "
                "WHERE topics_fts MATCH ? LIMIT 30",
                (q,),
            ):
                if bool(t["private"]) != private:
                    continue  # never merge a private thread into a public one or the reverse
                cos = tfidf_cosine(terms, topic_terms(t["title"], t["focus"]), df, n_docs)
                if cos > best[0]:
                    best = (cos, t["id"])
        if best[1] is not None and best[0] >= 0.30:
            fv = json.loads(db.execute("SELECT focus_variants FROM topics WHERE id=?", (best[1],)).fetchone()[0])
            if focus not in fv:
                fv.append(focus)
            db.execute("UPDATE topics SET focus_variants=? WHERE id=?", (json.dumps(fv), best[1]))
            if best[0] >= 0.75:
                self.metrics["topic_merge"] += 1
                return "same", best[1]
            db.execute(
                "INSERT INTO tasks(kind, payload, priority, next_attempt_at, created_at) VALUES ('deepen',?,?,?,?)",
                (json.dumps({"topic_id": best[1], "focus": focus}), 0.6, now(), now()),
            )
            self.metrics["topic_deepen"] += 1
            return "deepen", best[1]
        depth = 0
        if parent_id:
            p = db.execute("SELECT depth FROM topics WHERE id=?", (parent_id,)).fetchone()
            depth = (p["depth"] + 1) if p else 0
        cur = db.execute(
            "INSERT INTO topics(key, family, title, focus, parent_id, depth, discovered_via, created_at, private) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (key, family, title, focus, parent_id, depth, via, now(), int(private)),
        )
        for t in set(terms):
            db.execute(
                "INSERT INTO topic_terms_df(term, df) VALUES (?,1) ON CONFLICT(term) DO UPDATE SET df=df+1", (t,)
            )
        self.metrics["topic_new"] += 1
        return "new", cur.lastrowid


ENTITY_KINDS = {
    "grant",
    "hackathon",
    "fellowship",
    "scholarship",
    "credits",
    "accelerator",
    "competitor",
    "tool",
    "model",
    "other",
}
FAMILY_DEFAULTS = {
    "opportunities": (14, 1.0, "grants hackathons fellowships scholarships credits funding programs"),
    "competitors": (30, 0.8, "competing products pricing launches features"),
    "alpha": (1, 0.9, "new ai tools models infrastructure releases"),
}


# ---------------------------------------------------------------------------------------------
# The orchestrator.
# ---------------------------------------------------------------------------------------------


@dataclass
class TopicRun:
    topic: sqlite3.Row
    family: sqlite3.Row
    private: bool
    queries: list[sqlite3.Row] = field(default_factory=list)
    kb_answered: int = 0
    pages: list[dict] = field(default_factory=list)  # {page_id, result, hit}
    extractions: dict[int, dict] = field(default_factory=dict)
    verified: dict[int, dict] = field(default_factory=dict)
    deferred: set[int] = field(default_factory=set)  # fetched but not extracted: stays 'fetched' for the next run
    counts: Counter = field(default_factory=Counter)


class SnowballOrchestrator:
    MODES = ("auto", "gemini-bulk")

    def __init__(
        self, cfg: Config, store: Store, mode: str = "auto", dry: bool = False,
        topics_path: Path | None = None,
    ) -> None:
        self.cfg, self.dry = cfg, dry
        self.store = store.snapshot() if dry else store
        self.mode = mode
        self.topics_path = topics_path
        self.budget = TokenBudgetMiddleware(self.store, cfg)
        self.x = XAcquisition(cfg, self.budget, dry)
        self.router = FetchRouter(cfg, None if dry else self.store, self.x, dry)
        self.locks = LockEngine(self.store)
        self.worker_id = f"orch-{os.getpid()}"
        self.external_calls = 0

    # ---- seeding ----
    def seed_from_yaml(self, topics_path: Path | None = None) -> int:
        """topics.yml (or topics.example.yml) into families + topics. Idempotent via the topic lock."""
        try:
            import yaml
        except ImportError:
            return 0
        path = topics_path or self.topics_path or ROOT / "topics.yml"
        if not path.exists():
            path = ROOT / "topics.example.yml"
        if not path.exists():
            return 0
        d = yaml.safe_load(path.read_text()) or {}
        fams = d.get("families") or {}
        seeds = list(d.get("seeds") or [])
        for t in d.get("topics") or []:  # v0 queue.py format
            if t.get("status") in ("queued", "done", "suggested") and t.get("slug"):
                seeds.append(
                    {
                        "id": t["slug"],
                        "family": t.get("family", "opportunities"),
                        "focus": t.get("focus") or t.get("question") or t.get("title") or t["slug"],
                        "title": t.get("title"),
                        "private": t.get("private", False),
                    }
                )
        added = 0
        with self.store.tx() as db:
            for name in set(fams) | set(FAMILY_DEFAULTS) | {s.get("family", "opportunities") for s in seeds}:
                dd = FAMILY_DEFAULTS.get(name, (14, 1.0, name))
                f = fams.get(name) or {}
                db.execute(
                    "INSERT INTO families(name, recheck_days, weight, relevance_floor, focus) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET recheck_days=excluded.recheck_days, weight=excluded.weight, "
                    "relevance_floor=excluded.relevance_floor, focus=excluded.focus",
                    (
                        name,
                        int(f.get("recheck_days", dd[0])),
                        float(f.get("weight", dd[1])),
                        float(f.get("relevance_floor", 0.08)),
                        f.get("focus", dd[2]),
                    ),
                )
            for s in seeds:
                sid = str(s["id"])
                key = slug(sid)
                existing = db.execute("SELECT id FROM topics WHERE key=?", (key,)).fetchone()
                verdict, topic_id = self.locks.topic_lock(
                    db,
                    s.get("title") or sid.replace("-", " "),
                    s["focus"],
                    s.get("family", "opportunities"),
                    "seed",
                    private=bool(s.get("private")),
                    key=key,
                )
                if verdict == "new" or not existing:
                    added += 1
                    # Deterministic query grid: family x region x audience x source x window
                    regions = s.get("regions") or ["india", "global"]
                    audiences = s.get("audiences") or ["developers", "startups"]
                    sources = ["exa", "hn", "arctic", "github"]
                    if not s.get("private") and s.get("family", "opportunities") in ("alpha", "opportunities"):
                        sources.append("grok")
                    fam_info = fams.get(s.get("family", "opportunities")) or {}
                    recheck_days = int(fam_info.get("recheck_days", 14))
                    for reg in sorted(regions):
                        for aud in sorted(audiences):
                            for src in sorted(sources):
                                raw_q = f"{s['focus']} {reg} {aud}"
                                if self.dry:
                                    self.router.planned.append(f"seed query [{src}]: {raw_q}")
                                else:
                                    self.locks.query_lock(db, raw_q, src, recheck_days, topic_id, via="seed")
        return added

    # ---- priority selection: Score = Novelty x Value x Due ----
    def select_topics(self, max_topics: int, slug_filter: str | None) -> list[sqlite3.Row]:
        args: tuple = ()
        where = "t.status='active'"
        if slug_filter:
            where, args = "t.key=?", (slug(slug_filter),)
        sql = f"""
        SELECT t.*, f.recheck_days, f.weight,
               (CAST(t.new_items_total AS REAL) + 1.0) / (t.fetch_count + 2.0) / (1.0 + t.hops_dry)       AS novelty,
               f.weight * (1.0 + 0.1 * (SELECT count(*) FROM tasks k WHERE k.kind='deepen' AND k.status='pending'
                                         AND json_extract(k.payload,'$.topic_id')=t.id))                    AS value,
               CASE WHEN t.last_run_at IS NULL THEN 2.0
                    WHEN t.last_run_at + f.recheck_days*86400 > CAST(strftime('%s','now') AS INTEGER) THEN 0.0
                    ELSE 1.0 + MIN(1.0, (strftime('%s','now') - t.last_run_at - f.recheck_days*86400) / 86400.0) END AS due
          FROM topics t JOIN families f ON f.name=t.family
         WHERE {where}"""
        rows = self.store.all(sql, args)
        scored = sorted(
            rows, key=lambda r: -(r["novelty"] * r["value"] * (r["due"] if not slug_filter else max(r["due"], 1.0)))
        )
        return [r for r in scored if slug_filter or r["novelty"] * r["value"] * r["due"] > 0][:max_topics]

    # ---- engine selection ----
    def extract_engine(self):
        return transport_module().GeminiBulkExtractor(self.cfg, dry=self.dry)

    def review_engine(self):
        return transport_module().ReviewEngine(self.cfg, dry=self.dry)

    def plan_engine(self):
        return transport_module().ReviewEngine(self.cfg, dry=self.dry)

    def private_engine(self):
        return transport_module().LocalPrivateEngine(self.cfg, dry=self.dry)

    def _off_owner(self, fn):
        """Run network or model work on a thread that does not receive the Store."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(fn).result()

    def _open_engine(self, factory):
        try:
            engine = factory()
        except Exception as exc:  # noqa: BLE001 - a missing transport is unavailable, not a fallback
            return None, type(exc).__name__
        try:
            ok, why = engine.available()
        except Exception as exc:  # noqa: BLE001
            return None, type(exc).__name__
        if not ok:
            return None, why or "unavailable"
        return engine, None

    def _binding_from(self, row) -> dict:
        return {
            "goal_id": int(row["goal_id"] if not isinstance(row, dict) else row["goal_id"]),
            "contract_version": int(row["contract_version"]),
            "strategy_version": int(row["strategy_version"]),
            "epoch": int(row["epoch"]),
        }

    def _runnable_binding(self, runnable) -> dict:
        return {
            "goal_id": int(runnable.goal.id),
            "contract_version": int(runnable.contract.version),
            "strategy_version": int(runnable.goal.current_strategy_version),
            "epoch": int(runnable.goal.epoch),
        }

    def _model_binding(self, runnable, role: str) -> dict:
        day = int(runnable.grant.limits["day"]["model_tokens"])
        binding = self._runnable_binding(runnable)
        binding["role"] = role
        binding["shared_day_limits"] = {"model_tokens": day}
        return binding

    def _record_model_call(
        self, *, goal_id, task_id, wave_id, role, state, operation_id, error_code,
        record: Mapping | None, configured_model: str, configured_transport: str,
        frame_hash: str, snapshot_ids: list,
    ) -> None:
        """Persist producer metadata. A missing record does not borrow reply.model or zero the tokens."""
        if state not in {"pending", "complete", "failed", "lost"}:
            state = "failed"
        if record is None:
            requested = configured_model.strip() if isinstance(configured_model, str) and configured_model.strip() else "unknown"
            reported = None
            transport = configured_transport.strip() if isinstance(configured_transport, str) and configured_transport.strip() else "unknown"
            version = "unknown"
            stored_hash = frame_hash.strip() if isinstance(frame_hash, str) and frame_hash.strip() else None
            ids = [str(item) for item in snapshot_ids]
            tokens_in = None
            tokens_out = None
            thinking = None
            usage_kind = "unknown"
            stored_role = role
        else:
            requested = _record_text(record, "requested_model") or "unknown"
            reported = _record_text(record, "reported_model")
            transport = _record_text(record, "transport") or "unknown"
            version = _record_text(record, "prompt_version") or "unknown"
            stored_hash = _record_text(record, "public_frame_hash")
            raw_ids = record.get("source_snapshot_ids")
            if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, (list, tuple)):
                ids = []
            else:
                ids = [str(item) for item in raw_ids if isinstance(item, (str, int)) and not isinstance(item, bool)]
            tokens_in = _record_count(record.get("tokens_in"))
            tokens_out = _record_count(record.get("tokens_out"))
            thinking = _record_count(record.get("reasoning_tokens"))
            usage_kind = record.get("usage_kind")
            if usage_kind not in {"actual", "estimated", "legacy", "unknown"}:
                usage_kind = "unknown"
            stored_role = _record_text(record, "role") or role
        with self.store.tx() as db:
            db.execute(
                """INSERT INTO model_calls(
                       goal_id, task_id, wave_id, role, requested_model, reported_model, transport,
                       prompt_version, public_frame_hash, source_snapshot_ids_json, tokens_in, tokens_out,
                       thinking_tokens, usage_kind, state, reservation_operation_id, started_at, finished_at, error_code
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    goal_id, task_id, wave_id, stored_role, requested, reported, transport, version,
                    stored_hash, json.dumps(ids), tokens_in, tokens_out, thinking, usage_kind, state,
                    operation_id, now(), now(), error_code,
                ),
            )

    def _finish_model(
        self, *, reply, goal_id: int, task_id, wave_id, role: str, state: str, operation_key: str,
        error_code, configured_model: str, configured_transport: str, frame_hash: str, snapshot_ids: list,
        settle_amount: int | None = None,
    ) -> dict | None:
        record = _producer_record(reply)
        self._record_model_call(
            goal_id=goal_id, task_id=task_id, wave_id=wave_id, role=role, state=state,
            operation_id=self._operation_view(goal_id, operation_key).get("operation_id"),
            error_code=error_code, record=record, configured_model=configured_model,
            configured_transport=configured_transport, frame_hash=frame_hash, snapshot_ids=snapshot_ids,
        )
        if settle_amount is not None and state == "complete":
            actual = _model_actual(record, settle_amount)
            try:
                _sibling("ledger").settle_operation(
                    self.store, operation_key, int(goal_id), {"model_tokens": actual},
                )
            except Exception:  # noqa: BLE001
                self._settle_operation(int(goal_id), operation_key)
        return record

    def _operation_view(self, goal_id: int, operation_key: str) -> dict:
        try:
            return _sibling("ledger").admission_state(self.store, int(goal_id), operation_key)
        except Exception as exc:  # noqa: BLE001
            log(f"admission state skipped: {type(exc).__name__}")
            return {"state": None, "reservations": [], "operation_id": None}

    def _lose_operation(self, goal_id: int, operation_key: str | None) -> None:
        if not operation_key:
            return
        ledger = _sibling("ledger")
        view = self._operation_view(goal_id, operation_key)
        if view.get("state") == "reserved":
            try:
                ledger.release_undispatched(self.store, operation_key, int(goal_id))
            except Exception as exc:  # noqa: BLE001
                log(f"release skipped {operation_key}: {type(exc).__name__}")
            return
        for row in view.get("reservations") or []:
            if row.get("state") == "reserved":
                try:
                    ledger.mark_lost(self.store, int(row["id"]))
                except Exception as exc:  # noqa: BLE001
                    log(f"lost settlement skipped: {type(exc).__name__}")

    def _settle_operation(self, goal_id: int, operation_key: str | None) -> None:
        if not operation_key:
            return
        view = self._operation_view(goal_id, operation_key)
        actuals = {}
        for row in view.get("reservations") or []:
            if row.get("state") == "reserved":
                actuals[row["counter"]] = int(row["amount"])
        if not actuals:
            return
        try:
            _sibling("ledger").settle_operation(self.store, operation_key, int(goal_id), actuals)
        except Exception as exc:  # noqa: BLE001
            log(f"settle skipped {operation_key}: {type(exc).__name__}")
            self._lose_operation(goal_id, operation_key)

    def _expire_slot(self, task_id: int) -> None:
        with self.store.tx() as db:
            db.execute("UPDATE domain_slots SET lease_until=? WHERE task_id=?", (now() - 1, task_id))

    def _gate_result(self) -> dict | None:
        goal_mod = _sibling("goal")
        reason = goal_mod.runnable_reason(self.store, now())
        if reason == "runnable":
            return None
        return {
            "outcome": reason,
            "status": reason,
            "reason": reason,
            "substate": "research_ready" if reason == "research_ready" else "blocked",
            "topics": [],
            "planned_calls": [],
            "external_calls": 0,
            "dry_run": bool(self.dry),
        }

    def _stopped_result(self) -> dict:
        return {
            "stopped": True, "outcome": "stopped", "status": "stopped", "reason": "user_stopped",
            "substate": "user_stopped", "topics": [], "planned_calls": [], "external_calls": 0,
        }

    def claim_task(self, task_id: int) -> int | None:
        with self.store.tx() as db:
            r = db.execute(
                "UPDATE tasks SET status='claimed', worker_id=?, occurrence_seq=occurrence_seq+1, "
                "claimed_at=strftime('%s','now'), heartbeat_at=strftime('%s','now'), attempts=attempts+1 "
                "WHERE id=? AND status='pending' RETURNING occurrence_seq",
                (self.worker_id, task_id),
            ).fetchone()
        return r["occurrence_seq"] if r else None

    def heartbeat(self, task_id: int, seq: int) -> bool:
        """Heartbeat active task every 60s. Returns True if lease held, False if reaped/invalidated."""
        with self.store.tx() as db:
            cur = db.execute(
                "UPDATE tasks SET heartbeat_at=strftime('%s','now') "
                "WHERE id=? AND occurrence_seq=? AND status='claimed' AND worker_id=?",
                (task_id, seq, self.worker_id),
            )
            return cur.rowcount == 1

    def finish_task(
        self,
        task_id: int,
        seq: int,
        status: str,
        result: dict | None = None,
        error: str | None = None,
        refund: bool = False,
        retry_after: int = 0,
    ) -> bool:
        with self.store.tx() as db:
            if refund:  # outage: the attempt is refunded and the task waits for the window
                cur = db.execute(
                    "UPDATE tasks SET status='pending', attempts=attempts-1, outage_pauses=outage_pauses+1, "
                    "next_attempt_at=?, error=?, worker_id=NULL WHERE id=? AND occurrence_seq=? AND status='claimed' AND worker_id=?",
                    (now() + retry_after, error, task_id, seq, self.worker_id),
                )
            else:
                cur = db.execute(
                    "UPDATE tasks SET status=CASE WHEN ?='failed' AND attempts<max_attempts THEN 'pending' ELSE ? END, "
                    "result=?, error=?, next_attempt_at=strftime('%s','now')+60, worker_id=NULL WHERE id=? AND occurrence_seq=? AND status='claimed' AND worker_id=?",
                    (status, status, json.dumps(result) if result is not None else None, error, task_id, seq, self.worker_id),
                )
        return cur.rowcount == 1  # 0 rows: a reaper took the task while we worked; discard

    def reap(self) -> int:
        stale_ops: list[tuple] = []
        try:
            stale_ops = [
                (int(row["goal_id"]), str(row["operation_key"]))
                for row in self.store.all(
                    """SELECT goal_id, operation_key FROM tasks
                        WHERE status='claimed' AND heartbeat_at < strftime('%s','now') - 600
                          AND goal_id IS NOT NULL AND operation_key IS NOT NULL AND operation_key != ''"""
                )
            ]
        except sqlite3.OperationalError:
            stale_ops = []
        with self.store.tx() as db:
            cur = db.execute(
                "UPDATE tasks SET status=CASE WHEN attempts>=max_attempts THEN 'failed' ELSE 'pending' END, "
                "error='reaped: heartbeat stale', worker_id=NULL, occurrence_seq=occurrence_seq+1, "
                "next_attempt_at=strftime('%s','now')+60 "
                "WHERE status='claimed' AND heartbeat_at < strftime('%s','now') - 600"
            )
            n = cur.rowcount
            # a crash between claim and record leaves a page 'claimed'; nothing else would ever pick it up
            db.execute(
                "UPDATE pages SET status=CASE WHEN fetch_count>0 THEN 'scheduled' ELSE 'queued' END "
                "WHERE status='claimed' AND id NOT IN (SELECT page_id FROM fetches WHERE at > strftime('%s','now') - 3600)"
            )
        for goal_id, operation_key in stale_ops:
            view = self._operation_view(goal_id, operation_key)
            if view.get("state") == "dispatched":
                self._lose_operation(goal_id, operation_key)
            elif view.get("state") == "reserved":
                try:
                    _sibling("ledger").release_undispatched(self.store, operation_key, goal_id)
                except Exception as exc:  # noqa: BLE001 - undispatched release is best-effort
                    log(f"reaper release skipped {operation_key}: {type(exc).__name__}")
        return n

    def commit_with_reclaimed_occurrence(self, old_seq: int, task_id: int | None = None) -> bool:
        """Verify that attempting to commit side effects with a reclaimed occurrence sequence fails."""
        with self.store.tx() as db:
            if task_id is None:
                row = db.execute(
                    "SELECT 1 FROM tasks WHERE occurrence_seq=? AND status='claimed' AND worker_id=?",
                    (old_seq, self.worker_id),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT 1 FROM tasks WHERE id=? AND occurrence_seq=? AND status='claimed' AND worker_id=?",
                    (task_id, old_seq, self.worker_id),
                ).fetchone()
            return row is not None

    def new_task(self, db: sqlite3.Connection, kind: str, payload: dict, wave_id: int, priority: float = 0.5) -> int:
        db.execute(
            "INSERT INTO waves(id, started_at) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
            (wave_id, now()),
        )
        return db.execute(
            "INSERT INTO tasks(kind, payload, priority, next_attempt_at, wave_id, created_at) VALUES (?,?,?,?,?,?)",
            (kind, json.dumps(payload), priority, now(), wave_id, now()),
        ).lastrowid

    # ---- the batch ----
    def run(self, max_topics: int, slug_filter: str | None) -> dict:
        if STOP_FILE.exists() and not self.dry:
            log("STOP file present; not running")
            return self._stopped_result()
        gated = self._gate_result()
        if gated is not None:
            return gated
        goal_mod = _sibling("goal")
        try:
            runnable = goal_mod.runnable_goal(self.store, now())
        except Exception as exc:  # noqa: BLE001
            return {
                "outcome": "no_contract", "status": "no_contract", "reason": type(exc).__name__,
                "substate": "blocked", "topics": [], "planned_calls": [], "external_calls": 0,
            }
        if runnable is None:
            return self._gate_result() or self._stopped_result()
        if self.dry or getattr(self.store, "readonly", False):
            return self._dry_wave(runnable, slug_filter)
        return self._research_wave(runnable, max_topics, slug_filter)

    def supervise(self, max_topics: int = 1, interval_sec: int = 60, slug_filter: str | None = None) -> int:
        """Always the open store path. None is an in-memory store, not data/snowball.db."""
        return _sibling("goal_runtime").supervise(
            self.cfg,
            self.topics_path,
            self.dry,
            db_path=self.store.path,
            max_topics=max_topics,
            interval_sec=interval_sec,
            slug_filter=slug_filter,
        )

    def _dry_wave(self, runnable, slug_filter: str | None) -> dict:
        planned: list[str] = []
        try:
            coverage = _sibling("qualify").coverage(self.store, int(runnable.goal.id), int(runnable.contract.version))
            for proposal in _sibling("planner").gap_proposals(self.store, runnable, coverage):
                query = getattr(proposal, "query", None)
                if query:
                    planned.append(f"search: {query}")
        except Exception as exc:  # noqa: BLE001 - dry-run reports the skip and writes nothing
            planned.append(f"plan skipped: {type(exc).__name__}")
        return {
            "outcome": "dry-run",
            "status": "dry-run",
            "reason": "dry_run",
            "substate": "progressing",
            "dry_run": True,
            "goal_id": int(runnable.goal.id),
            "contract_version": int(runnable.contract.version),
            "grant_id": int(runnable.grant.id),
            "requested_topic": slug_filter,
            "topics": [],
            "planned_calls": planned,
            "external_calls": 0,
        }

    def _research_wave(self, runnable, max_topics: int, slug_filter: str | None) -> dict:
        del max_topics
        self.reap()
        planner = _sibling("planner")
        qualify = _sibling("qualify")
        goal_mod = _sibling("goal")
        substate = "progressing"
        reason = "wave"
        schedule_error = None
        with self.store.tx() as db:
            wave_id = db.execute("INSERT INTO waves(started_at) VALUES (?)", (now(),)).lastrowid
        coverage = None
        try:
            coverage = qualify.coverage(self.store, int(runnable.goal.id), int(runnable.contract.version))
        except Exception as exc:  # noqa: BLE001
            schedule_error = type(exc).__name__
            coverage = None
        proposals = []
        if coverage is not None:
            try:
                proposals = list(planner.gap_proposals(self.store, runnable, coverage))
            except Exception as exc:  # noqa: BLE001
                schedule_error = type(exc).__name__
                proposals = []
        try:
            proposals.extend(self._hypothesis_proposals(runnable, coverage, wave_id))
        except Exception as exc:  # noqa: BLE001 - a plan failure does not abort admitted fetch work
            schedule_error = type(exc).__name__
            log(f"hypotheses skipped: {schedule_error}")
        validated = []
        try:
            validated = planner.validate_proposals(self.store, runnable, proposals)
            planner.schedule(self.store, runnable, validated, int(wave_id))
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if name == "GrantExhausted":
                substate = "exhausted"
                reason = "grant_exhausted"
            else:
                schedule_error = name
                if substate == "progressing":
                    substate = "planner_error"
                    reason = name
        self._dispatch_admitted(runnable, int(wave_id), runnable.frame)
        try:
            completion = qualify.completion_status(self.store, runnable.goal)
        except Exception as exc:  # noqa: BLE001
            completion = None
            log(f"completion status skipped: {type(exc).__name__}")
        if completion is not None and completion.ready:
            fresh = goal_mod.get_goal(self.store, runnable.goal.id)
            try:
                goal_mod.transition(
                    self.store, fresh.id, "research_ready", "completion predicate satisfied",
                    fresh.row_version, "worker", f"ready-{wave_id}",
                )
                substate = "research_ready"
                reason = "research_ready"
            except Exception as exc:  # noqa: BLE001
                log(f"research_ready transition skipped: {type(exc).__name__}")
        elif completion is not None and getattr(completion, "substate", "") == "no_match":
            substate = "no_match"
            reason = "no_match"
        elif substate == "progressing" and schedule_error:
            substate = "planner_error"
            reason = schedule_error
        substate = self._write_runtime(runnable, int(wave_id), substate, reason)
        outcome = {
            "research_ready": "research_ready",
            "exhausted": "exhausted",
            "no_match": "no_match",
            "verifier_unavailable": "outage",
            "planner_error": "planner_error",
            "stalled": "ok",
        }.get(substate, "ok")
        with self.store.tx() as db:
            db.execute(
                "UPDATE waves SET ended_at=?, status=? WHERE id=?",
                (now(), "aborted" if outcome == "planner_error" else "done", wave_id),
            )
        return {
            "wave": wave_id,
            "outcome": outcome,
            "status": outcome,
            "reason": reason,
            "substate": substate,
            "dry_run": False,
            "goal_id": int(runnable.goal.id),
            "contract_version": int(runnable.contract.version),
            "grant_id": int(runnable.grant.id),
            "requested_topic": slug_filter,
            "topics": [],
            "planned_calls": [],
            "external_calls": self.external_calls,
            "schedule_error": schedule_error,
        }

    def _hypothesis_proposals(self, runnable, coverage, wave_id: int) -> list:
        engine, why = self._open_engine(self.plan_engine)
        if engine is None:
            log(f"plan engine unavailable: {why}")
            return []
        op = f"plan-{int(runnable.goal.id)}-{wave_id}"
        amount = max(1, min(int(self.cfg.plan_tokens_per_wave), int(runnable.grant.limits["day"]["model_tokens"])))
        try:
            _sibling("ledger").reserve_batch(
                self.store, int(runnable.grant.id), op, {"model_tokens": amount}, self._model_binding(runnable, "plan"),
            )
            _sibling("ledger").mark_dispatched(self.store, op, int(runnable.goal.id))
        except Exception as exc:  # noqa: BLE001
            log(f"plan reservation skipped: {type(exc).__name__}")
            return []
        captured: dict[str, Any] = {}

        def plan(*args, **kwargs):
            def work():
                return engine.plan(*args, **kwargs)
            captured["reply"] = self._off_owner(work)
            self.external_calls += 1
            return captured["reply"]

        class _Capture:
            def plan(self, *args, **kwargs):
                return plan(*args, **kwargs)

            def available(self):
                return True, "ok"

        frame_hash = getattr(runnable.contract, "frame_hash", "") or ""
        try:
            proposals = _sibling("planner").planner_proposals(
                _Capture(),
                runnable.frame,
                coverage,
                {
                    "db": self.store.db,
                    "binding": self._runnable_binding(runnable),
                    "operation_key": op,
                    "max_out": int(self.cfg.plan_tokens_per_wave),
                    "exploration_budget": int(self.cfg.plan_max_hypotheses),
                    "store": self.store,
                    "goal": runnable,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log(f"planner proposals failed: {type(exc).__name__}")
            self._lose_operation(int(runnable.goal.id), op)
            self._finish_model(
                reply=captured.get("reply"), goal_id=int(runnable.goal.id), task_id=None, wave_id=wave_id,
                role="plan", state="lost", operation_key=op, error_code=type(exc).__name__,
                configured_model=self.cfg.plan_model or self.cfg.review_model or "plan",
                configured_transport=self.cfg.plan_transport or "unknown", frame_hash=frame_hash, snapshot_ids=[],
            )
            return []
        self._finish_model(
            reply=captured.get("reply"), goal_id=int(runnable.goal.id), task_id=None, wave_id=wave_id,
            role="plan", state="complete", operation_key=op, error_code=None,
            configured_model=self.cfg.plan_model or self.cfg.review_model or "plan",
            configured_transport=self.cfg.plan_transport or "unknown", frame_hash=frame_hash, snapshot_ids=[],
            settle_amount=amount,
        )
        return list(proposals or [])

    def _dispatch_admitted(self, runnable, wave_id: int, frame) -> None:
        seen: set[int] = set()
        limit = max(1, int(self.cfg.pages_per_topic) + int(self.cfg.queries_per_topic))
        for _ in range(limit):
            task = self.store.one(
                """SELECT * FROM tasks
                    WHERE goal_id=? AND kind IN ('fetch','search') AND status IN ('pending','claimed')
                      AND (worker_id IS NULL OR worker_id=?)
                    ORDER BY id LIMIT 1""",
                (int(runnable.goal.id), self.worker_id),
            )
            if task is None or int(task["id"]) in seen:
                return
            seen.add(int(task["id"]))
            try:
                self._dispatch_task(runnable, wave_id, frame, task)
            except Exception as exc:  # noqa: BLE001 - one task must not abort the wave
                log(f"dispatch {task['id']} failed: {type(exc).__name__}: {str(exc)[:200]}")

    def _task_current(self, task_id: int, seq: int, token: str | None) -> bool:
        row = self.store.one(
            """SELECT occurrence_seq, lease_token, status, worker_id, goal_id, contract_version,
                      strategy_version, epoch FROM tasks WHERE id=?""",
            (task_id,),
        )
        if row is None or int(row["occurrence_seq"]) != int(seq) or row["status"] != "claimed":
            return False
        if row["worker_id"] != self.worker_id:
            return False
        if token and row["lease_token"] != token:
            return False
        with self.store.tx() as db:
            return bool(_sibling("goal").binding_valid(
                db, int(row["goal_id"]), int(row["contract_version"]), int(row["strategy_version"]), int(row["epoch"]),
            ))

    def _claim_for_dispatch(self, task_id: int, seq: int) -> bool:
        with self.store.tx() as db:
            cur = db.execute(
                """UPDATE tasks SET status='claimed', worker_id=?, heartbeat_at=?, claimed_at=COALESCE(claimed_at, ?)
                    WHERE id=? AND occurrence_seq=? AND status IN ('pending','claimed')
                      AND (worker_id IS NULL OR worker_id=?)""",
                (self.worker_id, now(), now(), task_id, seq, self.worker_id),
            )
            return cur.rowcount == 1

    def _dispatch_task(self, runnable, wave_id: int, frame, task) -> None:
        goal_mod = _sibling("goal")
        with self.store.tx() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (int(task["id"]),)).fetchone()
            if row is None or row["status"] not in ("pending", "claimed"):
                return
            if not goal_mod.binding_valid(
                db, int(row["goal_id"]), int(row["contract_version"]), int(row["strategy_version"]), int(row["epoch"]),
            ):
                return
        payload = json.loads(row["payload"] or "{}")
        if row["kind"] == "fetch":
            self._dispatch_fetch(runnable, wave_id, frame, row, payload)
        else:
            self._dispatch_search(runnable, wave_id, row, payload)

    def _ensure_lease(self, runnable, row, domain: str) -> tuple[str, str, int]:
        token = row["lease_token"]
        op = row["operation_key"]
        if token and op:
            return str(op), str(token), int(row["occurrence_seq"])
        token = secrets.token_hex(16)
        op = f"lease-{int(row['id'])}-{token[:12]}"
        until = now() + 600
        binding = self._binding_from(row)
        binding.update({"domain": domain, "task_id": int(row["id"]), "lease_token": token, "lease_until": until})
        _sibling("ledger").reserve_batch(
            self.store, int(runnable.grant.id), op, {"fetches": 1}, binding,
        )
        fresh = self.store.one("SELECT occurrence_seq, operation_key, lease_token FROM tasks WHERE id=?", (int(row["id"]),))
        return str(fresh["operation_key"] or op), str(fresh["lease_token"] or token), int(fresh["occurrence_seq"])

    def _dispatch_fetch(self, runnable, wave_id: int, frame, row, payload: dict) -> None:
        page_id = payload.get("page_id")
        if not isinstance(page_id, int):
            return
        page = self.store.one("SELECT * FROM pages WHERE id=?", (page_id,))
        if page is None:
            return
        if page["topic_id"] is None:
            return
        topic = self.store.one("SELECT * FROM topics WHERE id=?", (page["topic_id"],))
        family = self.store.one("SELECT * FROM families WHERE name=?", (topic["family"],)) if topic else None
        if topic is None or family is None:
            return
        op, token, seq = self._ensure_lease(runnable, row, str(page["domain"]))
        if not self._claim_for_dispatch(int(row["id"]), seq):
            return
        view = self._operation_view(int(runnable.goal.id), op)
        if view.get("state") == "dispatched":
            return
        if view.get("state") == "reserved":
            _sibling("ledger").mark_dispatched(self.store, op, int(runnable.goal.id))
        url = page["canon_url"]
        private = bool(topic["private"])
        fetch_fn = self.router.fetch
        if getattr(fetch_fn, "__func__", None) is FetchRouter.fetch:
            fetch_fn = FetchRouter(self.cfg, None, self.x, self.dry).fetch

        def work():
            return fetch_fn(url, private=private, page=None)

        try:
            result = self._off_owner(work)
            self.external_calls += 1
        except Exception as exc:  # noqa: BLE001
            log(f"fetch failed: {type(exc).__name__}")
            self._lose_operation(int(runnable.goal.id), op)
            self.finish_task(int(row["id"]), seq, "failed", error=type(exc).__name__)
            self._expire_slot(int(row["id"]))
            return
        if not self._task_current(int(row["id"]), seq, token):
            self._raw_attempt(page_id, result)
            self._lose_operation(int(runnable.goal.id), op)
            return
        run = TopicRun(topic, family, private)
        self.record_fetch(run, page, result, int(row["id"]), seq)
        if result.outcome == "ok" and (result.text or "").strip() and not private:
            fetch = self.store.one(
                "SELECT id FROM fetches WHERE page_id=? AND outcome='ok' ORDER BY id DESC LIMIT 1", (page_id,),
            )
            if fetch is not None:
                self._qualify_fetch(
                    runnable, wave_id, frame, row, page, int(fetch["id"]), result.text, seq, token,
                )
        self._settle_operation(int(runnable.goal.id), op)
        self.finish_task(int(row["id"]), seq, "done", result={"outcome": result.outcome})
        self._expire_slot(int(row["id"]))

    def _raw_attempt(self, page_id: int, result) -> None:
        with self.store.tx() as db:
            db.execute(
                """INSERT INTO fetches(page_id, at, route, http_status, bytes, ms, outcome, pinned_ip, due)
                   VALUES (?,?,?,?,?,?,?,?,0)""",
                (
                    page_id, now(), result.route or "direct", result.status, result.nbytes or len(result.text or ""),
                    result.ms, result.outcome, result.pinned_ip,
                ),
            )

    def _qualify_fetch(
        self, runnable, wave_id: int, frame, row, page, fetch_id: int, text: str, seq: int, token: str | None,
    ) -> None:
        qualify = _sibling("qualify")
        cycle = _evidenced_source_cycle(text)
        applicability = {"cycle_label": cycle} if cycle else None
        try:
            snapshot_id = qualify.record_snapshot(
                self.store, int(page["id"]), text, fetch_id, public_fetch=True,
                source_cycle_label=cycle, applicability=applicability,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"snapshot skipped: {type(exc).__name__}")
            return
        if not self._task_current(int(row["id"]), seq, token):
            return
        source = self._public_source(int(snapshot_id))
        if source is None:
            log("public snapshot unavailable")
            return
        program = self._public_program(int(page["id"]), source.text, source.title)
        field_keys = self._public_field_keys(frame)
        if not program.get("name") and not program.get("program_key"):
            log("public program unknown")
            return
        if not field_keys:
            log("public field keys unknown")
            return
        binding = self._binding_from(row)
        binding["task_id"] = int(row["id"])
        observation_ids = self._extract_observations(
            runnable, wave_id, frame, source, program, field_keys, binding, seq, token,
        )
        if not observation_ids:
            return
        self._review_observations(runnable, wave_id, frame, source, binding, observation_ids, seq, token)
        if not self._task_current(int(row["id"]), seq, token):
            return
        self._evaluate_current(runnable, binding)

    def _public_source(self, snapshot_id: int):
        """prompts.PublicSourceSnapshot from the immutable snapshot row. Owner ids stay off the object."""
        qualify = _sibling("qualify")
        prompts = _sibling("prompts")
        row = self.store.one(
            """SELECT s.id, s.content_hash, s.visibility, s.provenance, s.source_cycle_label,
                      s.applicability_json, s.superseded_by, p.canon_url, p.title
                 FROM source_snapshots s
                 JOIN pages p ON p.id = s.page_id
                WHERE s.id=?""",
            (int(snapshot_id),),
        )
        if row is None or row["superseded_by"] is not None:
            return None
        if row["visibility"] != "public" or row["provenance"] != "public_fetch":
            return None
        try:
            text = qualify.read_snapshot(self.store, int(snapshot_id))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(text, str) or not text:
            return None
        try:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        except UnicodeError:
            return None
        if row["content_hash"] != digest:
            return None
        cycle = row["source_cycle_label"]
        if cycle is not None and (not isinstance(cycle, str) or cycle not in text):
            return None
        try:
            stored = json.loads(row["applicability_json"] or "{}")
        except json.JSONDecodeError:
            return None
        applicability = _public_applicability(stored, text)
        if applicability is None:
            return None
        title = row["title"] if isinstance(row["title"], str) else ""
        if len(title) > 300 or any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
            title = ""
        url = row["canon_url"]
        if not isinstance(url, str) or not url:
            return None
        try:
            return prompts.PublicSourceSnapshot(
                id=int(row["id"]),
                url=url,
                title=title,
                text=text,
                content_hash=digest,
                source_cycle_label=cycle,
                applicability=applicability,
                visibility="public",
                provenance="public_fetch",
            )
        except Exception:  # noqa: BLE001
            return None

    def _public_program(self, page_id: int, text: str, title: str) -> dict:
        """Observed program metadata, or the page title when that title is in the source. Never a guessed name."""
        program: dict = {}
        row = None
        try:
            row = self.store.one(
                """SELECT pr.key, pr.name, pr.institution, pr.official_domain, pr.degree_level,
                          pr.modality, pr.field_tags_json
                     FROM programs pr
                     JOIN observations o ON o.program_key = pr.key
                    WHERE o.page_id=? AND pr.provenance='observed'
                    ORDER BY pr.key
                    LIMIT 1""",
                (int(page_id),),
            )
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            key = row["key"]
            if isinstance(key, str) and _PROGRAM_KEY_RE.fullmatch(key):
                program["program_key"] = key
            if _verbatim_label(row["name"], text, 200):
                program["name"] = row["name"]
            if _verbatim_label(row["institution"], text, 200):
                program["institution"] = row["institution"]
            domain = row["official_domain"]
            if isinstance(domain, str) and domain and " " not in domain and "." in domain and len(domain) <= 200:
                program["official_domain"] = domain
            degree = row["degree_level"]
            if degree in {"masters", "phd", "other"} and degree.replace("_", " ") in text.casefold():
                program["degree_level"] = degree
            modality = row["modality"]
            if modality in {"on_campus", "online", "hybrid"} and modality.replace("_", " ") in text.casefold():
                program["modality"] = modality
            try:
                tags = json.loads(row["field_tags_json"] or "[]")
            except json.JSONDecodeError:
                tags = []
            if (
                isinstance(tags, list)
                and tags
                and len(tags) <= 16
                and all(isinstance(item, str) and item and item in text and len(item) <= 80 for item in tags)
            ):
                program["field_tags"] = list(tags)
        if not program.get("name") and not program.get("program_key") and _verbatim_label(title, text, 200):
            program["name"] = title
        return {key: value for key, value in program.items() if key in _PROGRAM_META_KEYS}

    def _public_field_keys(self, frame) -> list[str]:
        allowed = set(_sibling("qualify").FIELD_KEYS)
        raw = getattr(frame, "required_source_fields", ()) or ()
        keys: list[str] = []
        for item in raw:
            if isinstance(item, str) and item in allowed and item not in keys:
                keys.append(item)
        return keys

    def _public_observations(self, observation_ids: list, source) -> list[dict]:
        rows = []
        for obs_id in observation_ids:
            row = self.store.one(
                """SELECT id, snapshot_id, field_key, value_json, subject_json, currency, period,
                          quote, assurance, conditions_json
                     FROM observations WHERE id=?""",
                (int(obs_id),),
            )
            if row is None:
                continue
            item = _review_observation(row, source)
            if item is not None:
                rows.append(item)
        return rows

    def _extract_observations(
        self, runnable, wave_id, frame, source, program: dict, field_keys: list, binding: dict, seq: int, token: str | None,
    ) -> list:
        engine, why = self._open_engine(self.extract_engine)
        op = f"extract-{int(runnable.goal.id)}-{int(source.id)}-{wave_id}"
        if engine is None:
            log(f"extract engine unavailable: {why}")
            return []
        try:
            frozen = _engine_binding(binding)
        except TypeError:
            log("extract binding refused")
            return []
        amount = max(1, min(4000, int(runnable.grant.limits["day"]["model_tokens"])))
        try:
            _sibling("ledger").reserve_batch(
                self.store, int(runnable.grant.id), op, {"model_tokens": amount}, self._model_binding(runnable, "extract"),
            )
            _sibling("ledger").mark_dispatched(self.store, op, int(runnable.goal.id))
        except Exception as exc:  # noqa: BLE001
            log(f"extract reservation skipped: {type(exc).__name__}")
            return []
        max_out = int(self.cfg.extract_budget.get("gemini", (0, 4000))[1] or 4000)

        def work():
            return extract_page_entities(frame, program, field_keys, source, frozen, op, engine, max_out)

        try:
            reply = self._off_owner(work)
            self.external_calls += 1
        except Exception as exc:  # noqa: BLE001
            self._lose_operation(int(runnable.goal.id), op)
            self._finish_model(
                reply=None, goal_id=int(runnable.goal.id), task_id=binding.get("task_id"), wave_id=wave_id,
                role="extract", state="lost", operation_key=op, error_code=type(exc).__name__,
                configured_model=self.cfg.gemini_model, configured_transport=self.cfg.gemini_transport or "unknown",
                frame_hash=getattr(runnable.contract, "frame_hash", "") or "", snapshot_ids=[int(source.id)],
            )
            return []
        self._finish_model(
            reply=reply, goal_id=int(runnable.goal.id), task_id=binding.get("task_id"), wave_id=wave_id,
            role="extract", state="complete", operation_key=op, error_code=None,
            configured_model=self.cfg.gemini_model, configured_transport=self.cfg.gemini_transport or "unknown",
            frame_hash=getattr(runnable.contract, "frame_hash", "") or "", snapshot_ids=[int(source.id)],
            settle_amount=amount,
        )
        if not self._task_current(int(binding["task_id"]), seq, token):
            return []
        if not _sibling("goal").binding_valid(
            self.store.db, binding["goal_id"], binding["contract_version"], binding["strategy_version"], binding["epoch"],
        ):
            return []
        observed_program, observations = _observations_from_reply(getattr(reply, "text", "") or "", source.text)
        if not observations:
            return []
        try:
            return list(_sibling("qualify").record_observations(
                self.store, int(source.id), observed_program, observations, binding,
                reported or asked, prompt_version,
            ))
        except Exception as exc:  # noqa: BLE001
            log(f"observations skipped: {type(exc).__name__}: {exc}")
            return []

    def _review_observations(
        self, runnable, wave_id, frame, source, binding, observation_ids: list, seq: int, token: str | None,
    ) -> None:
        qualify = _sibling("qualify")
        engine, why = self._open_engine(self.review_engine)
        if engine is None:
            try:
                qualify.record_reviews(self.store, observation_ids, None, unavailable=True)
            except Exception as exc:  # noqa: BLE001
                log(f"unavailable review skipped: {type(exc).__name__}")
            self._note_substate(int(runnable.goal.id), "verifier_unavailable", why or "review unavailable")
            return
        public_obs = self._public_observations(observation_ids, source)
        if len(public_obs) != len(observation_ids):
            log("review observations are not a public envelope")
            try:
                qualify.record_reviews(self.store, observation_ids, None, unavailable=True)
            except Exception as exc:  # noqa: BLE001
                log(f"unavailable review skipped: {type(exc).__name__}")
            return
        try:
            frozen = _engine_binding(binding)
        except TypeError:
            log("review binding refused")
            return
        op = f"review-{int(runnable.goal.id)}-{wave_id}-{observation_ids[0]}"
        amount = max(1, min(int(self.cfg.review_budget[1] or 8000), int(runnable.grant.limits["day"]["model_tokens"])))
        max_out = int(self.cfg.review_budget[1] or 8000)
        try:
            _sibling("ledger").reserve_batch(
                self.store, int(runnable.grant.id), op, {"model_tokens": amount}, self._model_binding(runnable, "review"),
            )
            _sibling("ledger").mark_dispatched(self.store, op, int(runnable.goal.id))
        except Exception as exc:  # noqa: BLE001
            log(f"review reservation skipped: {type(exc).__name__}")
            qualify.record_reviews(self.store, observation_ids, None, unavailable=True)
            return

        def work():
            return engine.review(frame, public_obs, source, frozen, op, max_out)

        try:
            reply = self._off_owner(work)
            self.external_calls += 1
        except Exception as exc:  # noqa: BLE001
            self._lose_operation(int(runnable.goal.id), op)
            qualify.record_reviews(self.store, observation_ids, None, unavailable=True)
            self._note_substate(int(runnable.goal.id), "verifier_unavailable", type(exc).__name__)
            self._finish_model(
                reply=None, goal_id=int(runnable.goal.id), task_id=binding.get("task_id"), wave_id=wave_id,
                role="review", state="lost", operation_key=op, error_code=type(exc).__name__,
                configured_model=self.cfg.review_model or "review",
                configured_transport=self.cfg.review_transport or "unknown",
                frame_hash=getattr(runnable.contract, "frame_hash", "") or "", snapshot_ids=[int(source.id)],
            )
            return
        record = self._finish_model(
            reply=reply, goal_id=int(runnable.goal.id), task_id=binding.get("task_id"), wave_id=wave_id,
            role="review", state="complete", operation_key=op, error_code=None,
            configured_model=self.cfg.review_model or "review",
            configured_transport=self.cfg.review_transport or "unknown",
            frame_hash=getattr(runnable.contract, "frame_hash", "") or "", snapshot_ids=[int(source.id)],
            settle_amount=amount,
        )
        if not self._task_current(int(binding["task_id"]), seq, token):
            return
        verdicts = _verdicts_from_reply(getattr(reply, "text", "") or "")
        try:
            qualify.record_reviews(
                self.store, observation_ids, verdicts, review_model=_reviewer_name(record),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"review record failed: {type(exc).__name__}")
            qualify.record_reviews(self.store, observation_ids, None, unavailable=True)
            self._note_substate(int(runnable.goal.id), "verifier_unavailable", type(exc).__name__)

    def _evaluate_current(self, runnable, binding: dict) -> None:
        with self.store.tx() as db:
            current = _sibling("goal").binding_valid(
                db, binding["goal_id"], binding["contract_version"], binding["strategy_version"], binding["epoch"],
            )
        if not current:
            return
        qualify = _sibling("qualify")
        try:
            programs = self.store.all(
                """SELECT DISTINCT program_key FROM observations
                    WHERE goal_id=? AND contract_version=? AND epoch=? AND assurance='reviewed_kept'
                      AND program_key IS NOT NULL AND invalidated_at IS NULL""",
                (binding["goal_id"], binding["contract_version"], binding["epoch"]),
            )
        except Exception:  # noqa: BLE001
            return
        for program in programs:
            key = program["program_key"]
            if not isinstance(key, str) or not key:
                continue
            cycles = self.store.all(
                """SELECT DISTINCT s.source_cycle_label AS cycle
                     FROM observations o
                     JOIN source_snapshots s ON s.id = o.snapshot_id
                    WHERE o.program_key=? AND o.goal_id=? AND o.contract_version=? AND o.epoch=?
                      AND o.assurance='reviewed_kept' AND o.invalidated_at IS NULL
                      AND s.visibility='public' AND s.provenance='public_fetch'
                      AND s.superseded_by IS NULL AND s.source_cycle_label IS NOT NULL""",
                (key, binding["goal_id"], binding["contract_version"], binding["epoch"]),
            )
            for cycle_row in cycles:
                label = cycle_row["cycle"]
                if not isinstance(label, str) or not label:
                    continue
                try:
                    for package in qualify.derive_funding_packages(self.store, key, label):
                        qualify.evaluate(
                            self.store, runnable.goal, binding["contract_version"], package.key, binding["epoch"],
                        )
                except Exception as exc:  # noqa: BLE001
                    log(f"evaluate skipped: {type(exc).__name__}")

    def _dispatch_search(self, runnable, wave_id: int, row, payload: dict) -> None:
        del wave_id
        query = payload.get("query") or payload.get("node_key")
        if not isinstance(query, str) or not query.strip():
            return
        op = row["operation_key"]
        seq = int(row["occurrence_seq"])
        if not self._claim_for_dispatch(int(row["id"]), seq):
            return
        if op:
            view = self._operation_view(int(runnable.goal.id), op)
            if view.get("state") == "reserved":
                try:
                    _sibling("ledger").mark_dispatched(self.store, op, int(runnable.goal.id))
                except Exception as exc:  # noqa: BLE001
                    log(f"search dispatch skipped: {type(exc).__name__}")
                    return
        source = payload.get("source") if isinstance(payload.get("source"), str) else "hn"
        search = self.router.search
        if getattr(search, "__func__", None) is FetchRouter.search:
            search = FetchRouter(self.cfg, None, self.x, self.dry).search

        def work():
            return search(query, source, int(self.cfg.grok_x_window_days))

        try:
            hits = self._off_owner(work)
            self.external_calls += 1
        except Exception as exc:  # noqa: BLE001
            self._lose_operation(int(runnable.goal.id), op)
            self.finish_task(int(row["id"]), seq, "failed", error=type(exc).__name__)
            return
        if not self._task_current(int(row["id"]), seq, row["lease_token"]):
            self._lose_operation(int(runnable.goal.id), op)
            return
        self._enqueue_hits(runnable, row, hits or [])
        self._settle_operation(int(runnable.goal.id), op)
        self.finish_task(int(row["id"]), seq, "done", result={"hits": len(hits or [])})

    def _enqueue_hits(self, runnable, row, hits) -> None:
        topic = self.store.one(
            "SELECT topic_id FROM goal_topics WHERE goal_id=? AND contract_version=? LIMIT 1",
            (int(runnable.goal.id), int(runnable.contract.version)),
        )
        topic_id = int(topic["topic_id"]) if topic else None
        if topic_id is None:
            return
        created = 0
        for hit in hits:
            if created >= int(self.cfg.pages_per_topic):
                break
            url = getattr(hit, "url", None) or (hit.get("url") if isinstance(hit, dict) else None)
            if not isinstance(url, str):
                continue
            with self.store.tx() as db:
                if not _sibling("goal").binding_valid(
                    db, int(row["goal_id"]), int(row["contract_version"]), int(row["strategy_version"]), int(row["epoch"]),
                ):
                    return
                page_id = self.locks.enqueue_url(db, url, topic_id, "search", 7)
                if page_id is None:
                    continue
                payload = {"page_id": int(page_id), "url": url, "origin": "search"}
                db.execute(
                    """INSERT INTO tasks(
                           kind, payload, priority, status, next_attempt_at, created_at, wave_id,
                           goal_id, contract_version, strategy_version, epoch, grant_id)
                       VALUES ('fetch', ?, 0.4, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        json.dumps(payload), now(), now(), row["wave_id"], int(row["goal_id"]),
                        int(row["contract_version"]), int(row["strategy_version"]), int(row["epoch"]),
                        int(runnable.grant.id),
                    ),
                )
            created += 1

    def _note_substate(self, goal_id: int, substate: str, reason: str) -> None:
        with self.store.tx() as db:
            db.execute(
                """INSERT INTO goal_runtime(goal_id, substate, reason, stall_count, updated_at)
                   VALUES (?,?,?,0,?)
                   ON CONFLICT(goal_id) DO UPDATE SET substate=excluded.substate, reason=excluded.reason, updated_at=excluded.updated_at""",
                (goal_id, substate, (reason or "")[:500], now()),
            )

    def _write_runtime(self, runnable, wave_id: int, substate: str, reason: str) -> str:
        planner = _sibling("planner")
        work = coverage = None
        try:
            work = planner.work_fingerprint(self.store, runnable)
            coverage = planner.coverage_fingerprint(self.store, runnable)
        except Exception as exc:  # noqa: BLE001
            log(f"fingerprint skipped: {type(exc).__name__}")
        previous = self.store.one("SELECT * FROM goal_runtime WHERE goal_id=?", (int(runnable.goal.id),))
        stall = 0
        if previous and coverage and previous["coverage_fingerprint"] == coverage:
            stall = int(previous["stall_count"] or 0) + 1
            if substate == "progressing":
                substate = "stalled"
        elif previous and coverage and previous["coverage_fingerprint"] and previous["coverage_fingerprint"] != coverage:
            fresh = _sibling("goal").get_goal(self.store, int(runnable.goal.id))
            if fresh is not None and fresh.state == "running":
                try:
                    _sibling("goal").transition(
                        self.store, fresh.id, "replan", "evidence fingerprint changed",
                        fresh.row_version, "worker", f"replan-{wave_id}",
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"replan skipped: {type(exc).__name__}")
        with self.store.tx() as db:
            db.execute(
                """INSERT INTO goal_runtime(
                       goal_id, substate, reason, work_fingerprint, coverage_fingerprint, stall_count, last_wave_id, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(goal_id) DO UPDATE SET
                       substate=excluded.substate, reason=excluded.reason,
                       work_fingerprint=COALESCE(excluded.work_fingerprint, goal_runtime.work_fingerprint),
                       coverage_fingerprint=COALESCE(excluded.coverage_fingerprint, goal_runtime.coverage_fingerprint),
                       stall_count=excluded.stall_count, last_wave_id=excluded.last_wave_id, updated_at=excluded.updated_at""",
                (int(runnable.goal.id), substate, (reason or "")[:500], work, coverage, stall, wave_id, now()),
            )
        return substate


    def seed_queries(self, run: TopicRun) -> list[tuple[str, str, int | None]]:
        """Code seeds, the web supplies (I1): queries come from the topic text and pending names, never a model."""
        t = run.topic
        focus = norm_text(t["focus"])[:300]
        kw = keyword_query(focus)
        # Exa and grok read natural language; Algolia, Arctic Shift and GitHub AND every word, so they get keywords
        out: list[tuple[str, str, int | None]] = [(focus, "exa", None)]
        if t["family"] in ("alpha", "opportunities") and not run.private:
            out.append((focus, "grok", None))  # X route: public topics only, it leaves the box
        out += [(kw, s, None) for s in ("hn", "arctic", "github") if kw]
        for v in json.loads(t["focus_variants"] or "[]")[:2]:
            out.append((norm_text(v)[:300], "exa", None))
        names = self.store.all(
            "SELECT id, raw FROM names WHERE topic_id=? AND status='pending' ORDER BY id LIMIT 3", (t["id"],)
        )
        # names go first: each unknown name gets exactly one search, and that is what snowballs the crawl
        return ([(n["raw"], "exa", n["id"]) for n in names] + out)[: self.cfg.queries_per_topic]

    def wave0_kb_first(self, run: TopicRun) -> None:
        t, days = run.topic, run.family["recheck_days"]
        exa_ok = self.dry or self.router.exa_allowed()
        with self.store.tx() as db:
            for raw, source, name_id in self.seed_queries(run):
                if source == "exa" and not exa_ok:
                    run.counts["exa_reserve_skip"] += 1
                    continue
                via = "name" if name_id else "seed"
                if name_id:
                    db.execute("UPDATE names SET status='searched', searched_at=? WHERE id=?", (now(), name_id))
                q = fts_terms(raw, 8)
                if q:
                    hits = db.execute(
                        "SELECT count(*) FROM claims_fts f JOIN claims c ON c.rowid=f.rowid "
                        "WHERE claims_fts MATCH ? AND c.last_seen_at > ?",
                        (q, now() - days * 86400),
                    ).fetchone()[0]
                    if hits >= self.cfg.kb_answer_min_claims:
                        key = query_key(raw, source, days, now())
                        db.execute(
                            "INSERT INTO queries(query_key, norm, raw, source, window_key, topic_id, discovered_via, "
                            "status, scheduled_at, ran_at, result_count) VALUES (?,?,?,?,?,?,?, 'done', ?, ?, 0) "
                            "ON CONFLICT(query_key) DO UPDATE SET dup_requests=dup_requests+1",
                            (key, norm_query(raw), raw, source, window_key(days, now()), t["id"], via, now(), now()),
                        )
                        run.kb_answered += 1
                        continue
                qid = self.locks.query_lock(db, raw, source, days, t["id"], via=via)
                if qid and name_id:
                    db.execute("UPDATE queries SET name_id=? WHERE id=?", (name_id, qid))
                if qid:
                    run.queries.append(db.execute("SELECT * FROM queries WHERE id=?", (qid,)).fetchone())
        # Consume pending deepen tasks
        deepen_tasks = self.store.all(
            "SELECT id, payload FROM tasks WHERE kind='deepen' AND status='pending' AND json_extract(payload, '$.topic_id')=? LIMIT 2",
            (t["id"],),
        )
        for dt in deepen_tasks:
            try:
                pl = json.loads(dt["payload"])
                dfocus = pl.get("focus")
                if dfocus:
                    with self.store.tx() as db:
                        qid = self.locks.query_lock(db, dfocus, "exa", days, t["id"], via="name")
                        if qid:
                            run.queries.append(db.execute("SELECT * FROM queries WHERE id=?", (qid,)).fetchone())
                        db.execute("UPDATE tasks SET status='done' WHERE id=?", (dt["id"],))
            except Exception as e:
                log(f"deepen task error: {e}")

        # Replay paused/pending queries
        existing_pending = self.store.all(
            "SELECT * FROM queries WHERE topic_id=? AND status='pending' AND scheduled_at <= strftime('%s','now') LIMIT 4",
            (t["id"],),
        )
        have_qids = {q["id"] for q in run.queries}
        for ep in existing_pending:
            if ep["id"] not in have_qids:
                run.queries.append(ep)
        log(
            f"[{t['key']}] wave0: {len(run.queries)} queries to run, {run.kb_answered} answered by the KB, "
            f"{self.locks.metrics['query_dup']} locked"
        )

    # ---- Wave 1: search + fetch ----
    def wave1_fetch(self, run: TopicRun, wave_id: int) -> None:
        t, days = run.topic, run.family["recheck_days"]
        hits_by_canon: dict[str, SearchHit] = {}
        for q in run.queries:
            with self.store.tx() as db:
                db.execute("UPDATE queries SET status='running' WHERE id=?", (q["id"],))
            try:
                hits = self.router.search(
                    q["raw"],
                    q["source"],
                    self.cfg.grok_x_window_days if q["source"] == "grok" else days * 4,
                    t["family"],
                )
                status = "done"
            except EngineOutage as e:
                log(f"[{t['key']}] {q['source']} outage: {e}")
                hits, status = [], "pending"  # refunded: the query stays pending for the next window
            except Exception as e:  # noqa: BLE001 - one dead source must not stop the topic
                log(f"[{t['key']}] search {q['source']} failed: {type(e).__name__}: {str(e)[:160]}")
                hits, status = [], "failed"
            new = 0
            with self.store.tx() as db:
                for h in hits:
                    c = canonical_url(h.url)
                    if not c:
                        continue
                    hits_by_canon.setdefault(c, h)
                    if self.locks.enqueue_url(
                        db,
                        h.url,
                        t["id"],
                        "name" if q["discovered_via"] == "name" else "seed",
                        days,
                        predicted="item" if h.route == "grok" else None,
                    ):
                        new += 1
                db.execute(
                    "UPDATE queries SET status=?, ran_at=?, result_count=?, new_pages=? WHERE id=?",
                    (status, now(), len(hits), new, q["id"]),
                )
                if q["source"] == "exa" and status == "done":
                    db.execute(
                        "INSERT INTO budgets(day, exa_calls) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET "
                        "exa_calls=exa_calls+1",
                        (today_ist(),),
                    )
            run.counts["search_hits"] += len(hits)
        pops = self.pop_frontier(t["id"], self.cfg.pages_per_topic)
        if not pops:
            log(f"[{t['key']}] wave1: frontier dry")
            return
        with self.store.tx() as db:
            for p in pops:
                db.execute("UPDATE pages SET status='claimed' WHERE id=?", (p["id"],))
        log(f"[{t['key']}] wave1: fetching {len(pops)} page(s) with concurrency {self.cfg.collector_concurrency}")
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.collector_concurrency)) as ex:
            results = list(
                ex.map(
                    lambda p: (
                        p,
                        self.router.fetch(
                            json.loads(p["raw_urls"])[0],
                            hits_by_canon.get(p["canon_url"]),
                            private=run.private,
                            page=p,
                        ),
                    ),
                    pops,
                )
            )
        for p, r in results:
            self.record_fetch(run, p, r)

    def pop_frontier(self, topic_id: int, n: int) -> list[sqlite3.Row]:
        """The section 5.3 score, restricted to one topic, max 4 pages per domain."""
        rows = self.store.all(
            """
        WITH cand AS (
          SELECT p.id, p.domain, p.canon_url, p.raw_urls, p.status, p.fetch_count, p.topic_id,
                 CASE WHEN p.fetch_count = 0
                      THEN MAX(0.3, 1.0 - 0.02 * d.fetch_count) * (CASE WHEN d.junk_count > 0 THEN 0.5 ELSE 1.0 END)
                      ELSE (COALESCE(h.rows_added_total, 0) + 1.0) / (p.fetch_count + 2.0) END AS novelty,
                 f.weight * CASE COALESCE(p.kind, p.predicted_kind, 'unsure') WHEN 'hub' THEN 1.0 WHEN 'item' THEN 0.9
                                 WHEN 'article' THEN 0.5 ELSE 0.6 END
                   * (COALESCE(pp.new_items_total, 0) + 1.0) / (COALESCE(pp.fetch_count, 0) + 2.0)
                   * MAX(0.1, 1.0 + 0.2 * d.feedback_score) AS value,
                 1.0 + MIN(1.0, (strftime('%s','now') - p.next_check_at) / 86400.0) AS due
            FROM pages p
            JOIN domains d ON d.domain = p.domain
            LEFT JOIN topics t ON t.id = p.topic_id
            JOIN families f ON f.name = COALESCE(t.family, 'opportunities')
            LEFT JOIN hubs h ON h.page_id = p.id
            LEFT JOIN pages pp ON pp.id = p.parent_page_id
           WHERE p.status IN ('queued','scheduled') AND p.topic_id = ?
             AND p.next_check_at <= strftime('%s','now')
             AND (p.next_attempt_at IS NULL OR p.next_attempt_at <= strftime('%s','now'))
             AND d.status = 'ok' AND (d.paused_until IS NULL OR d.paused_until <= strftime('%s','now')))
        SELECT *, novelty * value * due AS score FROM cand ORDER BY score DESC LIMIT ?""",
            (topic_id, n * 4),
        )
        per_dom: Counter = Counter()
        out = []
        for r in rows:
            if per_dom[r["domain"]] >= 4:
                continue
            per_dom[r["domain"]] += 1
            out.append(r)
            if len(out) >= n:
                break
        if len(out) < 4:
            added = self.refill_frontier(topic_id)
            if added > 0:
                # Reselect from frontier so newly enqueued pages are popped and not ignored
                re_rows = self.store.all(
                    """
                WITH cand AS (
                  SELECT p.id, p.domain, p.canon_url, p.raw_urls, p.status, p.fetch_count, p.topic_id,
                         CASE WHEN p.fetch_count = 0
                              THEN MAX(0.3, 1.0 - 0.02 * d.fetch_count) * (CASE WHEN d.junk_count > 0 THEN 0.5 ELSE 1.0 END)
                              ELSE (COALESCE(h.rows_added_total, 0) + 1.0) / (p.fetch_count + 2.0) END AS novelty,
                         f.weight * CASE COALESCE(p.kind, p.predicted_kind, 'unsure') WHEN 'hub' THEN 1.0 WHEN 'item' THEN 0.9
                                         WHEN 'article' THEN 0.5 ELSE 0.6 END
                           * (COALESCE(pp.new_items_total, 0) + 1.0) / (COALESCE(pp.fetch_count, 0) + 2.0)
                           * MAX(0.1, 1.0 + 0.2 * d.feedback_score) AS value,
                         1.0 + MIN(1.0, (strftime('%s','now') - p.next_check_at) / 86400.0) AS due
                    FROM pages p
                    JOIN domains d ON d.domain = p.domain
                    LEFT JOIN topics t ON t.id = p.topic_id
                    JOIN families f ON f.name = COALESCE(t.family, 'opportunities')
                    LEFT JOIN hubs h ON h.page_id = p.id
                    LEFT JOIN pages pp ON pp.id = p.parent_page_id
                   WHERE p.status IN ('queued','scheduled') AND p.topic_id = ?
                     AND p.next_check_at <= strftime('%s','now')
                     AND (p.next_attempt_at IS NULL OR p.next_attempt_at <= strftime('%s','now'))
                     AND d.status = 'ok' AND (d.paused_until IS NULL OR d.paused_until <= strftime('%s','now')))
                SELECT *, novelty * value * due AS score FROM cand ORDER BY score DESC LIMIT ?""",
                    (topic_id, n * 4),
                )
                re_dom: Counter = Counter()
                re_out = []
                for r in re_rows:
                    if re_dom[r["domain"]] >= 4:
                        continue
                    re_dom[r["domain"]] += 1
                    re_out.append(r)
                    if len(re_out) >= n:
                        break
                if len(re_out) > len(out):
                    return re_out
        return out

    def refill_frontier(self, topic_id: int) -> int:
        """Fill dry frontier when < 4 pages are due (docs/SYSTEM_DESIGN.md section 6.8):
        1. Exa findSimilar on top-5 high-yield hubs under reserve
        2. Backlink discovery from page_links
        3. Name snowball from pending names
        """
        added = 0
        topic = self.store.one("SELECT * FROM topics WHERE id=?", (topic_id,))
        if not topic:
            return 0
        days = 14
        top_hubs = self.store.all(
            "SELECT p.id, p.canon_url FROM hubs h JOIN pages p ON p.id=h.page_id "
            "WHERE p.topic_id=? ORDER BY h.rows_added_total DESC LIMIT 5",
            (topic_id,),
        )
        # 1. Exa findSimilar under reserve
        if top_hubs and self.router.exa_allowed():
            for hub in top_hubs:
                try:
                    similar_hits = self.router.find_similar(hub["canon_url"])
                    for hit in similar_hits:
                        with self.store.tx() as db:
                            pid = self.locks.enqueue_url(db, hit.url, topic_id, "similar", days, parent=hub["id"])
                            if pid:
                                added += 1
                except Exception as e:
                    log(f"findSimilar error for {hub['canon_url']}: {e}")
        # 2. Backlink discovery
        if top_hubs and added < 4:
            hub_canons = [h["canon_url"] for h in top_hubs]
            placeholders = ",".join("?" * len(hub_canons))
            backlink_rows = self.store.all(
                f"SELECT DISTINCT p.canon_url FROM page_links pl "
                f"JOIN pages p ON p.id=pl.from_page_id "
                f"WHERE pl.to_canon_url IN ({placeholders}) AND p.id NOT IN (SELECT page_id FROM hubs)",
                tuple(hub_canons),
            )
            for bl in backlink_rows:
                with self.store.tx() as db:
                    pid = self.locks.enqueue_url(db, bl["canon_url"], topic_id, "backlink", days)
                    if pid:
                        added += 1
        # 3. Name snowball
        if added < 4:
            pending_names = self.store.all(
                "SELECT n.id, n.raw, p.domain FROM names n JOIN pages p ON p.id=n.first_page_id "
                "WHERE n.topic_id=? AND n.status='pending' ORDER BY p.new_items_total DESC LIMIT 5",
                (topic_id,),
            )
            for pn in pending_names:
                with self.store.tx() as db:
                    qid = self.locks.query_lock(db, pn["raw"], "exa", days, topic_id, via="name")
                    if qid:
                        db.execute("UPDATE names SET status='searched', searched_at=? WHERE id=?", (now(), pn["id"]))
                        added += 1
        return added

    def record_fetch(
        self,
        run: TopicRun,
        p: sqlite3.Row,
        r: FetchResult,
        task_id: int | None = None,
        occurrence_seq: int | None = None,
    ) -> None:
        """Fetch record, outage/failure split (4.7), walls (4.6), then page locks 2b/2c, classification, drift, hubs."""
        ts, pid, dom = now(), p["id"], p["domain"]
        known_routes = {"direct", "exa_contents", "zenrows", "resi_proxy", "arctic", "hn", "github", "grok", "feed", "browser"}
        route = r.route if r.route in known_routes else "direct"
        if self.dry:
            run.pages.append({"page_id": pid, "result": r, "reprint": False, "domain": dom})
            return
        with self.store.tx() as db:
            if task_id is not None:
                valid = db.execute(
                    "SELECT 1 FROM tasks WHERE id=? AND occurrence_seq=? AND status='claimed' AND worker_id=?",
                    (task_id, occurrence_seq, self.worker_id),
                ).fetchone()
                if not valid:
                    return  # Stale worker lease; zero writes allowed
            cur_f = db.execute(
                "INSERT INTO fetches(page_id, at, route, http_status, bytes, ms, outcome, pinned_ip, due) "
                "VALUES (?,?,?,?,?,?,?,?,1)",
                (pid, ts, route, r.status, r.nbytes or len(r.text), r.ms, r.outcome, r.pinned_ip),
            )
            fetch_id = cur_f.lastrowid
            db.execute(
                "INSERT INTO access_attempts(fetch_id, page_id, mode, route, status, http_status, bytes, ms, error, at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (fetch_id, pid, getattr(r, "mode", "direct"), route, r.outcome, r.status, r.nbytes or len(r.text), r.ms, None if r.outcome == "ok" else r.outcome, ts),
            )
            db.execute(
                "INSERT INTO budgets(day, fetches) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET fetches=fetches+1",
                (today_ist(),),
            )
            db.execute("UPDATE domains SET fetch_count=fetch_count+1 WHERE domain=?", (dom,))
            if r.outcome == "outage":
                d = db.execute("SELECT outage_streak FROM domains WHERE domain=?", (dom,)).fetchone()
                n = (d["outage_streak"] if d else 0) + 1
                pause = r.retry_after or min(900 * 2 ** (n - 1), 86400)
                db.execute("UPDATE domains SET outage_streak=?, paused_until=? WHERE domain=?", (n, ts + pause, dom))
                db.execute(
                    "UPDATE pages SET status=?, next_attempt_at=? WHERE id=?",
                    ("scheduled" if p["fetch_count"] else "queued", ts + pause, pid),
                )  # attempt refunded
                run.counts["outage"] += 1
                return
            if r.outcome in ("failure", "ssrf_blocked"):
                s = db.execute("UPDATE pages SET strikes=strikes+1 WHERE id=? RETURNING strikes", (pid,)).fetchone()[
                    "strikes"
                ]
                if s >= 3 or r.outcome == "ssrf_blocked":
                    db.execute("UPDATE pages SET status='dead' WHERE id=?", (pid,))
                else:
                    db.execute(
                        "UPDATE pages SET status=?, next_attempt_at=? WHERE id=?",
                        ("scheduled" if p["fetch_count"] else "queued", ts + 3600 * s, pid),
                    )
                if r.outcome == "ssrf_blocked":
                    blocks = db.execute(
                        "SELECT count(*) FROM fetches f JOIN pages p ON p.id=f.page_id "
                        "WHERE p.domain=? AND f.outcome='ssrf_blocked'",
                        (dom,),
                    ).fetchone()[0]
                    if blocks >= 3:
                        db.execute("UPDATE domains SET status='blocked' WHERE domain=?", (dom,))
                run.counts["failure"] += 1
                return
            db.execute("UPDATE domains SET outage_streak=0 WHERE domain=?", (dom,))
            if r.outcome == "walled":
                db.execute(
                    "UPDATE pages SET status='walled', wall_kind=?, strikes=0, "
                    "next_check_at=? + CASE WHEN walled_count=0 THEN 30*86400 ELSE 90*86400 END, "
                    "walled_count=walled_count+1 WHERE id=?",
                    (r.wall_kind or "http", ts, pid),
                )
                db.execute("UPDATE domains SET walled_count=walled_count+1 WHERE domain=?", (dom,))
                run.counts["walled"] += 1
                return
            hits = injection_hits(r.text)
            raw_path = RAW_DIR / f"{content_hash(r.text)[:16]}.txt"
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(r.text)
            db.execute(
                "UPDATE pages SET status='fetched', http_status=?, route=?, title=?, text_len=?, truncated=?, "
                "raw_path=?, fetched_at=?, fetch_count=fetch_count+1, strikes=0 WHERE id=?",
                (
                    r.status,
                    route,
                    r.title[:300],
                    len(r.text),
                    int(r.truncated),
                    str(raw_path.relative_to(ROOT) if raw_path.is_relative_to(ROOT) else raw_path),
                    ts,
                    pid,
                ),
            )
            if hits:
                db.execute(
                    "UPDATE pages SET status='quarantined', quarantined=1, quarantine_reasons=? WHERE id=?",
                    (",".join(hits), pid),
                )
                with open(ROOT / "QUARANTINE.md", "a") as fh:
                    fh.write(f"- {datetime.now(IST):%Y-%m-%d %H:%M} {p['canon_url']} ({', '.join(hits)}) {raw_path}\n")
                run.counts["quarantined"] += 1
                return

            # Classification
            kind = classify_page(r.title, r.text, p["canon_url"], r.links, engine=None)
            db.execute("UPDATE pages SET kind=? WHERE id=?", (kind, pid))

            # Family drift floor
            fam_focus = run.family["focus"] if "focus" in run.family.keys() else ""
            relevance = compute_family_relevance(r.title, r.text, fam_focus)
            db.execute("UPDATE pages SET relevance=? WHERE id=?", (relevance, pid))
            floor = float(run.family["relevance_floor"] if "relevance_floor" in run.family.keys() else 0.08)
            if relevance < floor:
                db.execute("UPDATE pages SET status='dropped' WHERE id=?", (pid,))
                run.counts["drift_dropped"] = run.counts.get("drift_dropped", 0) + 1
                return  # off-topic: dropped and cannot expand links

            # Content locks (same / alias / reprint)
            verdict, other = self.locks.content_lock(db, pid, r.text)
            if verdict == "same":
                db.execute(
                    "UPDATE pages SET status='scheduled', interval_sec=MIN(interval_sec*2, 90*86400), "
                    "next_check_at=? + MIN(interval_sec*2, 90*86400) WHERE id=?",
                    (ts, pid),
                )
                run.counts["unchanged"] += 1
                return
            if verdict == "alias":
                db.execute("UPDATE pages SET status='alias', alias_of=? WHERE id=?", (other, pid))
                run.counts["alias"] += 1
                return
            if verdict == "reprint":
                db.execute("UPDATE pages SET status='reprint', reprint_of=? WHERE id=?", (other, pid))
                for i, (href, anchor) in enumerate(r.links[:400]):
                    c = canonical_url(urljoin(r.final_url or p["canon_url"], href))
                    if c and c != p["canon_url"]:
                        db.execute(
                            "INSERT OR IGNORE INTO page_links(from_page_id, to_canon_url, anchor, position) "
                            "VALUES (?,?,?,?)",
                            (pid, c, anchor[:200], i),
                        )
                return  # reprints suppress model extraction

            if p["fetch_count"] and kind != "hub":  # a changed re-check: halve the interval (I6)
                db.execute("UPDATE pages SET interval_sec=MAX(interval_sec/2, 6*3600) WHERE id=?", (pid,))

            # Pure hub row extraction and diffs. The first sight is a baseline: record rows,
            # do not enqueue them, and do not change the interval. Later sights enqueue only
            # rows that were not in that baseline, and only those shorten the interval.
            if kind == "hub":
                db.execute("INSERT INTO hubs(page_id) VALUES (?) ON CONFLICT(page_id) DO NOTHING", (pid,))
                existing_rows = {
                    row["row_hash"]: row for row in db.execute(
                        "SELECT row_hash, to_canon_url, anchor FROM hub_rows WHERE hub_page_id=?", (pid,)
                    ).fetchall()
                }
                baseline = not existing_rows
                seen_now = set()
                new_hub_rows = []
                for href, anchor in (r.links or []):
                    c = canonical_url(urljoin(r.final_url or p["canon_url"], href))
                    if c and c != p["canon_url"]:
                        norm_a = norm_text(anchor or "")
                        r_hash = hashlib.sha1(f"{c}|{norm_a}".encode()).hexdigest()
                        seen_now.add(r_hash)
                        if r_hash not in existing_rows:
                            new_hub_rows.append((pid, r_hash, c, norm_a[:200], ts, ts))
                            if not baseline:
                                self.locks.enqueue_url(db, c, p["topic_id"], "hub", run.family["recheck_days"], parent=pid)
                        else:
                            db.execute("UPDATE hub_rows SET last_seen_at=? WHERE hub_page_id=? AND row_hash=?", (ts, pid, r_hash))
                if new_hub_rows and not baseline:
                    db.executemany(
                        "INSERT OR IGNORE INTO hub_rows(hub_page_id, row_hash, to_canon_url, anchor, first_seen_at, last_seen_at) "
                        "VALUES (?,?,?,?,?,?)",
                        new_hub_rows,
                    )
                    db.execute(
                        "UPDATE pages SET interval_sec=MAX(interval_sec/2, 6*3600), "
                        "next_check_at=? + MAX(interval_sec/2, 6*3600), status='scheduled' WHERE id=?",
                        (ts, pid),
                    )
                    db.execute(
                        "UPDATE hubs SET item_count=?, rows_added_total=rows_added_total+?, last_diff_at=? WHERE page_id=?",
                        (len(seen_now), len(new_hub_rows), ts, pid),
                    )
                    run.counts["hub_new_rows"] = run.counts.get("hub_new_rows", 0) + len(new_hub_rows)
                elif new_hub_rows and baseline:
                    db.executemany(
                        "INSERT OR IGNORE INTO hub_rows(hub_page_id, row_hash, to_canon_url, anchor, first_seen_at, last_seen_at) "
                        "VALUES (?,?,?,?,?,?)",
                        new_hub_rows,
                    )
                    db.execute(
                        "UPDATE pages SET status='scheduled', next_check_at=? + interval_sec WHERE id=?",
                        (ts, pid),
                    )
                    db.execute(
                        "UPDATE hubs SET item_count=?, last_diff_at=? WHERE page_id=?",
                        (len(seen_now), ts, pid),
                    )
                else:
                    db.execute(
                        "UPDATE pages SET interval_sec=MIN(interval_sec*2, 90*86400), "
                        "next_check_at=? + MIN(interval_sec*2, 90*86400), status='scheduled' WHERE id=?",
                        (ts, pid),
                    )
                    db.execute(
                        "UPDATE hubs SET item_count=?, last_diff_at=? WHERE page_id=?",
                        (len(seen_now), ts, pid),
                    )
                    run.counts["hub_unchanged"] = run.counts.get("hub_unchanged", 0) + 1
                return  # Hubs use code-defined row extraction; do NOT extract via LLM

            for i, (href, anchor) in enumerate(r.links[:400]):
                c = canonical_url(urljoin(r.final_url or p["canon_url"], href))
                if c and c != p["canon_url"]:
                    db.execute(
                        "INSERT OR IGNORE INTO page_links(from_page_id, to_canon_url, anchor, position) "
                        "VALUES (?,?,?,?)",
                        (pid, c, anchor[:200], i),
                    )
        run.pages.append({"page_id": pid, "result": r, "reprint": False, "domain": dom})
        run.counts["fetched"] += 1
    # ---- Wave 2: extraction ----
    def load_leftovers(self, run: TopicRun) -> None:
        """Pages fetched by an earlier run whose extraction was deferred (outage, engine down) rejoin here."""
        have = {pg["page_id"] for pg in run.pages}
        for p in self.store.all(
            "SELECT id, canon_url, domain, title, raw_path, reprint_of FROM pages "
            "WHERE topic_id=? AND status='fetched' AND raw_path IS NOT NULL ORDER BY id LIMIT ?",
            (run.topic["id"], self.cfg.pages_per_topic),
        ):
            if p["id"] in have or not (ROOT / p["raw_path"]).exists():
                continue
            r = FetchResult(p["canon_url"], "direct", "ok", 200, p["title"] or "", (ROOT / p["raw_path"]).read_text())
            run.pages.append(
                {"page_id": p["id"], "result": r, "reprint": p["reprint_of"] is not None, "domain": p["domain"]}
            )
            run.counts["leftover"] += 1

    def wave4_commit(self, run: TopicRun, wave_id: int) -> None:
        t, days = run.topic, run.family["recheck_days"]
        paying = 0
        with self.store.tx() as db:
            for pg in run.pages:
                pid = pg["page_id"]
                if pid in run.deferred:
                    continue
                v = run.verified.get(pid)
                before = (
                    self.locks.metrics["entity_new"],
                    self.locks.metrics["entity_changed"],
                    self.locks.metrics["claim_new"],
                )
                new_rows = 0
                if v:
                    for row in v["rows"]:
                        verdict = self.locks.entity_lock(db, row, t["family"], pid, pg["reprint"])
                        new_rows += verdict in ("new", "changed")
                    for c in v["claims"]:
                        self.locks.claim_lock(db, c["text"], pid, t["id"], pg["domain"], c["_span"], pg["reprint"])
                    for name in v["names"][:25]:
                        nn = norm_name(name)[0]
                        known = db.execute("SELECT 1 FROM entities WHERE norm_name=?", (nn,)).fetchone()
                        if nn and not known:
                            db.execute(
                                "INSERT INTO names(norm_name, raw, first_page_id, topic_id, created_at) "
                                "VALUES (?,?,?,?,?) ON CONFLICT(norm_name) DO NOTHING",
                                (nn, name, pid, t["id"], now()),
                            )
                p = db.execute("SELECT hops_dry, depth FROM pages WHERE id=?", (pid,)).fetchone()
                gained = (
                    self.locks.metrics["entity_new"]
                    - before[0]
                    + self.locks.metrics["entity_changed"]
                    - before[1]
                    + self.locks.metrics["claim_new"]
                    - before[2]
                )
                hops = 0 if gained else p["hops_dry"] + 1
                paying += gained
                status = "reprint" if pg["reprint"] else "scheduled"
                interval = "90*86400" if hops >= 3 else "interval_sec"
                db.execute(
                    f"UPDATE pages SET status=?, kind=COALESCE(kind, ?), hops_dry=?, new_items_total=new_items_total+?, "
                    f"interval_sec={interval}, next_check_at=? + {interval} WHERE id=?",
                    (status, "item" if new_rows else "article", hops, gained, now(), pid),
                )
                db.execute(
                    "UPDATE domains SET new_items_total=new_items_total+? WHERE domain=?", (gained, pg["domain"])
                )
                if hops < 3 and v and not pg["reprint"]:  # dry branches stop spawning children (6.7)
                    for link in v["links"][:20]:
                        if link in pg["result"].text:
                            self.locks.enqueue_url(
                                db, link, t["id"], "hub", days, parent=pid, depth=p["depth"] + 1, hops_dry=hops
                            )
            db.execute(
                "UPDATE topics SET run_count=run_count+1, last_run_at=?, fetch_count=fetch_count+?, "
                "new_items_total=new_items_total+?, hops_dry=? WHERE id=?",
                (now(), run.counts["fetched"], paying, 0 if paying else t["hops_dry"] + 1, t["id"]),
            )
            if not run.queries and not run.pages:
                db.execute("UPDATE topics SET last_run_at=? WHERE id=?", (now(), t["id"]))
        run.counts["new_items"] = paying
        log(f"[{t['key']}] wave4: {paying} new or changed items committed")

    # ---- status ----
    def status(self) -> dict:
        s = self.store
        day = today_ist()
        frontier = {r["status"]: r["n"] for r in s.all("SELECT status, count(*) n FROM pages GROUP BY status")}
        tasks = {
            f"{r['kind']}:{r['status']}": r["n"]
            for r in s.all("SELECT kind, status, count(*) n FROM tasks GROUP BY 1,2")
        }
        spend = {}
        for r in s.all("SELECT * FROM engine_spend WHERE day=?", (day,)):
            cap = self.cfg.caps.get(r["engine"])
            spend[r["engine"]] = {
                "calls": r["calls"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
                "outages": r["outages"],
                "failures": r["failures"],
                "cap": cap,
                "remaining": self.budget.remaining(r["engine"]) if cap is not None else None,
            }
        roles = self._role_availability()
        yield_rows = s.all(
            "SELECT domain, status, fetch_count, new_items_total, walled_count, junk_count, "
            "ROUND((new_items_total + 1.0) / (fetch_count + 2.0), 3) AS yield FROM domains "
            "WHERE fetch_count > 0 ORDER BY yield DESC, fetch_count DESC LIMIT 15"
        )
        fetch_total = s.one("SELECT count(*) n FROM fetches WHERE at > ?", (now() - 86400,))["n"]
        repeats = s.one("SELECT count(*) n FROM fetches WHERE at > ? AND due = 0", (now() - 86400,))["n"]
        topics = [
            {"topic": r["key"], "score": round(r["novelty"] * r["value"] * r["due"], 3), "private": bool(r["private"])}
            for r in self.select_topics(10, None)
        ]
        return {
            "day": day,
            "frontier": frontier,
            "queries": {r["status"]: r["n"] for r in s.all("SELECT status, count(*) n FROM queries GROUP BY status")},
            "tasks": tasks,
            "entities": {r["status"]: r["n"] for r in s.all("SELECT status, count(*) n FROM entities GROUP BY status")},
            "unreported": {
                "entities": s.one("SELECT count(*) n FROM entities WHERE reported_at IS NULL")["n"],
                "changes": s.one("SELECT count(*) n FROM entity_changes WHERE reported_at IS NULL")["n"],
            },
            "claims": s.one("SELECT count(*) n FROM claims")["n"],
            "token_spend": spend,
            "global_tokens": {"used": sum(self.budget.spent()), "cap": self.cfg.llm_tokens_per_day},
            "roles": roles,
            "goal": self._status_goal(),
            "counters": self._status_counters(),
            "fetches_24h": fetch_total,
            "repeat_rate_24h": round(repeats / fetch_total, 4) if fetch_total else 0.0,
            "domain_yield": [dict(r) for r in yield_rows],
            "next_topics": topics,
        }

    def _role_availability(self) -> dict:
        """Configured role ids only. Status does not probe transports or print secrets."""

        def one(role: str, model: str, transport: str) -> dict:
            usage = "estimated" if transport in {"omp", "ssh-omp", "agy"} else "unknown"
            if not model or not transport:
                return {
                    "role": role, "status": "blocked", "model": model or None,
                    "transport": transport or None, "usage_kind": "unavailable",
                    "error": f"{role} model is not configured",
                }
            return {"role": role, "status": "configured", "model": model, "transport": transport, "usage_kind": usage}

        private_model = self.cfg.private_model_id or None
        endpoint = (self.cfg.private_model_endpoint or "").lower()
        if not self.cfg.private_model_endpoint or not self.cfg.private_model_id or not self.cfg.private_model_attested:
            private = {
                "role": "private_eval", "status": "blocked", "model": private_model,
                "usage_kind": "unavailable", "error": "private model is not configured",
            }
        elif any(token in endpoint for token in ("agy", "generativelanguage.googleapis.com", ":8088", "openrouter")):
            private = {
                "role": "private_eval", "status": "blocked", "model": private_model,
                "usage_kind": "unavailable", "error": "private endpoint matches a public transport",
            }
        else:
            private = {"role": "private_eval", "status": "configured", "model": private_model, "usage_kind": "unknown"}
        return {
            "extract": one("extract", self.cfg.gemini_model, self.cfg.gemini_transport),
            "review": one("review", self.cfg.review_model, self.cfg.review_transport),
            "plan": one("plan", self.cfg.plan_model or self.cfg.review_model, self.cfg.plan_transport or self.cfg.review_transport),
            "private": private,
        }

    def _status_goal(self) -> dict:
        try:
            goal = self.store.one(
                """SELECT id, slug, state, epoch, current_contract_version, current_strategy_version
                     FROM goals ORDER BY id DESC LIMIT 1"""
            )
        except sqlite3.OperationalError:
            return {}
        if goal is None:
            return {"state": "absent", "substate": "blocked"}
        grant = self.store.one(
            """SELECT id, state, mode, contract_version, expires_at FROM execution_grants
                WHERE goal_id=? ORDER BY id DESC LIMIT 1""",
            (goal["id"],),
        )
        runtime = self.store.one("SELECT substate, reason, stall_count, last_wave_id FROM goal_runtime WHERE goal_id=?", (goal["id"],))
        contract = self.store.one(
            "SELECT version, status FROM goal_contracts WHERE goal_id=? AND version=?",
            (goal["id"], goal["current_contract_version"]),
        )
        return {
            "id": goal["id"],
            "slug": goal["slug"],
            "state": goal["state"],
            "epoch": goal["epoch"],
            "contract_version": goal["current_contract_version"],
            "contract_status": None if contract is None else contract["status"],
            "strategy_version": goal["current_strategy_version"],
            "grant": None if grant is None else {
                "id": grant["id"], "state": grant["state"], "mode": grant["mode"],
                "contract_version": grant["contract_version"], "expires_at": grant["expires_at"],
            },
            "substate": None if runtime is None else runtime["substate"],
            "reason": None if runtime is None else runtime["reason"],
            "stall_count": 0 if runtime is None else runtime["stall_count"],
        }

    def _status_counters(self) -> list:
        try:
            return [dict(row) for row in self.store.all(
                "SELECT scope_key, window_kind, window_key, counter, reserved, settled, lost FROM resource_ledger"
            )]
        except sqlite3.OperationalError:
            return []


# ---------------------------------------------------------------------------------------------
# Deterministic checks used by waves 2 and 3.
# ---------------------------------------------------------------------------------------------


def _present(needle: str, hay_norm: str) -> bool:
    return bool(needle) and norm_text(needle).casefold() in hay_norm


def substring_guard(data: dict, source: str) -> dict:
    """Section 7.4. Names, names[] and quotes must be verbatim (casefold) in the chunks. Failures are removed."""
    hay = norm_text(source).casefold()
    rows, claims, names, links, dropped = [], [], [], [], 0
    for r in data.get("rows") or []:
        if not isinstance(r, dict):
            dropped += 1
            continue
        name, quote = str(r.get("name") or ""), str(r.get("evidence_quote") or "")
        if not (_present(name, hay) and 40 <= len(quote) <= 400 and _present(quote, hay)):
            dropped += 1
            continue
        amount = r.get("amount")
        try:
            amount = float(amount) if amount not in (None, "") else None
        except (TypeError, ValueError):
            amount = None
        dl = r.get("deadline")
        dl = dl if isinstance(dl, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", dl) else None
        link = (
            r.get("link") if isinstance(r.get("link"), str) and r["link"].startswith(("http://", "https://")) else None
        )
        rows.append(
            {
                "name": norm_text(name)[:200],
                "org": (norm_text(str(r["org"]))[:200] if r.get("org") else None),
                "kind": r.get("kind") if r.get("kind") in ENTITY_KINDS else "other",
                "amount": amount,
                "currency": (str(r["currency"]).upper()[:3] if r.get("currency") else None),
                "deadline": dl,
                "eligibility": (norm_text(str(r["eligibility"]))[:500] if r.get("eligibility") else None),
                "location": (norm_text(str(r["location"]))[:200] if r.get("location") else None),
                "link": link,
                "evidence_quote": norm_text(quote),
            }
        )
    for c in data.get("claims") or []:
        if (
            isinstance(c, dict)
            and c.get("text")
            and 40 <= len(str(c.get("evidence_quote") or "")) <= 400
            and _present(str(c["evidence_quote"]), hay)
        ):
            claims.append(
                {"text": norm_text(str(c["text"]))[:500], "evidence_quote": norm_text(str(c["evidence_quote"]))}
            )
        else:
            dropped += 1
    for n in data.get("names") or []:
        if isinstance(n, str) and 2 < len(n) <= 120 and _present(n, hay):
            names.append(norm_text(n))
    for u in data.get("links") or []:
        if isinstance(u, str) and u.startswith(("http://", "https://")) and u in source:
            links.append(u)
    return {"rows": rows, "claims": claims, "names": sorted(set(names)), "links": links, "dropped": dropped}


def mechanical_check(row: dict, page_text: str) -> tuple[bool, str]:
    """Section 7.5 tier 1: numbers must be in the page, dates must parse inside 2020..now+3y."""
    page_nums = numbers_in(page_text)
    a = row.get("amount")
    if a is not None and a >= 10:  # the spec checks numbers of 2+ digits
        figure = str(int(a)) if float(a).is_integer() else str(a)
        if figure not in page_nums and not any(_scaled_match(a, page_text)):
            return False, f"amount {a} not in source"
    if row.get("deadline"):
        try:
            d = datetime.strptime(row["deadline"], "%Y-%m-%d").date()
        except ValueError:
            return False, "deadline does not parse"
        if not (
            datetime(2020, 1, 1, tzinfo=timezone.utc).date()
            <= d
            <= (datetime.now(IST) + timedelta(days=3 * 365)).date()
        ):
            return False, f"deadline {d} out of range"
        if str(d.year) not in numbers_in(page_text):
            return False, f"deadline year {d.year} not in source"
    return True, "ok"


def _scaled_match(amount: float, text: str) -> Iterator[bool]:
    """'$50k', '2.5M', 'Rs 10 lakh' style amounts: accept when the scaled figure appears."""
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(k|m|mn|million|bn|billion|lakh|lac|crore|cr)\b", text, re.IGNORECASE):
        mult = {
            "k": 1e3,
            "m": 1e6,
            "mn": 1e6,
            "million": 1e6,
            "bn": 1e9,
            "billion": 1e9,
            "lakh": 1e5,
            "lac": 1e5,
            "crore": 1e7,
            "cr": 1e7,
        }[m.group(2).lower()]
        yield abs(float(m.group(1)) * mult - amount) < 0.5


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def print_status(st: dict) -> None:
    print(f"Snowball status {st['day']} (IST)")
    print(f"  frontier   {json.dumps(st['frontier'])}")
    print(f"  queries    {json.dumps(st['queries'])}")
    print(f"  tasks      {json.dumps(st['tasks'])}")
    print(
        f"  entities   {json.dumps(st['entities'])}  unreported {json.dumps(st['unreported'])}  claims {st['claims']}"
    )
    print(f"  fetches/24h {st['fetches_24h']}  repeat rate {st['repeat_rate_24h']:.2%} (target < 5%)")
    print(f"  tokens     global {st['global_tokens']['used']:,} / {st['global_tokens']['cap']:,}")
    goal = st.get("goal") or {}
    grant = goal.get("grant") or {}
    print(
        f"  goal       {goal.get('state', 'absent')}  contract {goal.get('contract_version')} "
        f"{goal.get('contract_status') or ''}  grant {grant.get('state', 'none')}  substate {goal.get('substate')}"
    )
    for role, info in (st.get("roles") or {}).items():
        print(f"    {role:<8} {info.get('status')}  model {info.get('model') or '-'}")
    print("  domain yield (Laplace new items per fetch)")
    for d in st["domain_yield"] or [{"domain": "(none yet)"}]:
        if "yield" in d:
            print(
                f"    {d['yield']:<6} {d['domain']:<40} fetches {d['fetch_count']}, new {d['new_items_total']}, "
                f"walled {d['walled_count']}, {d['status']}"
            )
        else:
            print(f"    {d['domain']}")
    print("  next topics (Novelty x Value x Due)")
    for t in st["next_topics"]:
        print(f"    {t['score']:<6} {t['topic']}{'  [private]' if t['private'] else ''}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Snowball multi-model orchestrator (one batch of topics per run)")
    ap.add_argument("--topic", metavar="SLUG", help="run one topic, even if it is not due")
    ap.add_argument("--max-topics", type=int, default=None, help="max topics per batch (default 2)")
    ap.add_argument(
        "--mode",
        choices=SnowballOrchestrator.MODES,
        default=None,
        help="extraction mode; public extract uses the configured Gemini transport",
    )
    ap.add_argument("--dry-run", action="store_true", help="simulate on an in-memory DB copy; no external calls")
    ap.add_argument("--status", action="store_true", help="frontier, token spend, domain yield")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--seed-only", action="store_true", help="seed topics and exit")
    ap.add_argument("--topics", type=Path, default=None, help="path to custom topics YAML file")
    ap.add_argument("--run-once", action="store_true", help="run one topic wave and exit with machine-readable outcome")
    ap.add_argument("--supervise", action="store_true", help="run supervisor persistent loop with bounded sleeps")
    ap.add_argument("--diagnose", action="store_true", help="run provider diagnostic availability checks")
    ap.add_argument("--live", action="store_true", help="perform live bounded completions during diagnostics")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="path to snowball.db")
    a = ap.parse_args(argv)
    cfg = load_config()

    if a.diagnose:
        diag = diagnose_providers(cfg, live=a.live)
        if a.json:
            print(json.dumps(diag, indent=1, default=str))
        else:
            print("Snowball Provider Diagnostics:")
            for p, st in diag.items():
                if p == "all_ok":
                    continue
                print(f"  {p:<8} status={st.get('status')} error={st.get('error', 'none')}")
        return 0 if diag.get("all_ok") else 1

    if a.db.exists():
        store = Store.connect_existing(a.db, readonly=a.dry_run or a.status)
    elif a.status:
        print(json.dumps({"error": "database_missing", "message": "Run migrate or goal new explicitly."}))
        return 2
    elif a.dry_run or not a.seed_only:
        store = Store.in_memory()
    else:
        store = Store.open(a.db)
    orch = SnowballOrchestrator(
        cfg, store, mode=a.mode or cfg.mode, dry=a.dry_run, topics_path=a.topics,
    )
    if a.status:
        st = orch.status()
        if a.json:
            print(json.dumps(st, indent=1, default=str))
        else:
            print_status(st)
        return 0
    if a.seed_only:
        n = orch.seed_from_yaml(topics_path=a.topics)
        if a.json:
            print(json.dumps({"seeded": n, "planned_calls": orch.router.planned}, indent=1))
        else:
            print(f"seeded {n} topics {'(dry-run in-memory)' if a.dry_run else ''}")
        return 0
    if a.supervise:
        return orch.supervise(a.max_topics or 1, slug_filter=a.topic)
    if a.dry_run:
        summary = orch.run(a.max_topics or cfg.max_topics, a.topic)
    else:
        lock_path = a.db.parent / "orchestrator.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as fh:
            try:
                _flock_nb(fh)
            except BlockingIOError:
                log("another orchestrator run holds the lock; exiting")
                return 3
            summary = orch.run(a.max_topics or cfg.max_topics, a.topic)
    if a.json:
        print(json.dumps(summary, indent=1, default=str))
    else:
        print(f"wave {summary.get('wave')} {'(dry run, nothing written)' if a.dry_run else ''}")
        for t in summary.get("topics", []):
            print(f"  {t}")
        print(f"  locks {summary.get('locks')}")
        for c in summary.get("planned_calls", []):
            print(f"  would call: {c}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
