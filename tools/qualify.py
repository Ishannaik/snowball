"""Public evidence qualification and funding evaluation for an approved goal.

Snapshots are exact immutable UTF-8 bytes. Observations stay code-checked until
an explicit reviewer keep or drop. Packages and constraint results use only
reviewed public evidence plus the frozen approved profile. Private values are
never written into public rows or caller-facing reasons.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence
from urllib.parse import urlparse


def _sibling(module_name: str):
    """Load a tools sibling by path under its canonical snowball_* name.

    sys.modules is consulted first. tools/ is never inserted into sys.path.
    """
    canonical = module_name if module_name.startswith("snowball_") else "snowball_" + module_name
    loaded = sys.modules.get(canonical)
    if loaded is not None:
        return loaded
    filename = module_name[len("snowball_"):] if module_name.startswith("snowball_") else module_name
    if not filename.endswith(".py"):
        filename = filename + ".py"
    path = Path(__file__).resolve().parent / filename
    if not path.is_file():
        raise ImportError("sibling module is not available: " + canonical)
    import importlib.util

    spec = importlib.util.spec_from_file_location(canonical, path)
    if spec is None or spec.loader is None:
        raise ImportError("sibling module cannot be loaded: " + canonical)
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    spec.loader.exec_module(module)
    return module


def _goal():
    return _sibling("goal")


# Spec field keys, plus the cost and accounting keys required to keep a stipend
# distinct from a living expense and to refuse an assumed whole-degree duration.
FIELD_KEYS = (
    "degree_level",
    "modality",
    "field_of_study",
    "intake_cycle",
    "application_deadline",
    "tuition",
    "mandatory_fees",
    "living_support_amount",
    "living_support_duration",
    "health_insurance",
    "visa_costs",
    "application_fee",
    "deposit",
    "travel_relocation",
    "proof_of_funds",
    "work_hours_required",
    "renewal_conditions",
    "gpa_conditions",
    "award_type",
    "funding_source_official",
    "eligibility_text",
    "cycle_dates",
    "program_name",
    "institution",
    "official_domain",
    "field_tags",
    "campus",
    "region",
    "intake",
    "applicability",
    "award_name",
    "living_costs",
    "program_duration",
    "tuition_coverage",
    "mandatory_fees_coverage",
    "health_insurance_coverage",
    "travel_coverage",
    "visa_coverage",
    "funding_amount",
    "stacking_policy",
    "disbursement_timing",
    "language_conditions",
    "citizenship_conditions",
    "deposit_credit",
    "refund_policy",
    "acceptance_status",
    "fx_rate",
)

COST_FIELDS = (
    "tuition",
    "mandatory_fees",
    "application_fee",
    "living_costs",
    "health_insurance",
    "visa_costs",
    "travel_relocation",
    "deposit",
)

COVERAGE_FIELDS = (
    "tuition_coverage",
    "mandatory_fees_coverage",
    "health_insurance_coverage",
    "travel_coverage",
    "visa_coverage",
)

MONEY_FIELDS = COST_FIELDS + COVERAGE_FIELDS + (
    "living_support_amount",
    "funding_amount",
    "deposit",
    "deposit_credit",
    "proof_of_funds",
    "refund_policy",
)

COMPONENT_FIELDS = {
    "tuition": ("tuition", "tuition_coverage"),
    "mandatory_fees": ("mandatory_fees", "mandatory_fees_coverage"),
    "living": ("living_costs", "living_support_amount", "funding_amount"),
    "living_amount": ("living_costs",),
    "living_duration": ("living_support_duration", "program_duration"),
    "health_insurance": ("health_insurance", "health_insurance_coverage"),
    "visa_costs": ("visa_costs", "visa_coverage"),
    "application_fee": ("application_fee",),
    "application_costs": ("application_fee",),
    "travel_relocation": ("travel_relocation", "travel_coverage"),
    "travel_costs": ("travel_relocation", "travel_coverage"),
    "deposit": ("deposit", "deposit_credit"),
    "deposits": ("deposit", "deposit_credit"),
    "proof_of_funds": ("proof_of_funds",),
    "proof_of_funds_liquidity": ("proof_of_funds",),
}

CRITICAL_PUBLIC_FIELDS = (
    "degree_level",
    "modality",
    "field_of_study",
    "intake_cycle",
    "tuition",
    "mandatory_fees",
    "living_costs",
    "living_support_amount",
    "living_support_duration",
    "program_duration",
    "health_insurance",
    "award_type",
    "funding_source_official",
    "eligibility_text",
    "cycle_dates",
    "applicability",
)

PERIODS = ("per_year", "per_semester", "per_month", "one_time", "total")
DEGREE_LEVELS = ("masters", "phd", "other")
MODALITIES = ("on_campus", "online", "hybrid")
AWARD_TYPES = (
    "guaranteed",
    "competitive",
    "conditional_ra_ta",
    "loan",
    "discount",
    "grant",
    "scholarship",
    "tuition_waiver",
    "stipend",
)
GRANT_AWARD_TYPES = ("guaranteed", "grant", "scholarship", "tuition_waiver", "stipend")
CONDITIONAL_AWARD_TYPES = ("competitive", "conditional_ra_ta")
NON_GRANT_AWARD_TYPES = ("loan", "discount")
ASSURANCE_CODE_CHECKED = "code_checked"
ASSURANCE_KEPT = "reviewed_kept"
ASSURANCE_DROPPED = "reviewed_dropped"
ASSURANCE_PENDING = "review_pending"
ASSURANCE_UNAVAILABLE = "review_unavailable"
QUALIFYING_ASSURANCE = (ASSURANCE_KEPT,)

_MONEY_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,4})?(?!\d)")
_CYCLE_RE = re.compile(r"\b(20\d{2})(?:\s*[-/]\s*(20\d{2}))?\b")
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_SEASON_CYCLE_RE = re.compile(
    r"(?i)\b(?:(fall|autumn|spring|summer|winter)\s+(20\d{2})|(20\d{2})\s*[-/]\s*(fall|autumn|spring|summer|winter))\b"
)
_RANGE_CYCLE_RE = re.compile(r"(?i)\b(20\d{2})\s*[-/]\s*(20\d{2})\b")
_INTAKE_CYCLE_RE = re.compile(
    r"(?i)\b(?:intake|cohort|admissions?|academic\s+year|cycle|class\s+of|entering)\b.{0,40}?\b(20\d{2})\b"
)
_SATELLITE_RE = re.compile(r"(?i)\b(?:satellite|branch campus|off[- ]campus)\b")
_ONLINE_RE = re.compile(r"(?i)\b(?:online|distance[- ]learning|distance[- ]education)\b")
_MULTI_CAMPUS_RE = re.compile(
    r"(?i)\b(?:multi[- ]campus|all campuses|every campus|any campus|applies to (?:all|every) campuses)\b"
)
_CYCLE_KEYS = ("cycle_label", "cycle", "intake", "intake_cycle", "cohort")
_RETRIEVAL_KEYS = ("retrieved_at", "fetched_at", "fetch_date", "retrieval_date", "as_of")
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


class QualificationError(Exception):
    """Consumer-visible qualification failure. field is the rejected boundary."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class ProgramCoverage:
    program_key: str
    public_unknown_fields: tuple
    public_contested_fields: tuple
    stale: bool


@dataclass(frozen=True)
class Coverage:
    programs: tuple


@dataclass(frozen=True)
class FundingPackage:
    key: str
    program_key: str
    cycle_label: str
    award_keys: tuple
    components: dict
    compatibility_basis_ids: tuple
    cash_flow: dict
    basis_ids: tuple


@dataclass(frozen=True)
class ConstraintEvaluation:
    program_key: str
    package_key: str
    constraint_key: str
    status: str
    basis: dict
    reason: str


@dataclass(frozen=True)
class CompletionStatus:
    ready: bool
    substate: str
    detail: dict


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError("value is not JSON serializable")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _dec(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise QualificationError("amount", "amount is missing")
    if isinstance(value, float):
        raise QualificationError("amount", "amount must not be a float")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise QualificationError("amount", "amount is not a decimal") from exc
    if not parsed.is_finite():
        raise QualificationError("amount", "amount is not finite")
    return parsed


def _money_str(value: Decimal) -> str:
    return format(_q2(value), "f")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


@contextmanager
def _transaction(store) -> Iterator[sqlite3.Connection]:
    """Use Store.tx. A caller already inside Store.tx is reused via savepoint."""
    tx = getattr(store, "tx", None)
    if tx is None:
        raise QualificationError("store", "store transaction boundary is unavailable")
    manager = tx()
    conn = manager.__enter__()
    try:
        yield conn
        manager.__exit__(None, None, None)
    except BaseException as exc:
        manager.__exit__(type(exc), exc, exc.__traceback__)
        raise


def _connect(store) -> sqlite3.Connection:
    raw = getattr(store, "db", None) or getattr(store, "conn", None) or getattr(store, "connection", None)
    if raw is None:
        raise QualificationError("store", "store connection is unavailable")
    return raw


def _storage_root(store) -> Path:
    root = getattr(store, "storage_root", None)
    if root is None:
        raise QualificationError("storage_root", "storage root is unavailable")
    return Path(root)


def _one(conn, sql: str, params: Sequence[Any] = ()):
    return conn.execute(sql, params).fetchone()


def _rows(conn, sql: str, params: Sequence[Any] = ()):
    return list(conn.execute(sql, params).fetchall())


def _columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}


def _require_tables(conn) -> None:
    required = (
        "pages", "domains", "fetches", "programs", "source_snapshots", "observations",
        "funding_packages", "constraint_evaluations", "contradictions", "fx_rates",
        "goal_programs", "goals", "goal_contracts", "strategies",
    )
    missing = [
        name for name in required
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is None
    ]
    if missing:
        raise QualificationError("schema", "qualification requires the v4 store schema")
    snapshots = _columns(conn, "source_snapshots")
    for column in ("text_format", "encoding", "offset_kind"):
        if column not in snapshots:
            raise QualificationError("schema", "source snapshots are missing immutable text metadata")
    pages = _columns(conn, "pages")
    if "canon_url" not in pages or "domain" not in pages:
        raise QualificationError("schema", "pages require canon_url and domain")


def _public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise QualificationError("public_fetch", "public fetch requires an http or https URL")
    host = parsed.hostname.lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local") or host.endswith(".internal"):
        raise QualificationError("public_fetch", "public fetch host is not public")
    try:
        import ipaddress
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise QualificationError("public_fetch", "public fetch host must be a public name")
    if parsed.username or parsed.password:
        raise QualificationError("public_fetch", "public fetch URL must not carry credentials")
    return host


def read_snapshot(store, snapshot_id: int) -> str:
    """Return snapshot text after checking path, original bytes, hash and offsets."""
    conn = _connect(store)
    _require_tables(conn)
    row = _one(
        conn,
        """SELECT content_hash, text_path, original_raw_path, encoding, offset_kind, text_format
           FROM source_snapshots WHERE id=?""",
        (snapshot_id,),
    )
    if row is None:
        raise QualificationError("snapshot_id", "snapshot does not exist")
    content_hash, text_path, original_raw_path, encoding, offset_kind, text_format = row
    if text_format not in (None, "utf8_text") or encoding not in (None, "utf-8") or offset_kind not in (None, "unicode_codepoints"):
        raise QualificationError("snapshot", "snapshot encoding is not unicode code points")
    path = Path(text_path)
    if not path.is_file():
        raise QualificationError("snapshot", "immutable snapshot file is missing")
    data = path.read_bytes()
    if _sha256_bytes(data) != content_hash:
        raise QualificationError("snapshot", "snapshot hash does not match immutable bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QualificationError("snapshot", "snapshot bytes are not UTF-8") from exc
    if text.encode("utf-8") != data:
        raise QualificationError("snapshot", "snapshot text is not stable UTF-8")
    if original_raw_path:
        original = Path(original_raw_path)
        if not original.is_file():
            raise QualificationError("snapshot", "original raw bytes are missing")
        raw = original.read_bytes()
        meta = _one(conn, "SELECT applicability_json FROM source_snapshots WHERE id=?", (snapshot_id,))
        try:
            applicability = json.loads(meta[0] or "{}")
        except json.JSONDecodeError as exc:
            raise QualificationError("snapshot", "snapshot applicability is malformed") from exc
        original_sha = applicability.get("original_sha256") if isinstance(applicability, dict) else None
        if original_sha and original_sha != _sha256_bytes(raw):
            raise QualificationError("snapshot", "original raw hash does not match")
        span = applicability.get("text_span") if isinstance(applicability, dict) else None
        if isinstance(span, dict):
            start = span.get("byte_start")
            end = span.get("byte_end")
            if not isinstance(start, int) or not isinstance(end, int) or raw[start:end] != data:
                raise QualificationError("snapshot", "text span does not match original bytes")
    return text


def record_snapshot(
    store,
    page_id: int,
    text: str,
    fetch_id: Optional[int] = None,
    *,
    public_fetch: bool = False,
    source_cycle_label: Optional[str] = None,
    applicability: Optional[Mapping[str, Any]] = None,
    original_raw_path: Optional[str] = None,
    original_bytes: Optional[bytes] = None,
    text_span: Optional[Mapping[str, int]] = None,
) -> int:
    """Store exact UTF-8 bytes at storage_root/raw/snapshots/<hash>.txt.

    A changed hash preserves the old file. It does not retire observations
    until a later code-checked extract names the same field and the new
    snapshot's declared cycle is the same or newer. Cycle and applicability
    come only from declared intake language in this text, never from the
    retrieval time or the intake the caller requested.
    """
    if not isinstance(text, str):
        raise QualificationError("text", "snapshot text must be a string")
    data = text.encode("utf-8")
    if data.decode("utf-8") != text:
        raise QualificationError("text", "snapshot text is not stable UTF-8")
    content_hash = _sha256_bytes(data)
    root = _storage_root(store)
    directory = root / "raw" / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    final_path = directory / (content_hash + ".txt")
    if final_path.exists():
        if final_path.read_bytes() != data:
            raise QualificationError("snapshot", "existing snapshot bytes do not match the hash")
    else:
        temporary = directory / (content_hash + ".txt.partial")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final_path)
    if final_path.read_bytes() != data:
        raise QualificationError("snapshot", "written snapshot failed hash verification")
    stored_original = None
    if original_raw_path or original_bytes is not None:
        if original_bytes is None:
            source = Path(original_raw_path)
            if not source.is_file():
                raise QualificationError("original_raw_path", "original raw bytes are missing")
            original_bytes = source.read_bytes()
        if not isinstance(original_bytes, (bytes, bytearray)):
            raise QualificationError("original_raw_path", "original raw bytes are required")
        original_bytes = bytes(original_bytes)
        original_hash = _sha256_bytes(original_bytes)
        raw_dir = root / "raw" / "original"
        raw_dir.mkdir(parents=True, exist_ok=True)
        stored_original = raw_dir / (original_hash + ".bin")
        if stored_original.exists() and stored_original.read_bytes() != original_bytes:
            raise QualificationError("original_raw_path", "existing original bytes do not match")
        if not stored_original.exists():
            temporary = raw_dir / (original_hash + ".bin.partial")
            with open(temporary, "wb") as handle:
                handle.write(original_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, stored_original)
        if text_span is not None:
            start = text_span.get("byte_start")
            end = text_span.get("byte_end")
            if not isinstance(start, int) or not isinstance(end, int) or original_bytes[start:end] != data:
                raise QualificationError("text_span", "text span does not match original bytes")
    supported_cycle = _cycle_supported(text, source_cycle_label)
    supported_applicability = _applicability_supported(text, applicability)
    if stored_original is not None:
        supported_applicability = dict(supported_applicability)
        supported_applicability["original_sha256"] = _sha256_bytes(original_bytes)
        if text_span is not None:
            supported_applicability["text_span"] = {
                "byte_start": int(text_span["byte_start"]),
                "byte_end": int(text_span["byte_end"]),
            }
    with _transaction(store) as conn:
        _require_tables(conn)
        page = _one(conn, "SELECT id, canon_url, domain FROM pages WHERE id=?", (page_id,))
        if page is None:
            raise QualificationError("page_id", "page does not exist")
        host = _public_url(page[1])
        if (page[2] or "").lower() != host:
            raise QualificationError("page", "page domain does not match canon_url")
        visibility = "private"
        provenance = "unknown"
        if public_fetch:
            if fetch_id is None:
                raise QualificationError("public_fetch", "public fetch requires a fetch id")
            fetch = _one(
                conn,
                "SELECT id, page_id, outcome FROM fetches WHERE id=?",
                (fetch_id,),
            )
            if fetch is None or fetch[1] != page_id or fetch[2] != "ok":
                raise QualificationError("public_fetch", "fetch is not a successful fetch of this page")
            visibility = "public"
            provenance = "public_fetch"
        now = _now()
        existing = _one(
            conn,
            "SELECT id FROM source_snapshots WHERE page_id=? AND content_hash=? ORDER BY id",
            (page_id, content_hash),
        )
        if existing is not None:
            if stored_original is not None:
                conn.execute(
                    "UPDATE source_snapshots SET original_raw_path=COALESCE(original_raw_path, ?) WHERE id=?",
                    (str(stored_original), existing[0]),
                )
            if supported_cycle:
                conn.execute(
                    """UPDATE source_snapshots
                       SET source_cycle_label=COALESCE(source_cycle_label, ?)
                       WHERE id=?""",
                    (supported_cycle, existing[0]),
                )
            if supported_applicability:
                current_row = _one(
                    conn, "SELECT applicability_json FROM source_snapshots WHERE id=?", (existing[0],),
                )
                try:
                    current_applicability = json.loads(current_row[0] or "{}")
                except json.JSONDecodeError as exc:
                    raise QualificationError("snapshot", "snapshot applicability is malformed") from exc
                if not isinstance(current_applicability, dict):
                    current_applicability = {}
                for key, value in supported_applicability.items():
                    current_applicability.setdefault(key, value)
                conn.execute(
                    "UPDATE source_snapshots SET applicability_json=? WHERE id=?",
                    (canonical_json(current_applicability), existing[0]),
                )
            return int(existing[0])
        conn.execute(
            """INSERT INTO source_snapshots (
                   page_id, fetch_id, content_hash, retrieved_at, text_path, original_raw_path,
                   visibility, provenance, source_cycle_label, applicability_json,
                   text_format, encoding, offset_kind
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'utf8_text', 'utf-8', 'unicode_codepoints')""",
            (
                page_id,
                fetch_id,
                content_hash,
                now,
                str(final_path),
                str(stored_original) if stored_original else None,
                visibility,
                provenance,
                supported_cycle,
                canonical_json(supported_applicability),
            ),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _declared_cycle_years(text: str) -> set:
    """Years that the source declares as an intake, cohort, or academic cycle.

    A bare copyright, announcement, or retrieval year is not a cycle.
    """
    years = set()
    for pattern in (_SEASON_CYCLE_RE, _RANGE_CYCLE_RE, _INTAKE_CYCLE_RE):
        for match in pattern.finditer(text or ""):
            for group in match.groups():
                if group and re.fullmatch(r"20\d{2}", group):
                    years.add(int(group))
    return years


def _cycle_rank(label: Optional[str]):
    if not isinstance(label, str):
        return None
    years = re.findall(r"20\d{2}", label)
    if not years:
        return None
    season = 0
    for name, rank in (("spring", 1), ("summer", 2), ("fall", 3), ("autumn", 3), ("winter", 4)):
        if re.search(rf"\b{name}\b", label, re.IGNORECASE):
            season = rank
            break
    return (int(years[0]), season)


def _cycle_same_or_newer(new_label: Optional[str], old_label: Optional[str]) -> bool:
    """True only when both cycles are declared and the new one is not older.

    Missing cycles never compare as newer by retrieval time.
    """
    new_rank = _cycle_rank(new_label)
    old_rank = _cycle_rank(old_label)
    if new_rank is None or old_rank is None:
        return False
    if new_rank[0] != old_rank[0]:
        return new_rank[0] > old_rank[0]
    if new_rank[1] == 0 or old_rank[1] == 0:
        return True
    return new_rank[1] >= old_rank[1]


def _cycle_supported(text: str, label: Optional[str]) -> Optional[str]:
    if label is None:
        return None
    if not isinstance(label, str) or not label.strip():
        raise QualificationError("source_cycle_label", "cycle label is empty")
    years = re.findall(r"20\d{2}", label)
    declared = _declared_cycle_years(text)
    if not years or any(int(year) not in declared for year in years):
        return None
    if label not in text:
        return None
    return label


def _quote_declares_cycle(quote: str, label: str) -> bool:
    if not isinstance(label, str) or not label or label not in (quote or ""):
        return False
    years = re.findall(r"20\d{2}", label)
    declared = _declared_cycle_years(quote)
    return bool(years) and all(int(year) in declared for year in years)


def _applicability_supported(text: str, applicability: Optional[Mapping[str, Any]]) -> dict:
    if applicability is None:
        return {}
    if not isinstance(applicability, Mapping):
        raise QualificationError("applicability", "applicability must be an object")
    cleaned = {}
    for key, value in applicability.items():
        if key in ("original_sha256", "text_span") or key in _RETRIEVAL_KEYS:
            continue
        if key in _CYCLE_KEYS:
            if isinstance(value, str) and _cycle_supported(text, value) == value:
                cleaned[key] = value
            continue
        if isinstance(value, str) and value and value in text:
            cleaned[key] = value
        elif isinstance(value, list) and value and all(isinstance(item, str) and item in text for item in value):
            cleaned[key] = list(value)
    return cleaned


def _binding_args(binding: Mapping[str, Any]) -> tuple:
    required = ("goal_id", "contract_version", "strategy_version", "epoch")
    if any(key not in binding for key in required):
        raise QualificationError("binding", "goal binding requires goal, contract, strategy and epoch")
    values = []
    for key in required:
        value = binding[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise QualificationError("binding", "goal binding values must be integers")
        values.append(value)
    return tuple(values)


def _binding_is_current(store, binding: Mapping[str, Any]) -> bool:
    """Current running or monitoring binding. A stale binding is false, never swallowed."""
    goal_id, contract_version, strategy_version, epoch = _binding_args(binding)
    goal = _goal()
    checker = getattr(goal, "binding_valid", None)
    if checker is None:
        raise QualificationError("binding", "goal binding check is unavailable")
    with _transaction(store) as conn:
        return bool(checker(conn, goal_id, contract_version, strategy_version, epoch))


def _require_program_name(program: Mapping[str, Any], text: str) -> str:
    name = program.get("name") if isinstance(program, Mapping) else None
    if not isinstance(name, str) or not name.strip() or name not in text:
        raise QualificationError("program", "program name is not verbatim in the source")
    key = program.get("key")
    if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", key)):
        raise QualificationError("program_key", "program key is not canonical")
    return name


def _canonical_program_key(program: Mapping[str, Any], observed_name: str) -> str:
    explicit = program.get("key")
    if isinstance(explicit, str) and explicit:
        return explicit
    slug = unicodedata.normalize("NFKC", observed_name).casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    if not slug:
        raise QualificationError("program_key", "program name does not yield a key")
    return slug[:128]


def _slice_utf16_units(text: str, start: int, end: int):
    """Slice by JavaScript UTF-16 code units. A split surrogate returns None."""
    index = 0
    chars = []
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        next_index = index + width
        if next_index <= start:
            index = next_index
            continue
        if index >= end:
            break
        if index < start or next_index > end:
            return None
        chars.append(char)
        index = next_index
    if index < end:
        return None
    return "".join(chars)


def _slice_quote(text: str, start: Any, end: Any, expected: Optional[str] = None) -> str:
    """Quote offsets are Python Unicode code points, not UTF-8 bytes or UTF-16 units.

    The mechanical check is snapshot_text[start:end] == quote. A non-BMP
    character such as an emoji counts as one character.
    """
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
        raise QualificationError("quote", "quote offsets must be integers")
    if start < 0 or end < start or end > len(text):
        raise QualificationError("quote", "quote offsets are outside the snapshot")
    quote = text[start:end]
    if expected is None:
        return quote
    if expected == quote:
        return quote
    encoded = text.encode("utf-8")
    if end <= len(encoded):
        try:
            if encoded[start:end].decode("utf-8") == expected:
                raise QualificationError("quote", "quote offsets are unicode code points, not UTF-8 bytes")
        except UnicodeDecodeError:
            pass
    utf16 = _slice_utf16_units(text, start, end)
    if utf16 is not None and utf16 == expected:
        raise QualificationError("quote", "quote offsets are unicode code points, not UTF-16 code units")
    raise QualificationError("quote", "quote does not match the snapshot slice")


def _numbers_in_quote(quote: str) -> set:
    found = set()
    for match in _MONEY_RE.finditer(quote.replace("\u00a0", " ")):
        raw = (match.group(1) + (match.group(2) or "")).replace(",", "")
        parsed = _dec(raw)
        found.add(format(parsed, "f"))
        found.add(format(_q2(parsed), "f"))
        found.add(str(parsed))
    return found


def _value_supported(field_key: str, value: Any, quote: str, period: Optional[str], currency: Optional[str]) -> Any:
    if field_key not in FIELD_KEYS:
        raise QualificationError("field_key", "field is not in the source whitelist")
    if value is None:
        raise QualificationError(field_key, "unknown observation value cannot be stored as a concrete value")
    if field_key == "degree_level":
        return _token_supported(field_key, value, quote, DEGREE_LEVELS)
    if field_key == "modality":
        return _token_supported(field_key, value, quote, MODALITIES, {"on_campus": ("on campus", "on-campus")})
    if field_key in ("field_tags", "field_of_study"):
        items = value if isinstance(value, list) else [value]
        if not items or any(not isinstance(item, str) or item not in quote for item in items):
            raise QualificationError(field_key, "field is not in the quote")
        return items
    if field_key == "award_type":
        return _token_supported(
            field_key,
            value,
            quote,
            AWARD_TYPES,
            {
                "conditional_ra_ta": ("conditional ra ta", "conditional ra/ta", "ra/ta", "ra or ta"),
                "tuition_waiver": ("tuition waiver",),
            },
        )
    if field_key in ("program_duration", "living_support_duration"):
        return _duration_supported(value, quote)
    if field_key == "stacking_policy":
        return _stacking_supported(value, quote)
    if field_key == "disbursement_timing":
        return _timing_supported(value, quote)
    if field_key in ("language_conditions", "citizenship_conditions", "gpa_conditions", "renewal_conditions", "eligibility_text"):
        return _condition_supported(field_key, value, quote)
    if field_key == "acceptance_status":
        return _acceptance_supported(value, quote)
    if field_key == "fx_rate":
        return _fx_supported(value, quote)
    if field_key == "applicability":
        supported = _applicability_supported(quote, value if isinstance(value, Mapping) else {"label": value})
        if not supported:
            raise QualificationError(field_key, "applicability is not in the quote")
        return supported
    if field_key in ("intake", "intake_cycle", "cycle_dates", "application_deadline"):
        if not isinstance(value, str) or value not in quote:
            raise QualificationError(field_key, "cycle is not in the quote")
        return value
    if field_key == "work_hours_required":
        if isinstance(value, bool) or not isinstance(value, int) or str(value) not in quote:
            raise QualificationError(field_key, "work hours are not in the quote")
        return value
    if field_key in MONEY_FIELDS or _looks_like_money(value):
        return _money_supported(field_key, value, quote, period, currency)
    if isinstance(value, str):
        if value not in quote:
            raise QualificationError(field_key, "value is not in the quote")
        return value
    if isinstance(value, bool):
        token = "yes" if value else "no"
        if token not in quote.lower() and str(value).lower() not in quote.lower():
            raise QualificationError(field_key, "boolean is not in the quote")
        return value
    raise QualificationError(field_key, "value shape is not allowed")


def _token_supported(field_key: str, value: Any, quote: str, allowed: Sequence[str], aliases: Optional[Mapping[str, Sequence[str]]] = None) -> str:
    if value not in allowed:
        raise QualificationError(field_key, "value is not an allowed token")
    lowered = quote.lower()
    candidates = [value, value.replace("_", " "), value.replace("_", "-")]
    candidates.extend((aliases or {}).get(value, ()))
    if not any(item in lowered for item in candidates):
        raise QualificationError(field_key, "value is not in the quote")
    return value


def _looks_like_money(value: Any) -> bool:
    return isinstance(value, Mapping) and "amount" in value and "currency" in value


def _money_supported(field_key: str, value: Any, quote: str, period: Optional[str], currency: Optional[str]) -> dict:
    if not isinstance(value, Mapping):
        raise QualificationError(field_key, "money value must be an object")
    amount = _dec(value.get("amount"))
    if amount < 0:
        raise QualificationError(field_key, "money amount cannot be negative")
    code = value.get("currency") or currency
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code) or code not in quote:
        raise QualificationError(field_key, "currency is not in the quote")
    rendered = {format(amount, "f"), format(_q2(amount), "f"), str(amount.normalize())}
    if not rendered.intersection(_numbers_in_quote(quote)):
        raise QualificationError(field_key, "amount is not in the quote")
    chosen_period = value.get("period") or period
    if chosen_period is not None and (chosen_period not in PERIODS or not _period_in_quote(chosen_period, quote)):
        raise QualificationError(field_key, "period is not in the quote")
    cleaned = {"amount": format(amount, "f"), "currency": code}
    if chosen_period is not None:
        cleaned["period"] = chosen_period
    for key in ("duration_months", "semesters", "credited_to", "component", "date", "optional"):
        if key not in value:
            continue
        item = value[key]
        if key in ("duration_months", "semesters"):
            if isinstance(item, bool) or not isinstance(item, int) or str(item) not in quote:
                raise QualificationError(field_key, key + " is not in the quote")
        elif key == "optional":
            if not isinstance(item, bool):
                raise QualificationError(field_key, "optional must be boolean")
        elif not isinstance(item, str) or item not in quote:
            raise QualificationError(field_key, key + " is not in the quote")
        cleaned[key] = item
    return cleaned


def _period_in_quote(period: str, quote: str) -> bool:
    lowered = quote.lower()
    aliases = {
        "per_year": ("per year", "a year", "annual", "annually", "/year"),
        "per_semester": ("per semester", "a semester", "/semester"),
        "per_month": ("per month", "a month", "monthly", "/month"),
        "one_time": ("one time", "one-time", "once"),
        "total": ("total", "whole degree", "for the degree"),
    }
    return any(token in lowered for token in aliases.get(period, ()))


def _duration_supported(value: Any, quote: str) -> dict:
    if not isinstance(value, Mapping):
        raise QualificationError("program_duration", "duration must be an object")
    cleaned = {}
    months = value.get("duration_months")
    semesters = value.get("semesters")
    if months is None and semesters is None:
        raise QualificationError("program_duration", "duration is missing")
    if months is not None:
        if isinstance(months, bool) or not isinstance(months, int) or months <= 0:
            raise QualificationError("program_duration", "duration months are invalid")
        if not re.search(rf"\b{months}\s+months?\b", quote, re.IGNORECASE):
            raise QualificationError("program_duration", "duration months are not in the quote")
        cleaned["duration_months"] = months
    if semesters is not None:
        if isinstance(semesters, bool) or not isinstance(semesters, int) or semesters <= 0:
            raise QualificationError("program_duration", "semester count is invalid")
        if not re.search(rf"\b{semesters}\s+semesters?\b", quote, re.IGNORECASE):
            raise QualificationError("program_duration", "semester count is not in the quote")
        cleaned["semesters"] = semesters
    return cleaned


def _stacking_supported(value: Any, quote: str) -> dict:
    if not isinstance(value, Mapping) or value.get("relation") not in ("compatible", "forbidden"):
        raise QualificationError("stacking_policy", "stacking relation is not allowed")
    keys = value.get("award_keys")
    if not isinstance(keys, list) or len(keys) < 2 or any(not isinstance(item, str) or item not in quote for item in keys):
        raise QualificationError("stacking_policy", "stacking award keys are not in the quote")
    relation = value["relation"]
    lowered = quote.lower()
    if relation == "compatible" and not any(token in lowered for token in ("may be combined", "can be combined", "compatible", "may stack")):
        raise QualificationError("stacking_policy", "compatibility is not in the quote")
    if relation == "forbidden" and not any(token in lowered for token in ("cannot be combined", "may not be combined", "not compatible", "cannot stack")):
        raise QualificationError("stacking_policy", "prohibition is not in the quote")
    return {"relation": relation, "award_keys": list(keys)}


def _timing_supported(value: Any, quote: str) -> dict:
    if not isinstance(value, Mapping) or not isinstance(value.get("events"), list):
        raise QualificationError("disbursement_timing", "timing events are required")
    events = []
    for event in value["events"]:
        if not isinstance(event, Mapping):
            raise QualificationError("disbursement_timing", "timing event must be an object")
        cleaned = _money_supported("disbursement_timing", event, quote, event.get("period") or "one_time", event.get("currency"))
        kind = event.get("kind")
        if kind not in ("cost", "deposit", "disbursement", "refund"):
            raise QualificationError("disbursement_timing", "timing kind is not allowed")
        if kind.replace("_", " ") not in quote.lower() and kind not in quote.lower():
            raise QualificationError("disbursement_timing", "timing kind is not in the quote")
        when = event.get("date")
        if not isinstance(when, str) or when not in quote or not _DATE_RE.fullmatch(when):
            raise QualificationError("disbursement_timing", "timing date is not in the quote")
        cleaned["kind"] = kind
        cleaned["date"] = when
        events.append(cleaned)
    return {"events": events}


def _condition_supported(field_key: str, value: Any, quote: str) -> dict:
    if isinstance(value, str):
        if value not in quote:
            raise QualificationError(field_key, "condition is not in the quote")
        return {"text": value}
    if not isinstance(value, Mapping):
        raise QualificationError(field_key, "condition must be structured")
    cleaned = {}
    for key, item in value.items():
        if isinstance(item, str):
            if item not in quote:
                raise QualificationError(field_key, "condition is not in the quote")
            cleaned[key] = item
        elif isinstance(item, list) and item and all(isinstance(part, str) and part in quote for part in item):
            cleaned[key] = list(item)
        elif key in ("minimum", "value") and not isinstance(item, float):
            parsed = _dec(item)
            if format(parsed, "f") not in _numbers_in_quote(quote) and str(parsed) not in _numbers_in_quote(quote):
                raise QualificationError(field_key, "condition number is not in the quote")
            cleaned[key] = format(parsed, "f")
        else:
            raise QualificationError(field_key, "condition shape is not in the quote")
    if not cleaned:
        raise QualificationError(field_key, "condition is empty")
    return cleaned


def _acceptance_supported(value: Any, quote: str) -> dict:
    if not isinstance(value, Mapping):
        raise QualificationError("acceptance_status", "acceptance must be an object")
    status = value.get("status")
    allowed = {
        "rejected": ("declined", "rejected", "not awarded"),
        "accepted": ("awarded", "accepted"),
        "competitive": ("competitive",),
        "conditional": ("conditional",),
        "accepted_specific_condition": ("accepted if", "conditional on"),
    }
    if status not in allowed or not any(token in quote.lower() for token in allowed[status]):
        raise QualificationError("acceptance_status", "acceptance status is not in the quote")
    cleaned = {"status": status}
    award_key = value.get("award_key")
    if award_key is not None:
        if not isinstance(award_key, str) or award_key not in quote:
            raise QualificationError("acceptance_status", "acceptance award is not in the quote")
        cleaned["award_key"] = award_key
    condition = value.get("condition")
    if condition is not None:
        if not isinstance(condition, str) or condition not in quote:
            raise QualificationError("acceptance_status", "acceptance condition is not in the quote")
        cleaned["condition"] = condition
    return cleaned


def _fx_supported(value: Any, quote: str) -> dict:
    if not isinstance(value, Mapping):
        raise QualificationError("fx_rate", "fx rate must be an object")
    base = value.get("base")
    quote_code = value.get("quote")
    as_of = value.get("as_of")
    rate = _dec(value.get("rate"))
    if not all(isinstance(item, str) and re.fullmatch(r"[A-Z]{3}", item) for item in (base, quote_code)):
        raise QualificationError("fx_rate", "fx currencies are invalid")
    if base not in quote or quote_code not in quote:
        raise QualificationError("fx_rate", "fx currencies are not in the quote")
    if format(rate, "f") not in _numbers_in_quote(quote):
        raise QualificationError("fx_rate", "fx rate is not in the quote")
    if not isinstance(as_of, str) or as_of not in quote or not _DATE_RE.fullmatch(as_of):
        raise QualificationError("fx_rate", "fx date is not in the quote")
    return {"base": base, "quote": quote_code, "rate": format(rate, "f"), "as_of": as_of}


def _subject_supported(subject: Mapping[str, Any], quote: str, program_key: str, text: str) -> dict:
    if not isinstance(subject, Mapping):
        raise QualificationError("subject", "subject must be an object")
    if subject.get("program_key") not in (None, program_key):
        raise QualificationError("subject", "subject program_key must equal the observed program")
    cleaned = {"program_key": program_key}
    for key in ("award_key", "campus", "cycle_label"):
        value = subject.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value or value not in quote:
            raise QualificationError("subject", key + " is not in the observation quote")
        cleaned[key] = value
    if "award_key" in cleaned and cleaned["award_key"] not in text:
        raise QualificationError("subject", "award key is not in the source")
    return cleaned


def _upsert_program(conn, program_key: str, program: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], now: int) -> None:
    degree = _unique_observed(observations, "degree_level")
    modality = _unique_observed(observations, "modality")
    tags = _unique_observed(observations, "field_tags") or _unique_observed(observations, "field_of_study")
    institution = program.get("institution") if isinstance(program.get("institution"), str) else None
    country = program.get("country") if isinstance(program.get("country"), str) else None
    domain = program.get("official_domain") if isinstance(program.get("official_domain"), str) else None
    existing = _one(conn, "SELECT provenance FROM programs WHERE key=?", (program_key,))
    if existing is not None and existing[0] == "lead_legacy":
        raise QualificationError("program", "legacy lead cannot be promoted by qualification")
    conn.execute(
        """INSERT INTO programs (
               key, name, institution, country, official_domain, degree_level, modality,
               field_tags_json, provenance, first_seen_at, last_seen_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'observed', ?, ?)
           ON CONFLICT(key) DO UPDATE SET
               name=excluded.name,
               institution=COALESCE(excluded.institution, programs.institution),
               country=COALESCE(excluded.country, programs.country),
               official_domain=COALESCE(excluded.official_domain, programs.official_domain),
               degree_level=COALESCE(excluded.degree_level, programs.degree_level),
               modality=COALESCE(excluded.modality, programs.modality),
               field_tags_json=excluded.field_tags_json,
               last_seen_at=excluded.last_seen_at
           WHERE programs.provenance='observed'""",
        (
            program_key,
            program["name"],
            institution,
            country,
            domain,
            degree if isinstance(degree, str) else None,
            modality if isinstance(modality, str) else None,
            canonical_json(tags or []),
            now,
            now,
        ),
    )


def _unique_observed(observations: Sequence[Mapping[str, Any]], field_key: str):
    values = [item["value"] for item in observations if item["field_key"] == field_key]
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if len(unique) == 1:
        return unique[0]
    return None


def record_observations(
    store,
    snapshot_id: int,
    program: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    binding: Optional[Mapping[str, Any]],
    extractor_model: str,
    prompt_version: str,
) -> list:
    """Validate quote-bound observations and store them as code-checked.

    A stale binding keeps the raw snapshot but does not associate the current
    goal. The requested intake is never copied onto the observation.
    """
    if not isinstance(extractor_model, str) or not extractor_model.strip():
        raise QualificationError("extractor_model", "extractor model is required")
    if not isinstance(prompt_version, str) or not prompt_version.strip():
        raise QualificationError("prompt_version", "prompt version is required")
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)) or not observations:
        raise QualificationError("observations", "observations must be a nonempty list")
    text = read_snapshot(store, snapshot_id)
    _require_program_name(program, text)
    if not isinstance(binding, Mapping) or not _binding_is_current(store, binding):
        raise QualificationError("binding", "goal binding is not current")
    prepared = []
    program_key = _canonical_program_key(program, program["name"])
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise QualificationError("observation", "observation must be an object")
        field_key = observation.get("field_key")
        if field_key not in FIELD_KEYS:
            raise QualificationError("field_key", "field is not in the source whitelist")
        quote = _slice_quote(
            text,
            observation.get("quote_start"),
            observation.get("quote_end"),
            observation.get("quote"),
        )
        if not quote.strip():
            raise QualificationError("quote", "quote does not match the snapshot slice")
        period = observation.get("period")
        if period is not None and period not in PERIODS:
            raise QualificationError("period", "period is not allowed")
        currency = observation.get("currency")
        if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
            raise QualificationError("currency", "currency must be an ISO code")
        subject = _subject_supported(observation.get("subject") or {}, quote, program_key, text)
        cleaned = _value_supported(field_key, observation.get("value"), quote, period, currency)
        if isinstance(cleaned, dict):
            period = cleaned.get("period", period)
            currency = cleaned.get("currency", currency)
        as_of = observation.get("as_of")
        if as_of is not None and (not isinstance(as_of, str) or as_of not in quote):
            raise QualificationError("as_of", "as_of is not in the quote")
        conditions = observation.get("conditions")
        rendered_conditions = None
        if conditions is not None:
            if not _structured_in_quote(conditions, quote):
                raise QualificationError("conditions", "conditions are not in the quote")
            rendered_conditions = canonical_json(conditions)
        prepared.append({
            "field_key": field_key,
            "value": cleaned,
            "quote": quote,
            "quote_start": observation["quote_start"],
            "quote_end": observation["quote_end"],
            "subject": subject,
            "period": period,
            "currency": currency,
            "conditions": rendered_conditions,
            "as_of": as_of,
            "program_key": program_key,
        })
    now = _now()
    ids = []
    with _transaction(store) as conn:
        _require_tables(conn)
        snap = _one(
            conn,
            "SELECT page_id, visibility, provenance, superseded_by FROM source_snapshots WHERE id=?",
            (snapshot_id,),
        )
        if snap is None or snap[1] != "public" or snap[2] != "public_fetch" or snap[3] is not None:
            raise QualificationError("snapshot", "observations require a current public_fetch snapshot")
        _upsert_program(conn, program_key, program, prepared, now)
        goal_id, contract_version, strategy_version, epoch = _binding_args(binding)
        task_id = binding.get("task_id")
        if task_id is not None and (isinstance(task_id, bool) or not isinstance(task_id, int)):
            raise QualificationError("binding", "task id must be an integer")
        for item in prepared:
            conn.execute(
                """INSERT INTO observations (
                       snapshot_id, page_id, program_key, field_key, value_json, subject_json,
                       currency, period, as_of, conditions_json, quote, quote_start, quote_end,
                       extractor_model, prompt_version, assurance, task_id, goal_id,
                       contract_version, strategy_version, epoch, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id, snap[0], program_key, item["field_key"], canonical_json(item["value"]),
                    canonical_json(item["subject"]), item["currency"], item["period"], item["as_of"],
                    item["conditions"], item["quote"], item["quote_start"], item["quote_end"],
                    extractor_model, prompt_version, ASSURANCE_CODE_CHECKED, task_id, goal_id,
                    contract_version, strategy_version, epoch, now,
                ),
            )
            ids.append(int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
        role = binding.get("role") if binding.get("role") in ("candidate", "alternative") else "candidate"
        conn.execute(
            """INSERT OR IGNORE INTO goal_programs (
                   goal_id, contract_version, program_key, role, visibility
               ) VALUES (?, ?, ?, ?, 'public')""",
            (goal_id, contract_version, program_key, role),
        )
        page_goal = _one(
            conn,
            "SELECT 1 FROM goal_pages WHERE goal_id=? AND contract_version=? AND page_id=?",
            (goal_id, contract_version, snap[0]),
        )
        if page_goal is None:
            conn.execute(
                """INSERT INTO goal_pages (
                       goal_id, contract_version, page_id, depth, origin, visibility, queued_at, visited_at
                   ) VALUES (?, ?, ?, 0, 'gap', 'public', ?, ?)""",
                (goal_id, contract_version, snap[0], now, now),
            )
        for item, obs_id in zip(prepared, ids):
            if item["field_key"] != "fx_rate":
                continue
            fx = item["value"]
            conn.execute(
                """INSERT INTO fx_rates (base, quote, rate_decimal, as_of, source_page_id)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(base, quote, as_of) DO UPDATE SET
                       rate_decimal=excluded.rate_decimal,
                       source_page_id=excluded.source_page_id""",
                (fx["base"], fx["quote"], fx["rate"], fx["as_of"], snap[0]),
            )
        _supersede_checked_fields(conn, snapshot_id, prepared, now)
    return ids


def _structured_in_quote(value: Any, quote: str) -> bool:
    if isinstance(value, str):
        return value in quote
    if isinstance(value, bool):
        return ("yes" if value else "no") in quote.lower()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value) in quote
    if isinstance(value, Decimal):
        return format(value, "f") in quote
    if isinstance(value, Mapping):
        return bool(value) and all(_structured_in_quote(item, quote) for item in value.values())
    if isinstance(value, list):
        return bool(value) and all(_structured_in_quote(item, quote) for item in value)
    return False


def record_reviews(
    store,
    observation_ids: Sequence[int],
    verdicts: Any,
    review_model: Optional[str] = None,
    unavailable: bool = False,
) -> None:
    """Apply one explicit keep or drop to each named observation.

    A missing verdict stays pending. A missing reviewer or a malformed reply
    stays unavailable. Mechanical quote checks never count as a review.
    """
    if not isinstance(observation_ids, Sequence) or isinstance(observation_ids, (str, bytes)) or not observation_ids:
        raise QualificationError("observation_ids", "observation ids must be a nonempty list")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in observation_ids):
        raise QualificationError("observation_ids", "observation id must be an integer")
    if len(set(observation_ids)) != len(observation_ids):
        raise QualificationError("observation_ids", "observation ids must be unique")
    parsed = {}
    if unavailable:
        if verdicts not in (None, [], {}):
            raise QualificationError("verdicts", "an unavailable review cannot carry verdicts")
    elif not isinstance(review_model, str) or not review_model.strip():
        unavailable = True
        if verdicts not in (None, [], {}):
            raise QualificationError("review_model", "a reviewer identity is required")
    else:
        parsed = _parse_verdicts(observation_ids, verdicts)
    now = _now()
    with _transaction(store) as conn:
        _require_tables(conn)
        for obs_id in observation_ids:
            before = _one(
                conn,
                """SELECT field_key, quote, quote_start, quote_end, value_json, assurance, snapshot_id
                   FROM observations WHERE id=?""",
                (obs_id,),
            )
            if before is None:
                raise QualificationError("observation_id", "observation does not exist")
            snapped = read_snapshot(store, int(before[6]))
            if (
                isinstance(before[2], bool)
                or isinstance(before[3], bool)
                or not isinstance(before[2], int)
                or not isinstance(before[3], int)
                or snapped[before[2]:before[3]] != before[1]
            ):
                raise QualificationError("quote", "snapshot text slice does not equal the quote")
            if before[0] not in FIELD_KEYS:
                assurance, reason = ASSURANCE_DROPPED, "unsupported_field"
            elif unavailable:
                assurance, reason = ASSURANCE_UNAVAILABLE, "review_unavailable"
            elif obs_id not in parsed:
                assurance, reason = ASSURANCE_PENDING, "review_pending"
            else:
                decision, reason = parsed[obs_id]
                if decision == "keep" and before[0] not in FIELD_KEYS:
                    raise QualificationError("field_key", "unsupported field cannot be kept")
                assurance = ASSURANCE_KEPT if decision == "keep" else ASSURANCE_DROPPED
            conn.execute(
                """UPDATE observations
                   SET assurance=?, review_model=?, review_reason=?
                   WHERE id=?""",
                (assurance, review_model if not unavailable else None, reason, obs_id),
            )
            after = _one(
                conn,
                "SELECT quote, quote_start, quote_end, value_json FROM observations WHERE id=?",
                (obs_id,),
            )
            if (after[0], after[1], after[2], after[3]) != (before[1], before[2], before[3], before[4]):
                raise QualificationError("quote", "review changed immutable observation content")
            if assurance == ASSURANCE_DROPPED:
                conn.execute(
                    """UPDATE constraint_evaluations
                       SET stale_for_goal=1
                       WHERE package_key IN (
                           SELECT key FROM funding_packages WHERE basis_observation_ids_json LIKE ?
                       )""",
                    ('%' + str(obs_id) + '%',),
                )
        _ = now


def _parse_verdicts(observation_ids: Sequence[int], verdicts: Any) -> dict:
    parsed = {}
    if isinstance(verdicts, Mapping):
        if len(verdicts) > len(observation_ids):
            raise QualificationError("verdicts", "too many verdicts")
        for key, verdict in verdicts.items():
            if isinstance(key, str) and key.isdigit():
                key = int(key)
            if isinstance(key, bool) or not isinstance(key, int) or key not in observation_ids:
                raise QualificationError("verdicts", "verdict key does not match an observation")
            parsed[key] = _parse_verdict(verdict)
        return parsed
    if isinstance(verdicts, Sequence) and not isinstance(verdicts, (str, bytes)):
        if len(verdicts) > len(observation_ids):
            raise QualificationError("verdicts", "too many verdicts")
        for index, verdict in enumerate(verdicts):
            if verdict is None:
                continue
            parsed[observation_ids[index]] = _parse_verdict(verdict)
        return parsed
    raise QualificationError("verdicts", "verdicts are malformed")


def _parse_verdict(verdict: Any) -> tuple:
    if isinstance(verdict, str):
        token = verdict.strip().lower()
        if token in ("keep", "drop"):
            return token, None
        raise QualificationError("verdicts", "verdict must be keep or drop")
    if isinstance(verdict, Mapping):
        decision = str(verdict.get("decision") or verdict.get("verdict") or "").lower()
        if decision not in ("keep", "drop"):
            raise QualificationError("verdicts", "verdict must be keep or drop")
        reason = verdict.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise QualificationError("verdicts", "verdict reason must be text")
        return decision, reason
    raise QualificationError("verdicts", "verdict is malformed")


def _load_observations(conn, program_key: str) -> list:
    rows = _rows(
        conn,
        """SELECT o.id, o.field_key, o.value_json, o.subject_json, o.currency, o.period, o.as_of,
                  o.quote, o.snapshot_id, o.assurance, o.invalidated_at, s.visibility, s.provenance,
                  s.superseded_by, s.source_cycle_label, s.applicability_json, s.retrieved_at,
                  p.provenance
           FROM observations o
           JOIN source_snapshots s ON s.id=o.snapshot_id
           JOIN programs p ON p.key=o.program_key
           WHERE o.program_key=?""",
        (program_key,),
    )
    loaded = []
    for row in rows:
        loaded.append({
            "id": row[0],
            "field_key": row[1],
            "value": json.loads(row[2]),
            "subject": json.loads(row[3]),
            "currency": row[4],
            "period": row[5],
            "as_of": row[6],
            "quote": row[7],
            "snapshot_id": row[8],
            "assurance": row[9],
            "invalidated": row[10] is not None or row[13] is not None,
            "visibility": row[11],
            "provenance": row[12],
            "source_cycle_label": row[14],
            "applicability": json.loads(row[15] or "{}"),
            "retrieved_at": row[16],
            "program_provenance": row[17],
        })
    return loaded


def _qualifying(observations: Sequence[Mapping[str, Any]]) -> list:
    return [
        item for item in observations
        if item["assurance"] == ASSURANCE_KEPT
        and not item["invalidated"]
        and item["visibility"] == "public"
        and item["provenance"] == "public_fetch"
        and item["program_provenance"] == "observed"
    ]


def coverage(store, goal_id: int, contract_version: int) -> Coverage:
    """Public evidence gaps for programs associated with this goal version."""
    conn = _connect(store)
    _require_tables(conn)
    contract = _load_contract(conn, goal_id, contract_version)
    body = json.loads(contract[2])
    critical = _critical_fields(body)
    programs = _rows(
        conn,
        """SELECT program_key FROM goal_programs
           WHERE goal_id=? AND contract_version=? AND visibility='public'
           ORDER BY program_key""",
        (goal_id, contract_version),
    )
    items = []
    for row in programs:
        program_key = row[0]
        observations = _load_observations(conn, program_key)
        kept = _assigned_observations(conn, _qualifying(observations))
        present = {item["field_key"] for item in kept}
        unknown = tuple(field for field in critical if field not in present)
        contested = []
        grouped = {}
        for item in kept:
            grouped.setdefault((
                item["field_key"],
                item["subject"].get("award_key"),
                item["subject"].get("campus"),
                item["subject"].get("cycle_label"),
            ), []).append(item)
        for (field_key, _award, _campus, _cycle), group in grouped.items():
            if len({canonical_json(item["value"]) for item in group}) > 1 and not _contradiction_resolved(conn, program_key, field_key):
                contested.append(field_key)
        stale = _program_stale(conn, goal_id, contract_version, program_key, observations)
        items.append(ProgramCoverage(program_key, unknown, tuple(sorted(set(contested))), stale))
    return Coverage(tuple(items))


def _critical_fields(body: Mapping[str, Any]) -> tuple:
    """Critical fields are the interview contract list, not a wider fallback."""
    expected = tuple(field for field in _interview().CRITICAL_SOURCE_FIELDS if field in FIELD_KEYS)
    configured = _completion_body(body).get("critical_fields")
    if isinstance(configured, list) and tuple(configured) == expected:
        return expected
    return expected


def _contradiction_resolved(conn, program_key: str, field_key: str) -> bool:
    rows = _rows(
        conn,
        "SELECT winner, resolved_at, observation_a, observation_b FROM contradictions WHERE program_key=? AND field_key=?",
        (program_key, field_key),
    )
    if not rows:
        return False
    return all(row[0] is not None and row[1] is not None for row in rows)


def _program_stale(conn, goal_id: int, contract_version: int, program_key: str, observations: Sequence[Mapping[str, Any]]) -> bool:
    row = _one(
        conn,
        """SELECT 1 FROM constraint_evaluations
           WHERE goal_id=? AND contract_version=? AND program_key=? AND stale_for_goal=1 LIMIT 1""",
        (goal_id, contract_version, program_key),
    )
    if row is not None:
        return True
    return any(item["invalidated"] for item in observations)


def derive_funding_packages(store, program_key: str, cycle_label: str) -> list:
    """Build one package per evidenced award and per explicitly compatible set.

    Incompatible and unknown combinations stay separate. Pending observations
    cannot enter a package.
    """
    if not isinstance(program_key, str) or not program_key:
        raise QualificationError("program_key", "program key is required")
    if not isinstance(cycle_label, str) or not cycle_label:
        raise QualificationError("cycle_label", "cycle label is required")
    with _transaction(store) as conn:
        _require_tables(conn)
        observations = _qualifying(_load_observations(conn, program_key))
        applicable = _assigned_observations(conn, observations, cycle_label)
        _record_contradictions(conn, program_key, applicable)
        raw_costs = [
            item for item in applicable
            if item["field_key"] in COST_FIELDS and item["subject"].get("award_key") is None
        ]
        costs, contested_fields, contested_ids = _split_contested(raw_costs)
        duration = _single_or_unknown(applicable, "program_duration")
        awards = {}
        for item in applicable:
            award_key = item["subject"].get("award_key")
            if award_key:
                awards.setdefault(award_key, []).append(item)
        stacking = [item for item in applicable if item["field_key"] == "stacking_policy"]
        compatible = []
        forbidden = []
        for item in stacking:
            keys = tuple(sorted(item["value"].get("award_keys") or []))
            if item["value"].get("relation") == "compatible" and len(keys) >= 2 and all(key in awards for key in keys):
                compatible.append((keys, item["id"]))
            elif item["value"].get("relation") == "forbidden":
                forbidden.append(set(keys))
        packages = []
        base_components = _public_components(costs, duration, applicable)
        base_components["contested_fields"] = list(contested_fields)
        base_ids = [item["id"] for item in costs] + list(contested_ids)
        if isinstance(duration, Mapping) and duration.get("id"):
            base_ids.append(duration["id"])
        if base_components["costs"] or base_components["duration"] is not None or contested_fields:
            packages.append(_package_shell(program_key, cycle_label, (), base_components, (), base_ids, conn))
        for award_key, rows in sorted(awards.items()):
            components = _award_components(base_components, rows)
            basis = base_ids + [item["id"] for item in rows]
            packages.append(_package_shell(program_key, cycle_label, (award_key,), components, (), basis, conn))
        for keys, basis_id in compatible:
            if any(block.issubset(set(keys)) for block in forbidden):
                continue
            rows = [item for key in keys for item in awards[key]]
            components = _award_components(base_components, rows)
            components["combined_award_keys"] = list(keys)
            basis = base_ids + [item["id"] for item in rows] + [basis_id]
            packages.append(_package_shell(program_key, cycle_label, keys, components, (basis_id,), basis, conn))
        return packages


def _observation_applies(observation: Mapping[str, Any], cycle_label: str) -> bool:
    """Applicability is the declared cohort in the quote, never retrieved_at."""
    subject_cycle = (observation.get("subject") or {}).get("cycle_label")
    quote = observation.get("quote") or ""
    if subject_cycle != cycle_label or not _quote_declares_cycle(quote, cycle_label):
        return False
    applicability = observation.get("applicability") or {}
    if not isinstance(applicability, Mapping):
        return False
    for key in _CYCLE_KEYS:
        if key in applicability and applicability[key] != cycle_label:
            return False
    source_cycle = observation.get("source_cycle_label")
    if source_cycle and source_cycle != cycle_label and not _quote_declares_cycle(quote, cycle_label):
        return False
    return True


def _foreign_scope(quote: str) -> bool:
    return _SATELLITE_RE.search(quote or "") is not None or _ONLINE_RE.search(quote or "") is not None


def _campus_is_satellite(item: Mapping[str, Any]) -> bool:
    campus = (item.get("subject") or {}).get("campus") or ""
    if isinstance(campus, str) and _SATELLITE_RE.search(campus):
        return True
    if item.get("field_key") == "campus" and _SATELLITE_RE.search(str(item.get("value") or "")):
        return True
    return False


def _program_modalities(observations: Sequence[Mapping[str, Any]]) -> set:
    return {
        item["value"]
        for item in observations
        if item.get("field_key") == "modality" and isinstance(item.get("value"), str)
    }


def _multi_campus_cited(text: str) -> bool:
    return _MULTI_CAMPUS_RE.search(text or "") is not None


def _snapshot_text(conn, snapshot_id: Any, cache: dict) -> str:
    if snapshot_id in cache:
        return cache[snapshot_id]
    text = ""
    if isinstance(snapshot_id, int) and not isinstance(snapshot_id, bool):
        row = _one(
            conn,
            "SELECT text_path, content_hash, encoding, offset_kind FROM source_snapshots WHERE id=?",
            (snapshot_id,),
        )
        if row is not None and row[2] in (None, "utf-8") and row[3] in (None, "unicode_codepoints"):
            path = Path(row[0])
            if path.is_file():
                data = path.read_bytes()
                if _sha256_bytes(data) == row[1]:
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        text = ""
    cache[snapshot_id] = text
    return text


def _on_campus_assignment_ok(conn, item: Mapping[str, Any], peers: Sequence[Mapping[str, Any]], cache: dict) -> bool:
    """Satellite or online evidence does not fill an on-campus degree without a cited policy."""
    if "on_campus" not in _program_modalities(peers):
        return True
    quote = item.get("quote") or ""
    if not _foreign_scope(quote) and not _campus_is_satellite(item):
        return True
    text = _snapshot_text(conn, item.get("snapshot_id"), cache)
    return _multi_campus_cited(text) or _multi_campus_cited(quote)


def _assigned_observations(conn, observations: Sequence[Mapping[str, Any]], cycle_label: Optional[str] = None) -> list:
    chosen = list(observations)
    if cycle_label is not None:
        chosen = [item for item in chosen if _observation_applies(item, cycle_label)]
    cache = {}
    return [item for item in chosen if _on_campus_assignment_ok(conn, item, chosen, cache)]


def _split_contested(rows: Sequence[Mapping[str, Any]]):
    """Keep one copy of identical values. Conflicting values are not averaged or summed."""
    grouped = {}
    for item in rows:
        scope = (
            item["field_key"],
            item["subject"].get("award_key"),
            item["subject"].get("campus"),
            item["subject"].get("cycle_label"),
        )
        grouped.setdefault(scope, []).append(item)
    kept = []
    contested_fields = []
    contested_ids = []
    for (field_key, _award, _campus, _cycle), group in grouped.items():
        signatures = {canonical_json(item["value"]) for item in group}
        if len(signatures) > 1:
            contested_fields.append(field_key)
            contested_ids.extend(item["id"] for item in group)
            continue
        kept.append(group[0])
    return kept, contested_fields, contested_ids


def _supersede_checked_fields(conn, snapshot_id: int, prepared: Sequence[Mapping[str, Any]], now: int) -> None:
    """Retire same-field observations only after this snapshot passes code checks.

    The new declared cycle must be the same or newer. An older call, an unknown
    cycle, or a retrieval timestamp does not supersede a newer cohort. Fields
    the new extract did not check stay in place.
    """
    meta = _one(
        conn,
        """SELECT page_id, source_cycle_label, content_hash, offset_kind, encoding
           FROM source_snapshots WHERE id=?""",
        (snapshot_id,),
    )
    if meta is None:
        return
    page_id, new_cycle, new_hash, offset_kind, encoding = meta
    if offset_kind != "unicode_codepoints" or encoding != "utf-8" or _cycle_rank(new_cycle) is None:
        return
    retired = []
    for item in prepared:
        subject = item.get("subject") or {}
        rows = _rows(
            conn,
            """SELECT o.id, o.subject_json, s.id, s.source_cycle_label, s.content_hash
               FROM observations o
               JOIN source_snapshots s ON s.id=o.snapshot_id
               WHERE s.page_id=? AND o.program_key=? AND o.field_key=?
                 AND o.snapshot_id!=? AND o.invalidated_at IS NULL""",
            (page_id, item.get("program_key"), item.get("field_key"), snapshot_id),
        )
        for obs_id, subject_json, _old_snapshot, old_cycle, old_hash in rows:
            if old_hash == new_hash or not _cycle_same_or_newer(new_cycle, old_cycle):
                continue
            try:
                previous = json.loads(subject_json)
            except json.JSONDecodeError:
                previous = {}
            if not isinstance(previous, Mapping):
                previous = {}
            if previous.get("award_key") != subject.get("award_key"):
                continue
            if previous.get("campus") != subject.get("campus"):
                continue
            conn.execute(
                """UPDATE observations
                   SET invalidated_at=?, invalidated_reason='source_changed'
                   WHERE id=? AND invalidated_at IS NULL""",
                (now, obs_id),
            )
            retired.append(obs_id)
    if retired:
        retired_set = set(retired)
        for key, basis_json in _rows(conn, "SELECT key, basis_observation_ids_json FROM funding_packages"):
            try:
                basis_ids = json.loads(basis_json)
            except json.JSONDecodeError:
                basis_ids = []
            if any(item in retired_set for item in basis_ids):
                conn.execute(
                    "UPDATE constraint_evaluations SET stale_for_goal=1 WHERE package_key=?",
                    (key,),
                )
    prior = _rows(
        conn,
        """SELECT id, source_cycle_label, content_hash FROM source_snapshots
           WHERE page_id=? AND id!=? AND superseded_by IS NULL""",
        (page_id, snapshot_id),
    )
    for old_id, old_cycle, old_hash in prior:
        if old_hash == new_hash or not _cycle_same_or_newer(new_cycle, old_cycle):
            continue
        alive = _one(
            conn,
            "SELECT 1 FROM observations WHERE snapshot_id=? AND invalidated_at IS NULL",
            (old_id,),
        )
        if alive is None:
            conn.execute(
                "UPDATE source_snapshots SET superseded_by=? WHERE id=? AND superseded_by IS NULL",
                (snapshot_id, old_id),
            )


def _single_or_unknown(observations: Sequence[Mapping[str, Any]], field_key: str):
    rows = [item for item in observations if item["field_key"] == field_key and item["subject"].get("award_key") is None]
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        return {"unknown": True, "observation_ids": [item["id"] for item in rows]}
    return None


def _public_components(costs: Sequence[Mapping[str, Any]], duration, applicable: Sequence[Mapping[str, Any]]) -> dict:
    duration_value = None
    duration_ids = []
    if isinstance(duration, Mapping) and duration.get("unknown"):
        duration_ids = list(duration.get("observation_ids") or [])
    elif isinstance(duration, Mapping) and "value" in duration:
        duration_value = duration["value"]
        duration_ids = [duration["id"]]
    conditions = []
    for item in applicable:
        if item["subject"].get("award_key") is None and item["field_key"] in (
            "language_conditions", "citizenship_conditions", "gpa_conditions", "renewal_conditions",
            "eligibility_text", "acceptance_status", "proof_of_funds", "deposit", "deposit_credit", "refund_policy",
        ):
            conditions.append({
                "field_key": item["field_key"],
                "value": item["value"],
                "observation_id": item["id"],
                "snapshot_id": item["snapshot_id"],
            })
    cost_rows = [{
        "field_key": item["field_key"],
        "value": item["value"],
        "observation_id": item["id"],
        "snapshot_id": item["snapshot_id"],
        "period": item.get("period"),
        "campus": item["subject"].get("campus"),
        "cycle_label": item["subject"].get("cycle_label"),
    } for item in costs]
    return {
        "costs": cost_rows,
        "awards": [],
        "conditions": conditions,
        "duration": duration_value,
        "duration_observation_ids": duration_ids,
        "contested_fields": [],
        "backing_ids": [item["id"] for item in costs] + duration_ids + [item["observation_id"] for item in conditions],
    }


def _award_components(base: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict:
    components = {
        "costs": list(base.get("costs") or []),
        "awards": [],
        "conditions": list(base.get("conditions") or []),
        "duration": base.get("duration"),
        "duration_observation_ids": list(base.get("duration_observation_ids") or []),
        "backing_ids": list(base.get("backing_ids") or []),
        "contested_fields": list(base.get("contested_fields") or []),
    }
    grouped = {}
    for item in rows:
        grouped.setdefault(item["subject"].get("award_key"), []).append(item)
    for key, group in grouped.items():
        award = {
            "award_key": key,
            "observation_ids": [item["id"] for item in group],
            "snapshot_ids": sorted({item["snapshot_id"] for item in group}),
            "values": {},
            "award_type": None,
            "campus": group[0]["subject"].get("campus"),
            "cycle_label": group[0]["subject"].get("cycle_label"),
        }
        for item in group:
            if item["field_key"] == "award_type":
                if award["award_type"] not in (None, item["value"]):
                    award["award_type"] = None
                    if "award_type" not in components["contested_fields"]:
                        components["contested_fields"].append("award_type")
                else:
                    award["award_type"] = item["value"]
            elif item["field_key"] in ("disbursement_timing", "deposit_credit", "refund_policy", "acceptance_status", "renewal_conditions"):
                components["conditions"].append({
                    "field_key": item["field_key"],
                    "value": item["value"],
                    "observation_id": item["id"],
                    "snapshot_id": item["snapshot_id"],
                    "award_key": key,
                })
            else:
                existing = award["values"].get(item["field_key"])
                conflict = existing is not None and (
                    existing.get("contested")
                    or canonical_json(existing.get("value")) != canonical_json(item["value"])
                )
                if conflict:
                    award["values"][item["field_key"]] = {
                        "contested": True,
                        "observation_id": item["id"],
                        "snapshot_id": item["snapshot_id"],
                    }
                    if item["field_key"] not in components["contested_fields"]:
                        components["contested_fields"].append(item["field_key"])
                elif existing is None:
                    award["values"][item["field_key"]] = {
                        "value": item["value"],
                        "observation_id": item["id"],
                        "snapshot_id": item["snapshot_id"],
                        "period": item.get("period"),
                    }
            components["backing_ids"].append(item["id"])
        components["awards"].append(award)
    return components


def _package_key(program_key: str, cycle_label: str, award_keys: Sequence[str]) -> str:
    raw = canonical_json({"program_key": program_key, "cycle_label": cycle_label, "award_keys": list(award_keys)})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _package_shell(program_key, cycle_label, award_keys, components, compatibility_ids, basis_ids, conn) -> FundingPackage:
    key = _package_key(program_key, cycle_label, award_keys)
    rates = [
        {"base": row[0], "quote": row[1], "rate": row[2], "as_of": row[3], "source_page_id": row[4]}
        for row in _rows(conn, "SELECT base, quote, rate_decimal, as_of, source_page_id FROM fx_rates")
    ]
    flow = _package_cash_flow(components, rates)
    now = _now()
    conn.execute(
        """INSERT INTO funding_packages (
               key, program_key, cycle_label, award_keys_json, components_json,
               compatibility_basis_ids_json, cash_flow_json, basis_observation_ids_json, computed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
               components_json=excluded.components_json,
               compatibility_basis_ids_json=excluded.compatibility_basis_ids_json,
               cash_flow_json=excluded.cash_flow_json,
               basis_observation_ids_json=excluded.basis_observation_ids_json,
               computed_at=excluded.computed_at""",
        (
            key, program_key, cycle_label, canonical_json(list(award_keys)), canonical_json(components),
            canonical_json(list(compatibility_ids)), canonical_json(flow), canonical_json(sorted(set(basis_ids))), now,
        ),
    )
    return FundingPackage(
        key, program_key, cycle_label, tuple(award_keys), components,
        tuple(compatibility_ids), flow, tuple(sorted(set(basis_ids))),
    )


def _package_cash_flow(components: Mapping[str, Any], rates: Sequence[Mapping[str, Any]]) -> dict:
    events = []
    requirements = {"fx_rates": list(rates)}
    unknown = [field for field in (components.get("contested_fields") or []) if isinstance(field, str)]
    duration = components.get("duration") if isinstance(components.get("duration"), Mapping) else {}
    for cost in components.get("costs") or []:
        value = cost.get("value")
        if not isinstance(value, Mapping) or "amount" not in value:
            unknown.append(cost.get("field_key") or "cost")
            continue
        events.append(_event_from_value("cost", value, cost.get("field_key"), duration, cost.get("observation_id")))
    for award in components.get("awards") or []:
        award_type = award.get("award_type")
        for field_key, payload in (award.get("values") or {}).items():
            value = payload.get("value")
            if not isinstance(value, Mapping) or "amount" not in value:
                continue
            kind = "loan" if award_type == "loan" else "discount" if award_type == "discount" else "disbursement"
            event = _event_from_value(kind, value, field_key, duration, payload.get("observation_id"))
            event["award_type"] = award_type
            event["award_key"] = award.get("award_key")
            events.append(event)
    for condition in components.get("conditions") or []:
        field_key = condition.get("field_key")
        value = condition.get("value")
        if field_key == "disbursement_timing" and isinstance(value, Mapping):
            for event in value.get("events") or []:
                copied = _event_from_value(event.get("kind") or "disbursement", event, field_key, duration, condition.get("observation_id"))
                copied["date"] = event.get("date")
                events.append(copied)
        elif field_key == "deposit_credit" and isinstance(value, Mapping):
            event = _event_from_value("deposit", value, value.get("credited_to") or "deposit", duration, condition.get("observation_id"))
            event["credited_to"] = value.get("credited_to")
            events.append(event)
        elif field_key == "proof_of_funds" and isinstance(value, Mapping):
            requirements["proof_of_funds"] = value
        elif field_key == "refund_policy" and isinstance(value, Mapping) and value.get("date"):
            event = _event_from_value("refund", value, value.get("credited_to") or "refund", duration, condition.get("observation_id"))
            event["date"] = value.get("date")
            events.append(event)
    flow = compute_cash_flow(events, requirements)
    if unknown:
        flow["unknown_reasons"] = _unique(list(flow.get("unknown_reasons") or []) + unknown)
        flow["net_cost"] = None
        flow["gross_cost"] = None
        flow["peak_own_cash"] = None
    flow["events"] = [
        {key: (format(item[key], "f") if isinstance(item[key], Decimal) else item[key]) for key in item}
        for item in events
    ]
    return flow


def _event_from_value(kind: str, value: Mapping[str, Any], identity: Optional[str], duration: Mapping[str, Any], observation_id: Optional[int]) -> dict:
    return {
        "kind": kind,
        "amount": value.get("amount"),
        "currency": value.get("currency"),
        "period": value.get("period"),
        "duration_months": value.get("duration_months") or duration.get("duration_months"),
        "semesters": value.get("semesters") or duration.get("semesters"),
        "date": value.get("date"),
        "identity": identity,
        "credited_to": value.get("credited_to"),
        "observation_id": observation_id,
    }


def compute_cash_flow(events: Sequence[Mapping[str, Any]], requirements: Optional[Mapping[str, Any]] = None) -> dict:
    """Dated own-cash schedule. Proof of funds is a balance, not a fee."""
    requirements = dict(requirements or {})
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise QualificationError("events", "events must be a list")
    unknown = []
    normalized = []
    currencies = set()
    for event in events:
        if not isinstance(event, Mapping) or event.get("kind") not in ("cost", "deposit", "disbursement", "refund", "discount", "loan"):
            raise QualificationError("events", "event kind is not allowed")
        if event.get("amount") is None or event.get("currency") is None:
            unknown.append(event["kind"] + "_amount")
            continue
        amount = _dec(event["amount"])
        currency = event["currency"]
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise QualificationError("currency", "currency must be an ISO code")
        period = event.get("period")
        if period in (None, "one_time", "total"):
            multiplier = Decimal(1)
        elif period == "per_month":
            months = event.get("duration_months")
            if isinstance(months, bool) or not isinstance(months, int) or months <= 0:
                unknown.append("duration")
                continue
            multiplier = Decimal(months)
        elif period == "per_year":
            months = event.get("duration_months")
            if isinstance(months, bool) or not isinstance(months, int) or months <= 0 or months % 12 != 0:
                unknown.append("duration")
                continue
            multiplier = Decimal(months) / Decimal(12)
        elif period == "per_semester":
            semesters = event.get("semesters")
            if isinstance(semesters, bool) or not isinstance(semesters, int) or semesters <= 0:
                unknown.append("semesters")
                continue
            multiplier = Decimal(semesters)
        else:
            unknown.append("period")
            continue
        currencies.add(currency)
        normalized.append({
            "kind": event["kind"],
            "total": _q2(amount * multiplier),
            "currency": currency,
            "date": event.get("date"),
            "credited_to": event.get("credited_to"),
            "identity": event.get("identity"),
            "award_type": event.get("award_type"),
            "award_key": event.get("award_key"),
        })
    target = requirements.get("currency")
    if len(currencies) > 1 or (target and currencies and target not in currencies):
        target = target or None
        if target is None:
            unknown.append("fx")
        else:
            converted = []
            for item in normalized:
                if item["currency"] == target:
                    converted.append(item)
                    continue
                rate = _find_fx(requirements.get("fx_rates") or [], item["currency"], target, item.get("date"))
                if rate is None:
                    unknown.append("fx")
                    continue
                copied = dict(item)
                copied["total"] = _q2(item["total"] * rate)
                copied["currency"] = target
                converted.append(copied)
            normalized = converted
    elif currencies:
        target = next(iter(currencies))
    credited = {}
    for item in normalized:
        if item["kind"] == "deposit" and item.get("credited_to"):
            credited.setdefault(item["credited_to"], Decimal(0))
            credited[item["credited_to"]] += item["total"]
    gross = Decimal(0)
    support = Decimal(0)
    refunds = Decimal(0)
    for item in normalized:
        if item["kind"] == "cost":
            gross += item["total"]
            credit = credited.get(item["identity"])
            if credit is not None and credit - item["total"] > Decimal("0.01"):
                unknown.append("deposit_credit_exceeds_cost")
        elif item["kind"] == "deposit" and not item.get("credited_to"):
            gross += item["total"]
        elif item["kind"] in ("disbursement", "discount"):
            support += item["total"]
        elif item["kind"] == "refund":
            refunds += item["total"]
    proof = requirements.get("proof_of_funds")
    proof_amount = None
    if isinstance(proof, Mapping) and proof.get("amount") is not None:
        if proof.get("currency") not in (None, target):
            unknown.append("fx")
        else:
            proof_amount = _dec(proof["amount"])
    dated = [item for item in normalized if item.get("date")]
    undated = [item for item in normalized if not item.get("date") and item["kind"] in ("cost", "deposit", "disbursement", "refund", "discount")]
    timing_unknown = bool(undated)
    peak = None
    schedule = []
    if not unknown and not timing_unknown and dated:
        running = Decimal(0)
        peak = Decimal(0)
        remaining_credit = dict(credited)
        order = {"deposit": 0, "cost": 1, "disbursement": 2, "discount": 2, "loan": 3, "refund": 4}
        for item in sorted(dated, key=lambda row: (row["date"], order.get(row["kind"], 9))):
            if item["kind"] == "loan":
                continue
            delta = item["total"]
            if item["kind"] == "cost":
                credit = remaining_credit.get(item["identity"], Decimal(0))
                applied = min(credit, delta)
                remaining_credit[item["identity"]] = credit - applied
                delta -= applied
            elif item["kind"] in ("disbursement", "discount", "refund"):
                delta = -delta
            running += delta
            obligation = running
            if proof_amount is not None:
                obligation = max(obligation, proof_amount)
            if obligation > peak:
                peak = obligation
            schedule.append({"date": item["date"], "balance": _money_str(obligation), "kind": item["kind"]})
        peak = _q2(peak)
    elif not unknown and not timing_unknown and proof_amount is not None:
        peak = _q2(proof_amount)
    if timing_unknown:
        unknown.append("timing")
        peak = None
    result = {
        "net_cost": None if any(item != "timing" for item in unknown) else _money_str(_q2(gross - support - refunds)),
        "gross_cost": None if any(item != "timing" for item in unknown) else _money_str(_q2(gross)),
        "peak_own_cash": None if unknown or peak is None else _money_str(peak),
        "currency": target,
        "unknown_reasons": _unique([item for item in unknown if item != "timing"]),
        "liquidity_unknown_reasons": ["timing"] if timing_unknown else [],
        "repayment_unknown": any(item["kind"] == "loan" or item.get("award_type") == "loan" for item in normalized),
        "credited_deposit_identities": sorted(credited),
        "schedule": schedule,
        "proof_of_funds_is_expense": False,
    }
    return result


def _find_fx(rates: Sequence[Mapping[str, Any]], base: str, quote: str, as_of: Optional[str]):
    if not as_of:
        return None
    for rate in rates:
        if rate.get("base") == base and rate.get("quote") == quote and str(rate.get("as_of")) == as_of:
            return _dec(rate.get("rate") or rate.get("rate_decimal"))
    return None


def _unique(items: Sequence[str]) -> list:
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _record_contradictions(conn, program_key: str, observations: Sequence[Mapping[str, Any]]) -> None:
    grouped = {}
    for item in observations:
        scope = (
            item["field_key"],
            item["subject"].get("award_key"),
            item["subject"].get("campus"),
            item["subject"].get("cycle_label"),
        )
        grouped.setdefault(scope, []).append(item)
    for (field_key, _award, _campus, _cycle), group in grouped.items():
        signatures = {}
        for item in group:
            signatures.setdefault(canonical_json(item["value"]), []).append(item)
        if len(signatures) < 2:
            continue
        rows = [group_rows[0] for group_rows in signatures.values()]
        first, second = rows[0], rows[1]
        existing = _one(
            conn,
            """SELECT id FROM contradictions
               WHERE program_key=? AND field_key=? AND observation_a=? AND observation_b=?""",
            (program_key, field_key, min(first["id"], second["id"]), max(first["id"], second["id"])),
        )
        if existing is None:
            conn.execute(
                """INSERT INTO contradictions (
                       program_key, field_key, observation_a, observation_b
                   ) VALUES (?, ?, ?, ?)""",
                (program_key, field_key, min(first["id"], second["id"]), max(first["id"], second["id"])),
            )


def _load_goal_row(conn, goal_id: int):
    return _one(
        conn,
        "SELECT id, state, current_contract_version, current_strategy_version, epoch FROM goals WHERE id=?",
        (goal_id,),
    )


def _load_contract(conn, goal_id: int, version: int):
    row = _one(
        conn,
        """SELECT version, status, body_json, body_hash, profile_snapshot_json, profile_snapshot_hash
           FROM goal_contracts WHERE goal_id=? AND version=?""",
        (goal_id, version),
    )
    if row is None:
        raise QualificationError("contract", "contract does not exist")
    return row


def _profile_value(snapshot: Mapping[str, Any], key: str):
    entry = snapshot.get(key)
    if not isinstance(entry, Mapping):
        return None, "missing"
    if entry.get("status") != "known" or entry.get("provenance") != "user_confirmed":
        return None, "unconfirmed"
    return entry.get("value"), "known"


def _interview():
    return _sibling("interview")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _objective(body: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(body.get("objective")) if isinstance(body, Mapping) else {}


def _scope(body: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(body.get("scope")) if isinstance(body, Mapping) else {}


def _funding(body: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(body.get("funding")) if isinstance(body, Mapping) else {}


def _completion_body(body: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(body.get("completion")) if isinstance(body, Mapping) else {}


_WORLDWIDE_REGIONS = frozenset({"worldwide", "world", "global", "earth", "anywhere", "any", "all", "*"})


def _is_worldwide_region(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold() in _WORLDWIDE_REGIONS


def _domain_ok(body: Mapping[str, Any]) -> bool:
    return _objective(body).get("domain") == _interview().DOMAIN


def _same_frozen(snapshot: Mapping[str, Any], key: str, expected: Any) -> bool:
    value, state = _profile_value(snapshot, key)
    return state == "known" and value == expected


def _threshold_value(context, key: str):
    """Resolve one declared threshold from the frozen profile only.

    Money copies in the contract body are ignored. A missing or reordered
    threshold_refs list does not fall back to a live interview field.
    """
    body = context["body"]
    funding = _funding(body)
    expected = list(_interview().THRESHOLD_REFS)
    if funding.get("threshold_refs") != expected or key not in expected:
        return None, "missing"
    if key in body or key in funding:
        return None, "missing"
    return _profile_value(context["profile"], key)


def _money_limit(value: Any):
    if not isinstance(value, Mapping) or set(value) != {"amount", "currency"}:
        return None
    amount = value.get("amount")
    currency = value.get("currency")
    if not isinstance(amount, str) or not isinstance(currency, str):
        return None
    return amount, currency


def _funding_choice(context, key: str):
    """Confirmed funding flag. Null stays unknown and is never coerced to false."""
    funding = _funding(context["body"])
    if key not in funding:
        return None, "missing"
    body_value = funding.get(key)
    profile_value, state = _profile_value(context["profile"], key)
    if state != "known" or profile_value != body_value:
        return None, "unconfirmed"
    if type(body_value) is bool:
        return body_value, "known"
    if body_value is None:
        return None, "unknown"
    return None, "invalid"


def _approved_profile(conn, goal_id: int, contract_version: int) -> tuple:
    contract = _load_contract(conn, goal_id, contract_version)
    if contract[1] != "approved":
        raise QualificationError("contract", "contract is not approved")
    raw = contract[4]
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != contract[5]:
        raise QualificationError("profile", "frozen profile hash does not match")
    snapshot = json.loads(raw)
    if not isinstance(snapshot, dict):
        raise QualificationError("profile", "frozen profile is not an object")
    return snapshot, contract[5], json.loads(contract[2])


def _goal_id(goal: Any) -> int:
    if isinstance(goal, bool):
        raise QualificationError("goal", "goal id is required")
    if isinstance(goal, int):
        return goal
    value = getattr(goal, "id", None)
    if value is None and isinstance(goal, Mapping):
        value = goal.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise QualificationError("goal", "goal id is required")
    return value


def evaluate(store, goal: Any, contract_version: int, package_key: str, epoch: int) -> list:
    """Evaluate one package against this version's frozen profile and current epoch."""
    goal_id = _goal_id(goal)
    strategy_version = getattr(goal, "current_strategy_version", None)
    if strategy_version is None and isinstance(goal, Mapping):
        strategy_version = goal.get("strategy_version", goal.get("current_strategy_version"))
    with _transaction(store) as conn:
        _require_tables(conn)
        row = _load_goal_row(conn, goal_id)
        if row is None:
            raise QualificationError("goal", "goal does not exist")
        if isinstance(strategy_version, bool) or not isinstance(strategy_version, int):
            strategy_version = row[3]
        if isinstance(strategy_version, bool) or not isinstance(strategy_version, int):
            raise QualificationError("binding", "goal strategy version is required")
        if not _goal().binding_valid(conn, goal_id, contract_version, strategy_version, epoch):
            raise QualificationError("binding", "goal binding is not current")
        if int(row[2] or -1) != int(contract_version) or int(row[4]) != int(epoch):
            raise QualificationError("binding", "goal epoch or contract is not current")
        profile, profile_hash, body = _approved_profile(conn, goal_id, contract_version)
        package = _one(
            conn,
            """SELECT key, program_key, cycle_label, award_keys_json, components_json,
                      compatibility_basis_ids_json, cash_flow_json, basis_observation_ids_json
               FROM funding_packages WHERE key=?""",
            (package_key,),
        )
        if package is None:
            raise QualificationError("package_key", "funding package does not exist")
        program_key = package[1]
        cycle_label = package[2]
        components = json.loads(package[4])
        cash_flow = json.loads(package[6])
        basis_ids = json.loads(package[7])
        observations = _assigned_observations(
            conn, _qualifying(_load_observations(conn, program_key)), cycle_label,
        )
        consent, consent_state = _profile_value(profile, "consent_private_evaluation")
        private_allowed = consent is True and consent_state == "known"
        builders = (
            _match_degree,
            _match_modality,
            _match_field,
            _match_region,
            _match_intake,
            _match_applicability,
            _match_award_binding,
            _match_period,
            _match_components,
            _match_living,
            _match_funding_type,
            _match_stacking,
            _match_conditions,
            _match_net_cost,
            _match_liquidity,
            _match_work_hours,
        )
        context = {
            "profile": profile,
            "body": body,
            "components": components,
            "observations": observations,
            "program_key": program_key,
            "package_key": package_key,
            "cycle_label": cycle_label,
            "private_allowed": private_allowed,
            "cash_flow": cash_flow,
            "basis_ids": basis_ids,
            "conn": conn,
        }
        results = [builder(context) for builder in builders]
        stored = []
        now = _now()
        for item in results:
            basis = {
                "observation_ids": [value for value in item.basis.get("observation_ids") or [] if isinstance(value, int)],
                "snapshot_ids": [value for value in item.basis.get("snapshot_ids") or [] if isinstance(value, int)],
                "profile_hash": profile_hash,
                "profile_keys": [value for value in item.basis.get("profile_keys") or [] if isinstance(value, str)],
            }
            reason = _public_reason(item.reason)
            conn.execute(
                """INSERT INTO constraint_evaluations (
                       goal_id, contract_version, program_key, constraint_key, package_key,
                       status, basis_json, reason, evaluated_at, epoch, stale_for_goal
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(goal_id, contract_version, package_key, constraint_key) DO UPDATE SET
                       status=excluded.status,
                       basis_json=excluded.basis_json,
                       reason=excluded.reason,
                       evaluated_at=excluded.evaluated_at,
                       epoch=excluded.epoch,
                       stale_for_goal=0""",
                (
                    goal_id, contract_version, program_key, item.constraint_key, package_key,
                    item.status, canonical_json(basis), reason, now, epoch,
                ),
            )
            stored.append(ConstraintEvaluation(
                program_key, package_key, item.constraint_key, item.status, basis, reason,
            ))
        return stored


def _public_reason(reason: str) -> str:
    lowered = reason.lower()
    if any(token in lowered for token in ("amount=", "gpa=", "score=", "threshold=")):
        return "private comparison withheld"
    return reason


def _basis(observations: Sequence[Mapping[str, Any]], keys: Sequence[str], profile_keys: Sequence[str] = ()) -> dict:
    chosen = [item for item in observations if item["field_key"] in keys]
    return {
        "observation_ids": [item["id"] for item in chosen],
        "snapshot_ids": sorted({item["snapshot_id"] for item in chosen}),
        "profile_keys": list(profile_keys),
    }


def _conflicted(conn, program_key: str, field_key: str) -> bool:
    row = _one(
        conn,
        """SELECT 1 FROM contradictions
           WHERE program_key=? AND field_key=? AND (winner IS NULL OR resolved_at IS NULL) LIMIT 1""",
        (program_key, field_key),
    )
    return row is not None


def _kept_rows(observations: Sequence[Mapping[str, Any]], field_key: str, award_key: Optional[str] = None) -> list:
    rows = []
    for item in observations:
        if item["field_key"] != field_key:
            continue
        if award_key is not None and item["subject"].get("award_key") != award_key:
            continue
        rows.append(item)
    return rows


def _evaluation(context, key: str, status: str, basis: dict, reason: str) -> ConstraintEvaluation:
    return ConstraintEvaluation(context["program_key"], context["package_key"], key, status, basis, reason)


def _match_degree(context) -> ConstraintEvaluation:
    return _match_exact_choice(context, "degree_level", ("objective", "degree_level"), "degree_level")


def _match_modality(context) -> ConstraintEvaluation:
    return _match_exact_choice(context, "modality", ("objective", "modality"), "modality")


def _match_exact_choice(context, field_key: str, body_path: tuple, constraint_key: str) -> ConstraintEvaluation:
    observations = context["observations"]
    basis = _basis(observations, [field_key], [constraint_key])
    if not _domain_ok(context["body"]):
        return _evaluation(context, constraint_key, "unknown", basis, "approved domain is not masters_funding")
    if _conflicted(context["conn"], context["program_key"], field_key):
        return _evaluation(context, constraint_key, "unknown", basis, "official observations conflict")
    rows = _kept_rows(observations, field_key)
    if not rows:
        return _evaluation(context, constraint_key, "unknown", basis, "public support is missing")
    values = {item["value"] for item in rows}
    if len(values) != 1:
        return _evaluation(context, constraint_key, "unknown", basis, "public support conflicts")
    observed = next(iter(values))
    wanted = context["body"]
    for key in body_path:
        wanted = wanted.get(key) if isinstance(wanted, Mapping) else None
    allowed = _interview().DEGREES if field_key == "degree_level" else _interview().MODALITIES
    if not isinstance(wanted, str) or wanted not in allowed or not isinstance(observed, str) or observed not in allowed:
        return _evaluation(context, constraint_key, "unknown", basis, "approved criterion is missing")
    if not _same_frozen(context["profile"], field_key, wanted):
        return _evaluation(context, constraint_key, "unknown", basis, "frozen criterion does not match the approved contract")
    if observed == wanted:
        return _evaluation(context, constraint_key, "met", basis, "reviewed source matches the frozen criterion")
    return _evaluation(context, constraint_key, "unmet", basis, "reviewed source differs from the frozen criterion")


def _match_field(context) -> ConstraintEvaluation:
    observations = context["observations"]
    basis = _basis(observations, ["field_of_study", "field_tags"], ["field_tags"])
    if not _domain_ok(context["body"]):
        return _evaluation(context, "field_of_study", "unknown", basis, "approved domain is not masters_funding")
    rows = _kept_rows(observations, "field_of_study") + _kept_rows(observations, "field_tags")
    if not rows:
        return _evaluation(context, "field_of_study", "unknown", basis, "public support is missing")
    wanted = _objective(context["body"]).get("field_tags")
    if not isinstance(wanted, list) or not wanted or not _same_frozen(context["profile"], "field_tags", wanted):
        return _evaluation(context, "field_of_study", "unknown", basis, "frozen field tags are unconfirmed")
    observed = []
    for item in rows:
        observed.extend(item["value"] if isinstance(item["value"], list) else [item["value"]])
    if set(wanted).issubset(set(observed)):
        return _evaluation(context, "field_of_study", "met", basis, "reviewed field contains the frozen tags")
    return _evaluation(context, "field_of_study", "unmet", basis, "reviewed field does not contain the frozen tags")


def _match_region(context) -> ConstraintEvaluation:
    observations = context["observations"]
    basis = _basis(observations, ["region"], ["regions"])
    if not _domain_ok(context["body"]):
        return _evaluation(context, "region", "unknown", basis, "approved domain is not masters_funding")
    rows = _kept_rows(observations, "region")
    if not rows:
        return _evaluation(context, "region", "unknown", basis, "public support is missing")
    scope = _scope(context["body"])
    wanted = scope.get("regions")
    if not isinstance(wanted, list) or not _same_frozen(context["profile"], "regions", wanted):
        return _evaluation(context, "region", "unknown", basis, "frozen regions are unconfirmed")
    if any(_is_worldwide_region(item) for item in wanted):
        return _evaluation(context, "region", "unknown", basis, "worldwide is not a region")
    observed = {item["value"] for item in rows}
    if len(observed) != 1:
        return _evaluation(context, "region", "unknown", basis, "region support conflicts")
    value = next(iter(observed))
    if _is_worldwide_region(value):
        return _evaluation(context, "region", "unmet", basis, "worldwide is not a region")
    if not wanted:
        acknowledged = scope.get("broad_acknowledged") is True and _same_frozen(context["profile"], "scope_broad_acknowledged", True)
        if not acknowledged:
            return _evaluation(context, "region", "unknown", basis, "empty regions are not worldwide")
        return _evaluation(context, "region", "met", basis, "bounded broad scope is explicitly acknowledged")
    if value in wanted:
        return _evaluation(context, "region", "met", basis, "reviewed region is inside the frozen scope")
    return _evaluation(context, "region", "unmet", basis, "reviewed region is outside the frozen scope")


def _match_intake(context) -> ConstraintEvaluation:
    observations = context["observations"]
    basis = _basis(observations, ["intake", "intake_cycle", "cycle_dates"], ["intake_windows"])
    if not _domain_ok(context["body"]):
        return _evaluation(context, "intake", "unknown", basis, "approved domain is not masters_funding")
    wanted = _scope(context["body"]).get("intake_windows")
    if not isinstance(wanted, list) or not wanted or not _same_frozen(context["profile"], "intake_windows", wanted):
        return _evaluation(context, "intake", "unknown", basis, "frozen intake is unconfirmed")
    cycle = context["cycle_label"]
    if cycle not in wanted:
        return _evaluation(context, "intake", "unmet", basis, "package cycle is outside the frozen intake")
    supported = [
        item for item in observations
        if item["subject"].get("cycle_label") == cycle and cycle in item["quote"]
    ]
    if not supported:
        return _evaluation(context, "intake", "unknown", basis, "cycle is not bound to reviewed evidence")
    return _evaluation(context, "intake", "met", _basis(supported, [item["field_key"] for item in supported], ["intake_windows"]), "cycle is source supported")


def _match_applicability(context) -> ConstraintEvaluation:
    observations = context["observations"]
    basis = _basis(observations, ["applicability"])
    if _conflicted(context["conn"], context["program_key"], "applicability"):
        return _evaluation(context, "applicability", "unknown", basis, "applicable observations conflict")
    rows = _kept_rows(observations, "applicability")
    cycle = context["cycle_label"]
    if not rows:
        if all(item["subject"].get("cycle_label") == cycle and cycle in item["quote"] for item in observations):
            return _evaluation(context, "applicability", "met", basis, "every reviewed observation names the cycle")
        return _evaluation(context, "applicability", "unknown", basis, "applicability is missing")
    for item in rows:
        rendered = canonical_json(item["value"])
        if cycle not in rendered and cycle not in item["quote"]:
            return _evaluation(context, "applicability", "unmet", basis, "reviewed applicability names another cycle")
    return _evaluation(context, "applicability", "met", basis, "reviewed applicability names the cycle")


def _match_award_binding(context) -> ConstraintEvaluation:
    components = context["components"]
    observations = context["observations"]
    basis = _basis(observations, [item["field_key"] for item in observations])
    awards = components.get("awards") or []
    if not awards:
        return _evaluation(context, "award_binding", "met" if components.get("costs") else "unknown", basis, "base programme cost has no award")
    for award in awards:
        key = award.get("award_key")
        bound = [
            item for item in observations
            if item["subject"].get("award_key") == key and item["subject"].get("campus") and item["subject"].get("cycle_label")
        ]
        if not key or not bound:
            return _evaluation(context, "award_binding", "unknown", basis, "award is not bound to campus and cycle")
        campuses = {item["subject"].get("campus") for item in bound}
        if len(campuses) != 1:
            return _evaluation(context, "award_binding", "unknown", basis, "award campus conflicts")
    return _evaluation(context, "award_binding", "met", basis, "awards are bound to reviewed evidence")


def _match_period(context) -> ConstraintEvaluation:
    components = context["components"]
    basis = {
        "observation_ids": list(components.get("duration_observation_ids") or []),
        "snapshot_ids": [],
        "profile_keys": [],
    }
    duration = components.get("duration") if isinstance(components.get("duration"), Mapping) else None
    for cost in components.get("costs") or []:
        value = cost.get("value") if isinstance(cost.get("value"), Mapping) else {}
        period = value.get("period") or cost.get("period")
        if period in ("per_year", "per_month") and not (duration or {}).get("duration_months"):
            return _evaluation(context, "period_binding", "unknown", basis, "duration is missing")
        if period == "per_semester" and not (duration or {}).get("semesters"):
            return _evaluation(context, "period_binding", "unknown", basis, "semester count is missing")
        if period is None:
            return _evaluation(context, "period_binding", "unknown", basis, "period is missing")
    for award in components.get("awards") or []:
        for payload in (award.get("values") or {}).values():
            value = payload.get("value") if isinstance(payload.get("value"), Mapping) else {}
            period = value.get("period")
            if period in ("per_year", "per_month", "per_semester") and not duration:
                return _evaluation(context, "period_binding", "unknown", basis, "award duration is missing")
    return _evaluation(context, "period_binding", "met", basis, "period is bound")


def _required_components(context):
    funding = _funding(context["body"])
    configured = funding.get("required_components")
    expected = tuple(_interview().COMPONENT_KEYS)
    if not isinstance(configured, Mapping) or set(configured) != set(expected):
        return None
    if any(type(configured[key]) is not bool for key in expected):
        return None
    normalized = {key: configured[key] for key in expected}
    if not _same_frozen(context["profile"], "required_components", normalized):
        return None
    return [key for key in expected if normalized[key] is True]


def _match_components(context) -> ConstraintEvaluation:
    observations = context["observations"]
    components = context["components"]
    required = _required_components(context)
    if required is None:
        return _evaluation(context, "required_components", "unknown", _basis(observations, ()), "required components are not an explicit map")
    basis = _basis(observations, [field for name in required for field in COMPONENT_FIELDS.get(name, ())])
    missing = []
    for name in required:
        fields = COMPONENT_FIELDS.get(name)
        if not fields:
            return _evaluation(context, "required_components", "unknown", basis, "required component is not a known field")
        if name in ("living", "living_duration") and not components.get("duration"):
            missing.append(name)
            continue
        if not any(_kept_rows(observations, field) for field in fields):
            missing.append(name)
    if missing or not required:
        return _evaluation(context, "required_components", "unknown", basis, "required component evidence is missing")
    if context["cash_flow"].get("net_cost") is None:
        return _evaluation(context, "required_components", "unknown", basis, "component accounting is incomplete")
    return _evaluation(context, "required_components", "met", basis, "required components are evidenced")


def _match_funding_type(context) -> ConstraintEvaluation:
    components = context["components"]
    observations = context["observations"]
    basis = _basis(observations, ["award_type", "funding_source_official"])
    awards = components.get("awards") or []
    grants_only, grants_state = _funding_choice(context, "grants_only")
    if grants_state != "known":
        return _evaluation(context, "funding_type", "unknown", basis, "grants_only is unconfirmed")
    if not awards:
        return _evaluation(context, "funding_type", "unmet", basis, "no evidenced award supplies funding")
    conditional = False
    for award in awards:
        award_type = award.get("award_type")
        if award_type not in AWARD_TYPES:
            return _evaluation(context, "funding_type", "unknown", basis, "award type is missing")
        if not _kept_rows(observations, "funding_source_official", award.get("award_key")):
            return _evaluation(context, "funding_type", "unknown", basis, "official funding source is missing")
        if award_type == "loan":
            reason = "a loan does not satisfy a component" if grants_only else "a loan does not establish full funding"
            return _evaluation(context, "funding_type", "unmet", basis, reason)
        if award_type == "discount":
            return _evaluation(context, "funding_type", "unmet", basis, "a discount does not establish full funding")
        if award_type in ("conditional_ra_ta", "competitive"):
            flag = "accept_conditional_ra_ta" if award_type == "conditional_ra_ta" else "accept_competitive_awards"
            accepted, state = _funding_choice(context, flag)
            label = "conditional assistantship" if award_type == "conditional_ra_ta" else "competitive award"
            if state == "known" and accepted is False:
                return _evaluation(context, "funding_type", "unmet", basis, label + " was refused")
            if state != "known" or accepted is not True:
                return _evaluation(context, "funding_type", "unknown", basis, label + " acceptance is unknown")
            conditional = True
        covered = set()
        for field_key, payload in (award.get("values") or {}).items():
            if field_key in COVERAGE_FIELDS or field_key in ("funding_amount", "living_support_amount"):
                covered.add(field_key)
        if not covered:
            return _evaluation(context, "funding_type", "unknown", basis, "award coverage amount is missing")
    if conditional:
        return _evaluation(context, "funding_type", "conditional", basis, "accepted condition remains conditional")
    if not _award_covers_required(context):
        return _evaluation(context, "funding_type", "unmet", basis, "award does not cover the required components")
    return _evaluation(context, "funding_type", "met", basis, "award type and coverage are reviewed")


def _award_covers_required(context) -> bool:
    required_list = _required_components(context)
    if required_list is None:
        return False
    required = set(required_list)
    coverage_map = {
        "tuition": "tuition_coverage",
        "mandatory_fees": "mandatory_fees_coverage",
        "living": "living_support_amount",
        "living_amount": "living_support_amount",
        "health_insurance": "health_insurance_coverage",
        "visa_costs": "visa_coverage",
        "travel_relocation": "travel_coverage",
        "travel_costs": "travel_coverage",
    }
    evidenced_only = {
        "living_duration", "deposit", "deposits", "proof_of_funds", "proof_of_funds_liquidity",
        "application_fee", "application_costs",
    }
    awards = context["components"].get("awards") or []
    for name in required:
        if name in evidenced_only:
            continue
        field_key = coverage_map.get(name)
        if field_key is None:
            return False
        if name in ("living", "living_amount"):
            present = any(
                "living_support_amount" in award.get("values", {}) or "funding_amount" in award.get("values", {})
                for award in awards
            )
        else:
            present = any(field_key in award.get("values", {}) for award in awards)
        if not present:
            return False
    return True


def _match_living(context) -> ConstraintEvaluation:
    observations = context["observations"]
    basis = _basis(observations, ["living_costs", "living_support_amount", "funding_amount"], ["min_living_adequacy_rule"])
    if not context["private_allowed"]:
        return _evaluation(context, "living_adequacy", "unknown", basis, "consent_required")
    rule, state = _threshold_value(context, "min_living_adequacy_rule")
    if state != "known" or not isinstance(rule, Mapping):
        return _evaluation(context, "living_adequacy", "unknown", basis, "living rule is unconfirmed")
    if rule.get("basis") != "same_location_period" or rule.get("count_housing_once") is not True or rule.get("count_insurance_once") is not True:
        return _evaluation(context, "living_adequacy", "unknown", basis, "living components are not counted once")
    duration = context["components"].get("duration") if isinstance(context["components"].get("duration"), Mapping) else None
    cost = _money_total(_kept_rows(observations, "living_costs"), duration)
    award_rows = []
    for award in context["components"].get("awards") or []:
        for field_key in ("living_support_amount", "funding_amount"):
            payload = (award.get("values") or {}).get(field_key)
            if isinstance(payload, Mapping) and isinstance(payload.get("value"), Mapping):
                award_rows.append({"value": payload["value"]})
    support = _money_total(award_rows, duration)
    if cost is None or support is None or cost[1] != support[1]:
        return _evaluation(context, "living_adequacy", "unknown", basis, "living support period or currency is unresolved")
    floor = rule.get("numeric_floor")
    if floor is not None:
        if not isinstance(floor, Mapping) or floor.get("currency") != support[1]:
            return _evaluation(context, "living_adequacy", "unknown", basis, "living floor currency is unresolved")
        if support[0] < _dec(floor.get("amount")):
            return _evaluation(context, "living_adequacy", "unmet", basis, "living support is below the frozen floor")
    if support[0] < cost[0]:
        return _evaluation(context, "living_adequacy", "unmet", basis, "living support is below the same-period cost")
    return _evaluation(context, "living_adequacy", "met", basis, "living support covers the same-period cost")


def _money_total(rows: Sequence[Mapping[str, Any]], duration: Optional[Mapping[str, Any]]):
    if not rows:
        return None
    total = Decimal(0)
    currency = None
    for item in rows:
        value = item.get("value")
        if not isinstance(value, Mapping) or "amount" not in value:
            return None
        code = value.get("currency")
        if not isinstance(code, str):
            return None
        if currency is None:
            currency = code
        elif currency != code:
            return None
        amount = _dec(value.get("amount"))
        period = value.get("period")
        if period in ("total", "one_time"):
            total += amount
        elif period == "per_year":
            months = None if not isinstance(duration, Mapping) else duration.get("duration_months")
            if isinstance(months, bool) or not isinstance(months, int) or months <= 0 or months % 12 != 0:
                return None
            total += amount * (Decimal(months) / Decimal(12))
        elif period == "per_month":
            months = None if not isinstance(duration, Mapping) else duration.get("duration_months")
            if isinstance(months, bool) or not isinstance(months, int) or months <= 0:
                return None
            total += amount * Decimal(months)
        elif period == "per_semester":
            semesters = None if not isinstance(duration, Mapping) else duration.get("semesters")
            if isinstance(semesters, bool) or not isinstance(semesters, int) or semesters <= 0:
                return None
            total += amount * Decimal(semesters)
        else:
            return None
    return total, currency


def _match_stacking(context) -> ConstraintEvaluation:
    components = context["components"]
    awards = components.get("awards") or []
    package = _one(
        context["conn"],
        "SELECT compatibility_basis_ids_json FROM funding_packages WHERE key=?",
        (context["package_key"],),
    )
    compatibility_ids = json.loads(package[0]) if package else []
    basis = {"observation_ids": compatibility_ids, "snapshot_ids": [], "profile_keys": []}
    keys = [award.get("award_key") for award in awards]
    if len(keys) <= 1:
        return _evaluation(context, "stacking", "met", basis, "single award needs no stacking")
    if set(components.get("combined_award_keys") or []) != set(keys) or not compatibility_ids:
        return _evaluation(context, "stacking", "unknown", basis, "stacking compatibility is unknown")
    rows = _rows(
        context["conn"],
        "SELECT id, value_json, assurance FROM observations WHERE id IN (" + ",".join("?" for _ in compatibility_ids) + ")",
        tuple(compatibility_ids),
    )
    if len(rows) != len(compatibility_ids) or any(row[2] != ASSURANCE_KEPT for row in rows):
        return _evaluation(context, "stacking", "unknown", basis, "stacking evidence is not reviewed")
    for row in rows:
        value = json.loads(row[1])
        if value.get("relation") != "compatible" or set(value.get("award_keys") or []) != set(keys):
            return _evaluation(context, "stacking", "unknown", basis, "stacking evidence does not name these awards")
    return _evaluation(context, "stacking", "met", basis, "stacking is reviewed compatible")


def _match_conditions(context) -> ConstraintEvaluation:
    observations = context["observations"]
    basis = _basis(observations, ["gpa_conditions", "language_conditions", "citizenship_conditions", "renewal_conditions", "acceptance_status"])
    if not context["private_allowed"]:
        person_specific = _kept_rows(observations, "gpa_conditions") + _kept_rows(observations, "citizenship_conditions")
        if person_specific:
            return _evaluation(context, "eligibility_conditions", "unknown", basis, "consent_required")
    for field_key, profile_key in (("gpa_conditions", "academic_record"), ("citizenship_conditions", "citizenship"), ("language_conditions", "languages")):
        rows = _kept_rows(observations, field_key)
        if not rows:
            continue
        if _numeric_condition_blocks(context, rows, profile_key):
            return _evaluation(context, "eligibility_conditions", "unmet", basis, "reviewed condition is not satisfied")
        if _numeric_condition_unknown(context, rows, profile_key):
            return _evaluation(context, "eligibility_conditions", "unknown", basis, "condition equivalence is unresolved")
    renewal = _kept_rows(observations, "renewal_conditions")
    if renewal:
        return _evaluation(context, "eligibility_conditions", "conditional", basis, "renewal condition remains conditional")
    acceptance = _kept_rows(observations, "acceptance_status")
    if any(isinstance(item["value"], Mapping) and item["value"].get("status") == "rejected" for item in acceptance):
        return _evaluation(context, "eligibility_conditions", "unmet", basis, "documented acceptance was declined")
    return _evaluation(context, "eligibility_conditions", "met", basis, "no blocking reviewed condition")


def _numeric_condition_blocks(context, rows: Sequence[Mapping[str, Any]], profile_key: str) -> bool:
    wanted, state = _profile_value(context["profile"], profile_key)
    if state != "known":
        return False
    for item in rows:
        value = item["value"]
        if not isinstance(value, Mapping) or "minimum" not in value or "scale" not in value:
            continue
        if not isinstance(wanted, Mapping) or wanted.get("scale") != value.get("scale"):
            continue
        try:
            if _dec(wanted.get("value")) < _dec(value.get("minimum")):
                return True
        except QualificationError:
            return False
    if profile_key == "citizenship" and isinstance(wanted, str):
        allowed = []
        for item in rows:
            value = item["value"]
            if isinstance(value, Mapping) and isinstance(value.get("citizenship"), list):
                allowed.extend(value["citizenship"])
        return bool(allowed) and wanted not in allowed
    return False


def _numeric_condition_unknown(context, rows: Sequence[Mapping[str, Any]], profile_key: str) -> bool:
    wanted, state = _profile_value(context["profile"], profile_key)
    if state != "known":
        return True
    for item in rows:
        value = item["value"]
        if isinstance(value, Mapping) and "minimum" in value:
            if not isinstance(wanted, Mapping) or wanted.get("scale") != value.get("scale"):
                return True
    return False


def _match_net_cost(context) -> ConstraintEvaluation:
    cash_flow = context["cash_flow"]
    basis = {"observation_ids": list(context["basis_ids"]), "snapshot_ids": [], "profile_keys": ["max_net_total_cost"]}
    if cash_flow.get("net_cost") is None or cash_flow.get("unknown_reasons"):
        return _evaluation(context, "net_cost", "unknown", basis, "net cost remains unknown")
    if not _award_covers_required(context):
        return _evaluation(context, "net_cost", "unmet", basis, "coverage does not pay the required components")
    if not context["private_allowed"]:
        return _evaluation(context, "net_cost", "unknown", basis, "consent_required")
    cap, state = _threshold_value(context, "max_net_total_cost")
    parsed = None if state != "known" else _money_limit(cap)
    if parsed is None:
        return _evaluation(context, "net_cost", "unknown", basis, "cost cap is unconfirmed")
    if parsed[1] != cash_flow.get("currency"):
        return _evaluation(context, "net_cost", "unknown", basis, "fx is unavailable")
    if _dec(cash_flow["net_cost"]) <= _dec(parsed[0]):
        return _evaluation(context, "net_cost", "met", basis, "net cost is within the frozen cap")
    return _evaluation(context, "net_cost", "unmet", basis, "net cost exceeds the frozen cap")


def _match_liquidity(context) -> ConstraintEvaluation:
    cash_flow = context["cash_flow"]
    basis = {"observation_ids": list(context["basis_ids"]), "snapshot_ids": [], "profile_keys": ["max_upfront_cash"]}
    if cash_flow.get("peak_own_cash") is None or cash_flow.get("unknown_reasons"):
        return _evaluation(context, "liquidity", "unknown", basis, "liquidity timing is unknown")
    if not context["private_allowed"]:
        return _evaluation(context, "liquidity", "unknown", basis, "consent_required")
    cap, state = _threshold_value(context, "max_upfront_cash")
    parsed = None if state != "known" else _money_limit(cap)
    if parsed is None or parsed[1] != cash_flow.get("currency"):
        return _evaluation(context, "liquidity", "unknown", basis, "liquidity cap is unresolved")
    if _dec(cash_flow["peak_own_cash"]) <= _dec(parsed[0]):
        return _evaluation(context, "liquidity", "met", basis, "peak own cash is within the frozen cap")
    return _evaluation(context, "liquidity", "unmet", basis, "peak own cash exceeds the frozen cap")


def _match_work_hours(context) -> ConstraintEvaluation:
    observations = context["observations"]
    basis = _basis(observations, ["work_hours_required"], ["max_work_hours_acceptable"])
    rows = _kept_rows(observations, "work_hours_required")
    if not rows:
        return _evaluation(context, "work_hours", "met", basis, "no reviewed work requirement is evidenced")
    if not context["private_allowed"]:
        return _evaluation(context, "work_hours", "unknown", basis, "consent_required")
    cap, state = _threshold_value(context, "max_work_hours_acceptable")
    if state != "known" or isinstance(cap, bool) or not isinstance(cap, int):
        return _evaluation(context, "work_hours", "unknown", basis, "work-hour cap is unconfirmed")
    hours = []
    for item in rows:
        value = item["value"]
        if isinstance(value, bool) or not isinstance(value, int):
            return _evaluation(context, "work_hours", "unknown", basis, "work requirement is unresolved")
        hours.append(value)
    if len(set(hours)) != 1:
        return _evaluation(context, "work_hours", "unknown", basis, "work requirement conflicts")
    if hours[0] <= cap:
        return _evaluation(context, "work_hours", "met", basis, "reviewed work requirement is within the frozen cap")
    return _evaluation(context, "work_hours", "unmet", basis, "reviewed work requirement exceeds the frozen cap")


def completion_status(store, goal: Any) -> CompletionStatus:
    """Positive completion requires the nested approved count of genuinely matching packages."""
    goal_id = _goal_id(goal)
    conn = _connect(store)
    _require_tables(conn)
    row = _load_goal_row(conn, goal_id)
    if row is None:
        raise QualificationError("goal", "goal does not exist")
    contract_version = row[2]
    epoch = int(row[4])

    def _not_ready(reason: str, substate: str = "partial_coverage") -> CompletionStatus:
        return CompletionStatus(False, substate, {
            "reason": reason,
            "eligible_packages": [],
            "research_not_offer": True,
        })

    if contract_version is None:
        return _not_ready("no_approved_contract")
    contract = _load_contract(conn, goal_id, int(contract_version))
    if contract[1] != "approved":
        return _not_ready("contract_not_approved")
    raw_profile = contract[4]
    if not isinstance(raw_profile, str) or hashlib.sha256(raw_profile.encode("utf-8")).hexdigest() != contract[5]:
        return _not_ready("profile_unconfirmed")
    profile = json.loads(raw_profile)
    if not isinstance(profile, dict):
        return _not_ready("profile_unconfirmed")
    body = json.loads(contract[2])
    if not isinstance(body, dict) or not _domain_ok(body):
        return _not_ready("domain_unsupported")
    completion = body.get("completion")
    if not isinstance(completion, Mapping) or "min_candidates" not in completion:
        return _not_ready("min_candidates_missing")
    minimum = completion.get("min_candidates")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        return _not_ready("min_candidates_missing")
    if not _same_frozen(profile, "completion_min_candidates", minimum):
        return _not_ready("min_candidates_missing")
    critical = _critical_fields(body)
    if list(completion.get("critical_fields") or []) != list(critical):
        return _not_ready("critical_fields_missing")
    evaluations = _rows(
        conn,
        """SELECT package_key, constraint_key, status, stale_for_goal, epoch, basis_json, program_key
           FROM constraint_evaluations
           WHERE goal_id=? AND contract_version=? AND epoch=?""",
        (goal_id, contract_version, epoch),
    )
    pending = _rows(
        conn,
        """SELECT assurance FROM observations
           WHERE goal_id=? AND contract_version=? AND epoch=? AND invalidated_at IS NULL""",
        (goal_id, contract_version, epoch),
    )
    if not evaluations:
        if pending and all(item[0] in (ASSURANCE_PENDING, ASSURANCE_UNAVAILABLE, ASSURANCE_CODE_CHECKED) for item in pending):
            return CompletionStatus(False, "verifier_unavailable" if any(item[0] == ASSURANCE_UNAVAILABLE for item in pending) else "partial_coverage", {
                "reason": "review_pending",
                "eligible_packages": [],
                "pending_observations": len(pending),
                "research_not_offer": True,
            })
        return CompletionStatus(False, "partial_coverage", {"reason": "no_current_evaluations", "eligible_packages": [], "research_not_offer": True})
    grouped = {}
    stale = False
    for package_key, constraint_key, status, stale_flag, eval_epoch, basis_json, program_key in evaluations:
        if int(eval_epoch) != epoch or int(stale_flag) == 1:
            stale = True
            continue
        grouped.setdefault(package_key, {"program_key": program_key, "rows": []})["rows"].append(
            (constraint_key, status, basis_json)
        )
    if not grouped:
        return CompletionStatus(False, "partial_coverage", {"reason": "stale_evaluations", "eligible_packages": [], "research_not_offer": True})
    eligible = []
    unmet = 0
    unknown = 0
    for package_key, payload in grouped.items():
        statuses = {key: status for key, status, _basis in payload["rows"]}
        if any(status == "unmet" for status in statuses.values()):
            unmet += 1
            continue
        if any(status == "unknown" for status in statuses.values()):
            unknown += 1
            continue
        evidenced = _coverage_fields(conn, payload["program_key"])
        if any(_critical_evidence_missing(field, evidenced) for field in critical):
            unknown += 1
            continue
        contradiction = _one(
            conn,
            """SELECT c.field_key FROM contradictions c
               JOIN funding_packages p ON p.program_key=c.program_key
               WHERE p.key=? AND (c.winner IS NULL OR c.resolved_at IS NULL)
                 AND c.field_key IN ({})
               LIMIT 1""".format(",".join("?" for _ in critical)),
            (package_key, *critical),
        )
        if contradiction is not None:
            unknown += 1
            continue
        conditional_rows = [(key, status, basis) for key, status, basis in payload["rows"] if status == "conditional"]
        if conditional_rows and not _accepted_conditional(profile, conditional_rows, conn, body):
            unknown += 1
            continue
        label = "conditional" if conditional_rows else "met"
        eligible.append({
            "package_key": package_key,
            "program_key": payload["program_key"],
            "outcome": label,
            "research_not_offer": True,
            "award_receipt": False,
        })
    detail = {
        "eligible_packages": eligible,
        "eligible_count": len(eligible),
        "min_candidates": minimum,
        "unmet_packages": unmet,
        "unknown_packages": unknown,
        "research_not_offer": True,
        "critical_fields": list(critical),
    }
    if len(eligible) >= minimum:
        return CompletionStatus(True, "research_ready", detail)
    if grouped and unmet == len(grouped):
        return CompletionStatus(False, "no_match", detail)
    return CompletionStatus(False, "partial_coverage", detail)


def _coverage_fields(conn, program_key: str) -> set:
    return {
        item["field_key"]
        for item in _assigned_observations(conn, _qualifying(_load_observations(conn, program_key)))
    }


def _critical_evidence_missing(field: str, evidenced: set) -> bool:
    equivalents = {
        "living_support_amount": {"living_support_amount", "funding_amount"},
        "living_support_duration": {"living_support_duration", "program_duration"},
        "field_of_study": {"field_of_study", "field_tags"},
        "intake_cycle": {"intake_cycle", "intake", "cycle_dates"},
    }
    return not equivalents.get(field, {field}).intersection(evidenced)


def _accepted_conditional(snapshot: Mapping[str, Any], rows: Sequence[tuple], conn, body: Mapping[str, Any]) -> bool:
    acceptance = {
        "conditional_ra_ta": "accept_conditional_ra_ta",
        "competitive": "accept_competitive_awards",
        "renewal": "accept_renewal_conditions",
    }
    funding = _funding(body)
    for _key, _status, basis_json in rows:
        try:
            basis = json.loads(basis_json)
        except json.JSONDecodeError:
            return False
        ids = [item for item in basis.get("observation_ids") or [] if isinstance(item, int)]
        if not ids:
            return False
        values = _rows(
            conn,
            "SELECT field_key, value_json, assurance FROM observations WHERE id IN (" + ",".join("?" for _ in ids) + ")",
            tuple(ids),
        )
        if len(values) != len(ids) or any(item[2] != ASSURANCE_KEPT for item in values):
            return False
        for field_key, value_json, _assurance in values:
            value = json.loads(value_json)
            if field_key == "award_type" and value in acceptance:
                flag = acceptance[value]
                if funding.get(flag) is not True:
                    return False
                accepted, state = _profile_value(snapshot, flag)
                if state != "known" or accepted is not True:
                    return False
            if field_key == "renewal_conditions":
                accepted, state = _profile_value(snapshot, "accept_renewal_conditions")
                if state != "known" or accepted is not True:
                    return False
            if field_key == "acceptance_status" and isinstance(value, Mapping):
                if value.get("status") in ("rejected", "competitive"):
                    return False
    return True
