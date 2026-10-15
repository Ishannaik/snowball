"""Goal-gated frontier planner.

Gap queries are code templates. One model call may add hypotheses, and only
a typed PublicFrame plus public coverage reaches engine.plan. Admission is
ledger.reserve_batch with the node and lease binding. Novelty breaks ties
and is not part of the score or the progress fingerprint.
"""
from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import re
import secrets
import sqlite3
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

PROGRAM_FIELD_QUERY_COOLDOWN_SEC = 604800
PLAN_MAX_HYPOTHESES = 6
LEASE_SEC = 900
QUERY_SOURCE = "exa"

_QUERY_MIN = 8
_QUERY_MAX = 200
_URL_MAX = 500
_RATIONALE_MAX = 300
_KEY_MAX = 80

DEFAULT_CRITICAL = (
    "tuition",
    "mandatory_fees",
    "living_costs",
    "health_insurance",
    "program_duration",
    "tuition_coverage",
    "funding_amount",
)

_FORBIDDEN_KEYS = frozenset({
    "profile",
    "profile_snapshot",
    "profile_snapshot_json",
    "profile_snapshot_hash",
    "profile_fields",
    "body_hash",
    "private",
    "thresholds",
    "threshold",
    "eligibility",
    "max_upfront_cash",
    "citizenship",
    "gpa",
    "passport",
    "salary",
})

_CREDENTIALS = (
    "passport",
    "password",
    "ssn",
    "profile_hash",
    "private_hash",
)

_SOCIAL_HOSTS = frozenset({
    "reddit.com",
    "redd.it",
    "x.com",
    "twitter.com",
    "facebook.com",
    "discord.com",
    "quora.com",
    "news.ycombinator.com",
    "linkedin.com",
})

_FIELD_PHRASE = {
    "tuition": "official tuition fees",
    "mandatory_fees": "mandatory fees",
    "living_costs": "estimated living costs",
    "health_insurance": "health insurance",
    "program_duration": "program duration",
    "tuition_coverage": "tuition coverage",
    "funding_amount": "funding amount",
    "application_fee": "application fee",
    "visa_costs": "visa costs",
    "travel": "travel costs",
    "stacking_policy": "award stacking rules",
    "disbursement_timing": "disbursement timing",
}


class PlannerError(Exception):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


class PlannerOutage(PlannerError):
    """The planner model reply is missing or not the hypothesis object."""


@dataclass(frozen=True)
class Proposal:
    kind: str
    program_key: Optional[str]
    field_key: Optional[str]
    query: Optional[str]
    url: Optional[str]
    rationale: str
    origin: str


@dataclass(frozen=True)
class ValidatedProposal:
    proposal: Proposal
    query_id: Optional[int]
    page_id: Optional[int]
    decision: str
    reason: str


@dataclass(frozen=True)
class SchedulePlan:
    selected_query_ids: tuple
    selected_page_ids: tuple
    decision_ids: tuple
    reservation_ids: tuple


def _sibling(name: str):
    canonical = f"snowball_{name}"
    loaded = sys.modules.get(canonical)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(canonical, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    spec.loader.exec_module(module)
    return module


def _register_self() -> None:
    sys.modules.setdefault("snowball_planner", sys.modules[__name__])


_register_self()


def _goal():
    return _sibling("goal")


def _ledger():
    return _sibling("ledger")


def _qualify():
    return _sibling("qualify")


def _orch():
    return _sibling("orchestrator")


def _interview():
    return _sibling("interview")


def _now() -> int:
    return int(_ledger().now_unix())


def _digest(value: Any) -> str:
    return _goal().digest(value)


def _canonical(value: Any) -> str:
    return _goal().canonical(value)


def _db(store) -> sqlite3.Connection:
    conn = getattr(store, "db", None)
    if conn is None or not hasattr(conn, "execute"):
        raise PlannerError("store", "Store.db is required")
    return conn


def _require_frame(frame: Any):
    public = _interview().PublicFrame
    if not isinstance(frame, public):
        raise PlannerError("frame", "typed PublicFrame required")
    return frame


def _runnable(store, goal: Any):
    goal_mod = _goal()
    if isinstance(goal, goal_mod.RunnableGoal):
        runnable = goal
    elif isinstance(goal, goal_mod.Goal):
        runnable = goal_mod.runnable_goal(store, _now())
        if runnable is None or runnable.goal.id != goal.id:
            raise PlannerError("goal", "goal is not runnable")
    else:
        raise PlannerError("goal", "expected RunnableGoal")
    _require_frame(runnable.frame)
    return runnable


def _field_keys() -> frozenset:
    keys = getattr(_qualify(), "FIELD_KEYS", None)
    if not keys:
        raise PlannerError("field_key", "qualify.FIELD_KEYS missing")
    return frozenset(keys)


def _critical_fields(contract) -> list:
    body = contract.body if hasattr(contract, "body") else {}
    completion = body.get("completion") if isinstance(body, Mapping) else {}
    raw = completion.get("critical_fields") if isinstance(completion, Mapping) else None
    known = _field_keys()
    if isinstance(raw, list):
        picked = [item for item in raw if isinstance(item, str) and item in known]
        if picked:
            return picked
    return [item for item in DEFAULT_CRITICAL if item in known]


def _reject_private_keys(payload: Mapping[str, Any], field: str) -> None:
    found = sorted(set(payload) & _FORBIDDEN_KEYS)
    if found:
        raise PlannerError(field, "private fields are not public coverage: " + ",".join(found))


def _flatten(value: Any) -> list:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, Mapping):
        out = []
        for key, item in value.items():
            out.extend(_flatten(key))
            out.extend(_flatten(item))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_flatten(item))
        return out
    return []


def _tokens(text: str) -> list:
    return re.findall(r"[a-z0-9]+", text.lower())


def _public_strings(frame) -> list:
    values = []
    for name in (
        "domain",
        "public_objective",
        "public_criteria",
        "field_tags",
        "regions",
        "intake_windows",
        "required_source_fields",
    ):
        values.extend(_flatten(getattr(frame, name, None)))
    return values


def _anchor_tokens(frame, program: Optional[Mapping[str, Any]] = None) -> set:
    tokens = set()
    for text in _public_strings(frame):
        tokens.update(token for token in _tokens(text) if len(token) >= 4)
    if program:
        for key in ("name", "official_domain"):
            raw = program.get(key)
            if isinstance(raw, str):
                tokens.update(token for token in _tokens(raw.replace(".", " ")) if len(token) >= 4)
    return tokens


def _degree_modality(frame) -> tuple:
    criteria = getattr(frame, "public_criteria", None)
    degree = None
    modality = None
    if isinstance(criteria, Mapping):
        degree = criteria.get("degree_level") or criteria.get("degree")
        modality = criteria.get("modality")
    if not isinstance(degree, str):
        degree = None
    if not isinstance(modality, str):
        modality = None
    return degree, modality


def _scope_conflict(text: str, frame, program: Optional[Mapping[str, Any]]) -> Optional[str]:
    degree, modality = _degree_modality(frame)
    lowered = text.lower()
    if degree == "masters" and re.search(r"\b(phd|doctorate)\b", lowered):
        return "phd is outside the approved masters scope"
    if modality == "on_campus" and re.search(r"\bonline\b", lowered):
        return "online is outside the approved on-campus scope"
    if program and degree == "masters" and program.get("degree_level") == "phd":
        return "programme degree is outside the approved scope"
    if program and modality == "on_campus" and program.get("modality") == "online":
        return "programme modality is outside the approved scope"
    return None


def _credential(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in _CREDENTIALS)


def _one(conn, sql: str, params: tuple = ()): 
    return conn.execute(sql, params).fetchone()


def _all(conn, sql: str, params: tuple = ()): 
    return list(conn.execute(sql, params).fetchall())


def _program(conn, program_key: str) -> Optional[dict]:
    row = _one(
        conn,
        """SELECT key, name, official_domain, degree_level, modality, provenance
             FROM programs WHERE key=?""",
        (program_key,),
    )
    if row is None:
        return None
    return {
        "key": row["key"],
        "name": row["name"],
        "official_domain": row["official_domain"],
        "degree_level": row["degree_level"],
        "modality": row["modality"],
        "provenance": row["provenance"],
    }


def _program_role(conn, goal_id: int, version: int, program_key: str) -> Optional[dict]:
    row = _one(
        conn,
        """SELECT role, visibility FROM goal_programs
             WHERE goal_id=? AND contract_version=? AND program_key=?""",
        (goal_id, version, program_key),
    )
    if row is None:
        return None
    return {"role": row["role"], "visibility": row["visibility"]}


def _coverage_programs(coverage: Any) -> list:
    if hasattr(coverage, "programs"):
        rows = coverage.programs
    elif isinstance(coverage, Mapping):
        _reject_private_keys(coverage, "coverage")
        extra = set(coverage) - {"programs", "known_domains"}
        if extra:
            raise PlannerError("coverage", "unknown public coverage fields: " + ",".join(sorted(extra)))
        rows = coverage.get("programs")
    else:
        raise PlannerError("coverage", "expected public coverage")
    if not isinstance(rows, (list, tuple)):
        raise PlannerError("coverage", "programs must be a list")
    parsed = []
    allowed = {"program_key", "name", "official_domain", "unknown_fields", "contradictions"}
    for index, item in enumerate(rows):
        if hasattr(item, "program_key") and not isinstance(item, Mapping):
            unknown = getattr(item, "public_unknown_fields", None)
            if unknown is None:
                unknown = getattr(item, "unknown_fields", ())
            contested = getattr(item, "public_contested_fields", ())
            parsed.append({
                "program_key": item.program_key,
                "name": getattr(item, "name", None),
                "official_domain": getattr(item, "official_domain", None),
                "unknown_fields": [field for field in unknown if isinstance(field, str)],
                "contradictions": [field for field in contested if isinstance(field, str)],
            })
            continue
        if not isinstance(item, Mapping):
            raise PlannerError("coverage", f"programs[{index}] must be an object")
        _reject_private_keys(item, "coverage")
        extra = set(item) - allowed
        if extra:
            raise PlannerError("coverage", "unknown program coverage fields: " + ",".join(sorted(extra)))
        key = item.get("program_key")
        if not isinstance(key, str) or not key:
            raise PlannerError("coverage", "program_key is required")
        unknown = item.get("unknown_fields") or []
        if not isinstance(unknown, list) or not all(isinstance(field, str) for field in unknown):
            raise PlannerError("coverage", "unknown_fields must be strings")
        contradictions = item.get("contradictions") or []
        clean_contradictions = []
        if not isinstance(contradictions, list):
            raise PlannerError("coverage", "contradictions must be a list")
        for entry in contradictions:
            if isinstance(entry, str):
                clean_contradictions.append(entry)
            elif isinstance(entry, Mapping) and isinstance(entry.get("field_key"), str):
                clean_contradictions.append(entry["field_key"])
            else:
                raise PlannerError("coverage", "contradiction must name a public field")
        parsed.append({
            "program_key": key,
            "name": item.get("name") if isinstance(item.get("name"), str) else None,
            "official_domain": item.get("official_domain") if isinstance(item.get("official_domain"), str) else None,
            "unknown_fields": list(unknown),
            "contradictions": clean_contradictions,
        })
    return parsed


def public_coverage_summary(store, goal: Any, coverage: Any) -> dict:
    """Public program names, domains, and missing facts. No profile values."""
    runnable = _runnable(store, goal)
    conn = _db(store)
    programs = []
    for item in _coverage_programs(coverage)[:20]:
        row = _program(conn, item["program_key"])
        name = row["name"] if row and isinstance(row["name"], str) else item.get("name")
        domain = row["official_domain"] if row and isinstance(row["official_domain"], str) else item.get("official_domain")
        contradictions = [{"field_key": field, "source_ids": []} for field in item["contradictions"]]
        programs.append({
            "program_key": item["program_key"],
            "name": name,
            "official_domain": domain,
            "unknown_fields": list(item["unknown_fields"]),
            "contradictions": contradictions,
        })
    known = {item["official_domain"] for item in programs if item.get("official_domain")}
    for row in _all(
        conn,
        """SELECT DISTINCT p.domain FROM pages p
             JOIN goal_pages gp ON gp.page_id=p.id
             WHERE gp.goal_id=? AND gp.contract_version=? AND p.status='fetched'""",
        (runnable.goal.id, runnable.contract.version),
    ):
        if row["domain"]:
            known.add(row["domain"])
    return {"programs": programs, "known_domains": sorted(known)}


def _whitelist_summary(coverage: Any) -> dict:
    programs = []
    for item in _coverage_programs(coverage)[:20]:
        contradictions = []
        for field in item["contradictions"]:
            contradictions.append({"field_key": field, "source_ids": []})
        programs.append({
            "program_key": item["program_key"],
            "name": item.get("name"),
            "official_domain": item.get("official_domain"),
            "unknown_fields": list(item["unknown_fields"]),
            "contradictions": contradictions,
        })
    known = coverage.get("known_domains") if isinstance(coverage, Mapping) else []
    if known is None:
        known = []
    if not isinstance(known, list) or not all(isinstance(item, str) for item in known):
        raise PlannerError("coverage", "known_domains must be strings")
    return {"programs": programs, "known_domains": list(known)}


def _kept_field_hashes(conn, goal_id: int, version: int) -> list:
    rows = _all(
        conn,
        """SELECT o.program_key, o.field_key, s.content_hash
             FROM observations o
             JOIN source_snapshots s ON s.id=o.snapshot_id
             WHERE o.goal_id=? AND o.contract_version=? AND o.invalidated_at IS NULL
               AND o.assurance='reviewed_kept' AND s.visibility='public'
               AND s.superseded_by IS NULL
             ORDER BY o.program_key, o.field_key, s.content_hash""",
        (goal_id, version),
    )
    return [[row["program_key"], row["field_key"], row["content_hash"]] for row in rows]


def _unknown_critical(conn, runnable) -> dict:
    critical = _critical_fields(runnable.contract)
    rows = _all(
        conn,
        """SELECT program_key FROM goal_programs
             WHERE goal_id=? AND contract_version=? AND visibility='public' AND role='candidate'
             ORDER BY program_key""",
        (runnable.goal.id, runnable.contract.version),
    )
    kept_rows = _kept_field_hashes(conn, runnable.goal.id, runnable.contract.version)
    kept = {}
    for program_key, field_key, _hash in kept_rows:
        kept.setdefault(program_key, set()).add(field_key)
    unknown = {}
    for row in rows:
        program_key = row["program_key"]
        present = kept.get(program_key, set())
        unknown[program_key] = [field for field in critical if field not in present]
    return unknown


def coverage_fingerprint(store, goal: Any) -> str:
    """Evidence identity. Retrieval times, strategy numbers, and novelty are excluded."""
    runnable = _runnable(store, goal)
    conn = _db(store)
    unknown = _unknown_critical(conn, runnable)
    material = {
        "kept": _kept_field_hashes(conn, runnable.goal.id, runnable.contract.version),
        "unknown": [[key, fields] for key, fields in sorted(unknown.items())],
    }
    return _digest(material)


def work_fingerprint(store, goal: Any) -> str:
    """Queued identities. Timestamps and strategy versions are excluded."""
    runnable = _runnable(store, goal)
    rows = _all(
        _db(store),
        """SELECT node_kind, node_key, visited_at FROM goal_node_identities
             WHERE goal_id=? ORDER BY node_kind, node_key""",
        (runnable.goal.id,),
    )
    material = [
        {"visited": row["visited_at"] is not None, "kind": row["node_kind"], "key": row["node_key"]}
        for row in rows
    ]
    return _digest(material)


def _field_snapshot(conn, program_key: str, field_key: str) -> str:
    rows = _all(
        conn,
        """SELECT s.content_hash FROM observations o
             JOIN source_snapshots s ON s.id=o.snapshot_id
             WHERE o.program_key=? AND o.field_key=? AND o.invalidated_at IS NULL
               AND o.assurance='reviewed_kept' AND s.visibility='public'
               AND s.superseded_by IS NULL
             ORDER BY s.content_hash""",
        (program_key, field_key),
    )
    return _digest([row["content_hash"] for row in rows])


def _cooldown_row(conn, goal_id: int, version: int, program_key: str, field_key: str):
    return _one(
        conn,
        """SELECT next_query_at, snapshot_hash FROM program_field_cooldowns
             WHERE goal_id=? AND contract_version=? AND program_key=? AND field_key=?""",
        (goal_id, version, program_key, field_key),
    )


def _cooldown_active(conn, goal_id: int, version: int, program_key: str, field_key: str, now: int) -> bool:
    row = _cooldown_row(conn, goal_id, version, program_key, field_key)
    if row is None:
        return False
    current = _field_snapshot(conn, program_key, field_key)
    previous = row["snapshot_hash"]
    if previous and current and previous != current:
        return False
    return now < int(row["next_query_at"])


def _remember_cooldown(conn, goal_id: int, version: int, program_key: str, field_key: str, now: int) -> None:
    snapshot = _field_snapshot(conn, program_key, field_key)
    conn.execute(
        """INSERT INTO program_field_cooldowns
             (goal_id, contract_version, program_key, field_key, next_query_at, snapshot_hash)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (goal_id, contract_version, program_key, field_key)
           DO UPDATE SET next_query_at=excluded.next_query_at, snapshot_hash=excluded.snapshot_hash""",
        (goal_id, version, program_key, field_key, now + PROGRAM_FIELD_QUERY_COOLDOWN_SEC, snapshot),
    )


def _compose_gap_query(frame, program: Mapping[str, Any], field_key: str) -> str:
    phrase = _FIELD_PHRASE.get(field_key, "official " + field_key.replace("_", " "))
    degree, modality = _degree_modality(frame)
    bits = [program.get("name") or "", program.get("official_domain") or "", phrase]
    if isinstance(degree, str):
        bits.append(degree.replace("_", " "))
    if isinstance(modality, str):
        bits.append(modality.replace("_", " "))
    for name in ("regions", "intake_windows", "field_tags"):
        values = getattr(frame, name, None) or []
        if isinstance(values, (list, tuple)) and values:
            bits.append(str(values[0]).replace("_", " "))
            break
    text = " ".join(bit.strip() for bit in bits if isinstance(bit, str) and bit.strip())
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _QUERY_MAX:
        text = text[:_QUERY_MAX].rstrip()
    return text


def gap_proposals(store, goal: Any, coverage: Any) -> list:
    """Deterministic search templates for unknown critical public fields."""
    runnable = _runnable(store, goal)
    frame = runnable.frame
    if not _anchor_tokens(frame):
        raise PlannerError("frame", "unknown scope is not worldwide")
    conn = _db(store)
    now = _now()
    critical = set(_critical_fields(runnable.contract))
    proposals = []
    seen = set()
    for item in _coverage_programs(coverage):
        role = _program_role(conn, runnable.goal.id, runnable.contract.version, item["program_key"])
        if role is None or role["visibility"] != "public" or role["role"] != "candidate":
            continue
        program = _program(conn, item["program_key"])
        if program is None or program["provenance"] != "observed":
            continue
        fields = []
        for field in list(item["unknown_fields"]) + list(item["contradictions"]):
            if field in critical and field not in fields:
                fields.append(field)
        for field_key in fields:
            if _cooldown_active(conn, runnable.goal.id, runnable.contract.version, item["program_key"], field_key, now):
                continue
            query = _compose_gap_query(frame, program, field_key)
            if len(query) < _QUERY_MIN:
                continue
            identity = ("search", item["program_key"], field_key, query)
            if identity in seen:
                continue
            seen.add(identity)
            proposals.append(Proposal(
                "search", item["program_key"], field_key, query, None,
                f"official evidence for {field_key}", "gap",
            ))
    if not proposals and not _coverage_programs(coverage):
        anchors = []
        for name in ("field_tags", "regions", "intake_windows"):
            values = getattr(frame, name, None) or []
            if isinstance(values, (list, tuple)) and values:
                anchors.append(str(values[0]))
        query = " ".join(anchors + ["official graduate programme"]).strip()
        if _QUERY_MIN <= len(query) <= _QUERY_MAX:
            proposals.append(Proposal(
                "search", None, None, query, None, "scoped public discovery", "gap",
            ))
    return proposals


def _binding_dict(raw: Mapping[str, Any]) -> dict:
    if not isinstance(raw, Mapping):
        raise PlannerError("binding", "expected object")
    allowed = {"goal_id", "contract_version", "strategy_version", "epoch"}
    extra = set(raw) - allowed
    if extra:
        raise PlannerError("binding", "unknown binding fields: " + ",".join(sorted(extra)))
    try:
        binding = {
            "goal_id": int(raw["goal_id"]),
            "contract_version": int(raw["contract_version"]),
            "strategy_version": int(raw["strategy_version"]),
            "epoch": int(raw["epoch"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PlannerError("binding", "goal, contract, strategy, and epoch are required") from exc
    return binding


def _check_binding(db, binding: Mapping[str, Any]) -> None:
    ok = _goal().binding_valid(
        db,
        int(binding["goal_id"]),
        int(binding["contract_version"]),
        int(binding["strategy_version"]),
        int(binding["epoch"]),
    )
    if ok is not True:
        raise PlannerError("binding", "goal binding is not valid")


def planner_proposals(engine: Any, frame: Any, coverage: Any, budget: Mapping[str, Any]) -> list:
    """One engine.plan call. The database handle is not forwarded."""
    public = _require_frame(frame)
    if not isinstance(budget, Mapping):
        raise PlannerError("budget", "expected object")
    db = budget.get("db")
    if db is None or not hasattr(db, "execute"):
        raise PlannerError("binding", "budget db connection is required")
    binding = _binding_dict(budget.get("binding") or {})
    _check_binding(db, binding)
    operation_key = budget.get("operation_key")
    if not isinstance(operation_key, str) or not operation_key.strip() or any(ch.isspace() for ch in operation_key):
        raise PlannerError("operation_key", "operation_key is required")
    max_out = budget.get("max_out")
    if isinstance(max_out, bool) or not isinstance(max_out, int) or max_out < 1:
        raise PlannerError("max_out", "positive integer required")
    exploration_budget = budget.get("exploration_budget", PLAN_MAX_HYPOTHESES)
    if isinstance(exploration_budget, bool) or not isinstance(exploration_budget, int) or exploration_budget < 0:
        raise PlannerError("exploration_budget", "non-negative integer required")
    store = budget.get("store")
    if store is not None:
        summary = public_coverage_summary(store, _runnable(store, budget.get("goal")), coverage) if budget.get("goal") is not None else _whitelist_summary(coverage)
    else:
        summary = _whitelist_summary(coverage)
    plan = getattr(engine, "plan", None)
    if not callable(plan):
        raise PlannerOutage("engine", "plan unavailable")
    try:
        reply = plan(public, summary, int(exploration_budget), dict(binding), operation_key, int(max_out))
    except PlannerOutage:
        raise
    except Exception as exc:
        raise PlannerOutage("engine", "plan failed") from exc
    text = reply.get("text") if isinstance(reply, Mapping) else getattr(reply, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise PlannerOutage("engine", "empty plan reply")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerOutage("engine", "plan reply is not the hypothesis object") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"hypotheses"} or not isinstance(parsed.get("hypotheses"), list):
        raise PlannerOutage("engine", "plan reply is not the hypothesis object")
    known_programs = {item["program_key"] for item in summary["programs"]}
    proposals = []
    for row in parsed["hypotheses"]:
        proposal = _hypothesis_proposal(row, known_programs)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def _hypothesis_proposal(row: Any, known_programs: set) -> Optional[Proposal]:
    if not isinstance(row, Mapping):
        return None
    allowed = {"kind", "program_key", "field_key", "query", "url", "rationale"}
    if set(row) - allowed:
        return None
    kind = row.get("kind")
    if kind not in ("search", "fetch"):
        return None
    program_key = row.get("program_key")
    field_key = row.get("field_key")
    query = row.get("query")
    url = row.get("url")
    rationale = row.get("rationale")
    if program_key is not None and (known_programs is None or not isinstance(program_key, str)):
        return None
    if field_key is not None and not isinstance(field_key, str):
        return None
    if query is not None and not isinstance(query, str):
        return None
    if url is not None and not isinstance(url, str):
        return None
    if not isinstance(rationale, str):
        return None
    if kind == "search" and (not isinstance(query, str) or not query.strip()):
        return None
    if kind == "fetch" and (not isinstance(url, str) or not url.strip()):
        return None
    return Proposal(
        kind,
        program_key,
        field_key,
        query.strip() if isinstance(query, str) and query.strip() else None,
        url.strip() if isinstance(url, str) and url.strip() else None,
        rationale[:_RATIONALE_MAX],
        "hypothesis",
    )


def _canonical_url(raw: str) -> Optional[str]:
    return _orch().canonical_url(raw)


def _norm_query(raw: str) -> str:
    return _orch().norm_query(raw)


def _discovered(conn, goal_id: int, canon: str) -> bool:
    row = _one(
        conn,
        """SELECT 1 AS n FROM pages p
             JOIN goal_pages gp ON gp.page_id=p.id AND gp.goal_id=?
             WHERE p.canon_url=?
           UNION ALL
           SELECT 1 FROM page_links pl
             JOIN goal_pages gp ON gp.page_id=pl.from_page_id AND gp.goal_id=?
             WHERE pl.to_canon_url=?
           UNION ALL
           SELECT 1 FROM hub_rows hr
             JOIN goal_pages gp ON gp.page_id=hr.hub_page_id AND gp.goal_id=?
             WHERE hr.to_canon_url=?
           UNION ALL
           SELECT 1 FROM pages p
             WHERE p.canon_url=? AND p.topic_id IN (
               SELECT topic_id FROM goal_topics WHERE goal_id=? AND visibility='public')
           LIMIT 1""",
        (goal_id, canon, goal_id, canon, goal_id, canon, canon, goal_id),
    )
    return row is not None


def _url_safety(url: str) -> Optional[str]:
    if len(url) > _URL_MAX:
        return "url is too long"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "url scheme is not http"
    if parts.username or parts.password:
        return "url contains credentials"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return "url host is missing"
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata.internal"}:
        return "url host is internal"
    if host.endswith(".local") or host.endswith(".internal") or host.endswith(".localhost"):
        return "url host is internal"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
        or addr.is_multicast or addr.is_unspecified
    ):
        return "url address is reserved"
    return None


def validate_proposals(store, goal: Any, proposals: Sequence[Any]) -> list:
    """Scope, anchor, discovery, and dedupe checks. Eligibility is scheduled separately."""
    runnable = _runnable(store, goal)
    frame = runnable.frame
    conn = _db(store)
    if not isinstance(proposals, Sequence) or isinstance(proposals, (str, bytes)):
        raise PlannerError("proposals", "expected a sequence")
    validated = []
    seen = set()
    anchors = _anchor_tokens(frame)
    if not anchors:
        raise PlannerError("frame", "unknown scope is not worldwide")
    for proposal in proposals:
        if not isinstance(proposal, Proposal):
            blank = Proposal("", None, None, None, None, "", "")
            validated.append(ValidatedProposal(blank, None, None, "dropped", "not a proposal"))
            continue
        reason = _validate_one(conn, runnable, frame, proposal, anchors, seen)
        if reason is not None:
            validated.append(ValidatedProposal(proposal, None, None, "dropped", reason))
            continue
        identity = (proposal.kind, proposal.program_key, proposal.field_key, proposal.query, proposal.url)
        seen.add(identity)
        validated.append(ValidatedProposal(proposal, None, None, "valid", "public and in scope"))
    return validated


def _validate_one(conn, runnable, frame, proposal: Proposal, anchors: set, seen: set) -> Optional[str]:
    if proposal.kind not in ("search", "fetch"):
        return "kind is not search or fetch"
    if proposal.origin not in ("gap", "hypothesis"):
        return "origin is not gap or hypothesis"
    if not isinstance(proposal.rationale, str) or len(proposal.rationale) > _RATIONALE_MAX:
        return "rationale is not a short string"
    if proposal.field_key is not None and proposal.field_key not in _field_keys():
        return "field is not a known public field"
    if proposal.kind == "search" and not proposal.query:
        return "search requires a query"
    if proposal.kind == "fetch" and not proposal.url:
        return "fetch requires a url"
    identity = (proposal.kind, proposal.program_key, proposal.field_key, proposal.query, proposal.url)
    if identity in seen:
        return "duplicate proposal"
    program = None
    if proposal.program_key is not None:
        if not isinstance(proposal.program_key, str) or len(proposal.program_key) > _KEY_MAX:
            return "program_key is invalid"
        role = _program_role(conn, runnable.goal.id, runnable.contract.version, proposal.program_key)
        if role is None or role["visibility"] != "public":
            return "programme is not a public approved association"
        program = _program(conn, proposal.program_key)
        if program is None or program["provenance"] != "observed":
            return "programme is not observed"
        conflict = _scope_conflict("", frame, program)
        if conflict:
            return conflict
    blob = " ".join(bit for bit in (proposal.query, proposal.url, proposal.rationale) if bit)
    if _credential(blob):
        return "query contains credential material"
    if proposal.query:
        if not _QUERY_MIN <= len(proposal.query) <= _QUERY_MAX:
            return "query length is outside 8 to 200 characters"
        query_anchors = set(anchors)
        if program:
            query_anchors |= _anchor_tokens(frame, program)
        if not (set(_tokens(proposal.query)) & query_anchors):
            return "query has no public anchor"
        conflict = _scope_conflict(proposal.query, frame, program)
        if conflict:
            return conflict
    if proposal.url:
        safety = _url_safety(proposal.url)
        if safety:
            return safety
        canon = _canonical_url(proposal.url)
        if not canon:
            return "url cannot be canonicalised"
        if not _discovered(conn, runnable.goal.id, canon):
            return "url is not a discovered public source"
        conflict = _scope_conflict(canon, frame, program)
        if conflict:
            return conflict
    return None


def _share_value(limits: Mapping[str, Any]) -> Decimal:
    raw = limits.get("exploration_share", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise PlannerError("exploration_share", "exploration share is missing")
    try:
        share = Decimal(str(raw))
    except Exception as exc:
        raise PlannerError("exploration_share", "exploration share is invalid") from exc
    if share < 0 or share > 1:
        raise PlannerError("exploration_share", "exploration share must be from 0 to 1")
    return share


def _share_limit(share: Decimal, slots: int, critical_gaps: bool) -> int:
    if slots <= 0:
        return 0
    if not critical_gaps:
        return slots
    amount = (share * Decimal(slots)).to_integral_value(ROUND_FLOOR)
    return max(0, int(amount))


def _authority(host: str, official: Optional[str], kind: Optional[str]) -> float:
    host = (host or "").lower()
    official_host = (official or "").lower().rstrip(".")
    if official_host and (host == official_host or host.endswith("." + official_host)):
        return 1.0
    if "scholarship" in host or "fellowship" in host or host.endswith("grants.gov"):
        return 0.9
    if host in _SOCIAL_HOSTS or host.startswith("forum.") or ".forum." in host:
        return 0.3
    if kind in {"hub", "article"}:
        return 0.5
    return 0.5


def _novelty(conn, page_id: int) -> float:
    row = _one(
        conn,
        """SELECT CASE WHEN p.fetch_count = 0
                      THEN MAX(0.3, 1.0 - 0.02 * d.fetch_count) * (CASE WHEN d.junk_count > 0 THEN 0.5 ELSE 1.0 END)
                      ELSE (COALESCE(h.rows_added_total, 0) + 1.0) / (p.fetch_count + 2.0) END AS novelty
             FROM pages p
             JOIN domains d ON d.domain=p.domain
             LEFT JOIN hubs h ON h.page_id=p.id
             WHERE p.id=?""",
        (page_id,),
    )
    if row is None or row["novelty"] is None:
        return 0.0
    return float(row["novelty"])


def _freshness(conn, page_id: Optional[int], now: int) -> float:
    if page_id is None:
        return 1.0
    page = _one(conn, "SELECT next_check_at FROM pages WHERE id=?", (page_id,))
    snapshot = _one(
        conn,
        """SELECT 1 AS n FROM source_snapshots
             WHERE page_id=? AND visibility='public' AND superseded_by IS NULL LIMIT 1""",
        (page_id,),
    )
    if snapshot is None or page is None or int(page["next_check_at"]) <= now:
        return 1.0
    return 0.3


def _score(goal_value: float, uncertainty: float, authority: float, freshness: float, cost: float) -> float:
    return goal_value * uncertainty * authority * freshness / cost


def _ensure_family(conn) -> None:
    conn.execute(
        """INSERT INTO families(name, recheck_days, weight, relevance_floor, focus)
           VALUES ('opportunities', 14, 1.0, 0.08, 'public programme funding')
           ON CONFLICT(name) DO NOTHING"""
    )


def _topic_id(conn, store, runnable) -> int:
    row = _one(
        conn,
        """SELECT topic_id FROM goal_topics
             WHERE goal_id=? AND contract_version=? AND visibility='public'
             ORDER BY topic_id LIMIT 1""",
        (runnable.goal.id, runnable.contract.version),
    )
    if row is not None:
        return int(row["topic_id"])
    _ensure_family(conn)
    frame = runnable.frame
    bits = []
    for name in ("field_tags", "regions", "intake_windows"):
        values = getattr(frame, name, None) or []
        if isinstance(values, (list, tuple)) and values:
            bits.append(str(values[0]))
    title = " ".join(bits) or "public programme discovery"
    key = "scope-" + _digest({"goal": runnable.goal.id, "title": title})[:24]
    locks = _orch().LockEngine(store)
    _verdict, topic_id = locks.topic_lock(conn, title[:180], "public programme discovery", "opportunities", "hub", key=key)
    conn.execute(
        """INSERT OR IGNORE INTO goal_topics(goal_id, contract_version, topic_id, role, visibility)
           VALUES (?, ?, ?, 'candidate', 'public')""",
        (runnable.goal.id, runnable.contract.version, int(topic_id)),
    )
    return int(topic_id)


def _page_row(conn, canon: str):
    return _one(conn, "SELECT * FROM pages WHERE canon_url=?", (canon,))


def _eligibility(conn, page_id: int, now: int, per_domain: int) -> Optional[str]:
    row = _one(
        conn,
        """SELECT p.id
             FROM pages p
             JOIN domains d ON d.domain=p.domain
             WHERE p.id=?
               AND p.status NOT IN ('walled','dead','dropped','quarantined')
               AND p.quarantined=0
               AND COALESCE(p.wall_kind,'')!='robots'
               AND p.next_check_at<=?
               AND (p.next_attempt_at IS NULL OR p.next_attempt_at<=?)
               AND d.status='ok'
               AND (d.paused_until IS NULL OR d.paused_until<=?)
               AND (SELECT COUNT(*) FROM domain_slots s WHERE s.domain=p.domain)<?""",
        (page_id, now, now, now, per_domain),
    )
    if row is not None:
        return None
    diag = _one(
        conn,
        """SELECT p.status, p.wall_kind, p.quarantined, p.next_check_at, p.next_attempt_at,
                  d.domain, d.status AS domain_status, d.paused_until, d.robots_txt,
                  (SELECT COUNT(*) FROM domain_slots s WHERE s.domain=p.domain) AS leases
             FROM pages p JOIN domains d ON d.domain=p.domain WHERE p.id=?""",
        (page_id,),
    )
    if diag is None:
        return "dropped: page is missing"
    if diag["quarantined"] or diag["status"] == "quarantined":
        return "dropped: page is quarantined"
    if diag["wall_kind"] == "robots" or diag["status"] == "walled":
        return "dropped: robots or wall blocks the page"
    if diag["status"] in {"dead", "dropped"}:
        return f"dropped: page is {diag['status']}"
    if diag["domain_status"] != "ok" or (diag["paused_until"] is not None and int(diag["paused_until"]) > now):
        until = diag["paused_until"] if diag["paused_until"] is not None else "unspecified"
        return f"deferred: domain {diag['domain']} paused until {until}"
    if int(diag["next_check_at"]) > now or (diag["next_attempt_at"] is not None and int(diag["next_attempt_at"]) > now):
        return "deferred: page is not due"
    if int(diag["leases"]) >= per_domain:
        return f"deferred: domain {diag['domain']} lease cap {per_domain} reached"
    robots = diag["robots_txt"] or ""
    if robots and _robots_disallow(robots, "/"):
        return "dropped: robots disallow the page"
    return "deferred: page is not eligible"


def _robots_disallow(robots_txt: str, path: str) -> bool:
    active = False
    rules = []
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            active = value == "*"
        elif active and key == "disallow" and value:
            rules.append(value)
    path = path or "/"
    return any(path.startswith(rule) for rule in rules)


def _assign_depth(conn, goal_id: int, version: int, page_id: int) -> int:
    existing = _one(
        conn,
        """SELECT depth FROM goal_pages
             WHERE goal_id=? AND contract_version=? AND page_id=?""",
        (goal_id, version, page_id),
    )
    if existing is not None:
        return int(existing["depth"])
    parent = _one(conn, "SELECT parent_page_id FROM pages WHERE id=?", (page_id,))
    parent_id = None if parent is None else parent["parent_page_id"]
    if parent_id is None:
        return 0
    parent_depth = _one(
        conn,
        """SELECT depth FROM goal_pages
             WHERE goal_id=? AND contract_version=? AND page_id=?""",
        (goal_id, version, parent_id),
    )
    if parent_depth is None:
        return 0
    return int(parent_depth["depth"]) + 1


def _operation_key(goal_id: int, wave_id: int, node_kind: str, node_key: str) -> str:
    digest = hashlib.sha256(f"{goal_id}|{wave_id}|{node_kind}|{node_key}".encode()).hexdigest()[:32]
    return "plan-" + digest


def _insert_decision(conn, *, wave_id, goal_id, strategy_version, node_kind, node_id, decision, score, components, reason, now) -> int:
    cur = conn.execute(
        """INSERT INTO frontier_decisions
             (wave_id, goal_id, strategy_version, node_kind, node_id, decision, score, components_json, reason, at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            wave_id, goal_id, strategy_version, node_kind, int(node_id),
            decision, score, _canonical(components), reason[:500], now,
        ),
    )
    return int(cur.lastrowid)


def _ensure_wave(conn, wave_id: int, now: int) -> None:
    conn.execute(
        "INSERT INTO waves(id, started_at, status) VALUES (?, ?, 'running') ON CONFLICT(id) DO NOTHING",
        (wave_id, now),
    )


def _headroom_fetches(store, grant_id: int, now: int) -> int:
    report = _ledger().headroom(store, grant_id, now)
    counters = report.get("day", {}).get("counters", {})
    fetches = counters.get("fetches") or {}
    return int(fetches.get("remaining") or 0)


def _open_queue(conn, goal_id: int) -> int:
    row = _one(
        conn,
        """SELECT COUNT(*) AS n FROM goal_node_identities
             WHERE goal_id=? AND queued_at IS NOT NULL AND visited_at IS NULL""",
        (goal_id,),
    )
    return int(row["n"])


def _stale_tasks(conn, goal_id: int, epoch: int) -> list:
    return _all(
        conn,
        """SELECT id, epoch, kind, payload FROM tasks
             WHERE goal_id=? AND epoch!=? AND status IN ('pending','claimed')""",
        (goal_id, epoch),
    )


def _payload_ids(payload: str) -> dict:
    try:
        parsed = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _commit_strategy(conn, runnable, wave_id: int, substate: str, reason: str, exhausted: list, now: int) -> None:
    goal_id = runnable.goal.id
    version = runnable.contract.version
    row = _one(conn, "SELECT current_strategy_version FROM goals WHERE id=?", (goal_id,))
    current = int(row["current_strategy_version"])
    nxt = current + 1
    unknown = _unknown_critical(conn, runnable)
    focus = []
    for program_key, fields in sorted(unknown.items()):
        for field in fields:
            focus.append({"program_key": program_key, "field_key": field})
    body = {
        "exhausted": list(exhausted),
        "focus": focus[:20],
        "moved": [item[1] for item in _kept_field_hashes(conn, goal_id, version)],
        "substate": substate,
    }
    conn.execute(
        """INSERT INTO strategies(goal_id, version, contract_version, body_json, outcome, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (goal_id, nxt, version, _canonical(body), substate, reason[:500], now),
    )
    updated = conn.execute(
        "UPDATE goals SET current_strategy_version=? WHERE id=? AND current_strategy_version=?",
        (nxt, goal_id, current),
    )
    if updated.rowcount != 1:
        raise PlannerError("strategy", "strategy version changed")
    previous = _one(conn, "SELECT stall_count FROM goal_runtime WHERE goal_id=?", (goal_id,))
    stall = int(previous["stall_count"]) + 1 if previous is not None and substate == "stalled" else (1 if substate == "stalled" else 0)
    conn.execute(
        """INSERT INTO goal_runtime
             (goal_id, substate, reason, work_fingerprint, coverage_fingerprint, stall_count, last_wave_id, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(goal_id) DO UPDATE SET
             substate=excluded.substate, reason=excluded.reason,
             work_fingerprint=excluded.work_fingerprint,
             coverage_fingerprint=excluded.coverage_fingerprint,
             stall_count=excluded.stall_count, last_wave_id=excluded.last_wave_id,
             updated_at=excluded.updated_at""",
        (
            goal_id, substate, reason[:500], work_fingerprint_conn(conn, runnable),
            coverage_fingerprint_conn(conn, runnable), stall, wave_id, now,
        ),
    )


def work_fingerprint_conn(conn, runnable) -> str:
    rows = _all(
        conn,
        """SELECT node_kind, node_key, visited_at FROM goal_node_identities
             WHERE goal_id=? ORDER BY node_kind, node_key""",
        (runnable.goal.id,),
    )
    material = [
        {"visited": row["visited_at"] is not None, "kind": row["node_kind"], "key": row["node_key"]}
        for row in rows
    ]
    return _digest(material)


def coverage_fingerprint_conn(conn, runnable) -> str:
    unknown = _unknown_critical(conn, runnable)
    material = {
        "kept": _kept_field_hashes(conn, runnable.goal.id, runnable.contract.version),
        "unknown": [[key, fields] for key, fields in sorted(unknown.items())],
    }
    return _digest(material)


def _outcome(conn, runnable, selected: int, valid: int, exhausted: list, critical_gaps: bool) -> tuple:
    previous = _one(
        conn,
        "SELECT work_fingerprint, coverage_fingerprint FROM goal_runtime WHERE goal_id=?",
        (runnable.goal.id,),
    )
    work = work_fingerprint_conn(conn, runnable)
    coverage = coverage_fingerprint_conn(conn, runnable)
    if previous is not None and previous["work_fingerprint"] == work and previous["coverage_fingerprint"] == coverage:
        return "stalled", "repeated work produced no new supported facts"
    if previous is not None and previous["coverage_fingerprint"] != coverage:
        return "progressing", "supported public facts changed"
    if exhausted:
        return "exhausted", "grant exhausted for " + exhausted[0]
    if valid == 0 and selected == 0:
        return "empty_frontier", "no eligible node and no valid proposal"
    if critical_gaps and selected == 0:
        return "partial_coverage", "critical fields remain and no eligible node was admitted"
    if critical_gaps:
        return "partial_coverage", "queued distinct targets without new supported facts"
    if selected == 0:
        return "no_match", "no matching public evidence was scheduled"
    return "partial_coverage", "queued distinct targets without new supported facts"


def schedule(store, goal: Any, validated: Sequence[Any], wave_id: int) -> SchedulePlan:
    """Reserve an affordable eligible frontier and record every decision."""
    if isinstance(wave_id, bool) or not isinstance(wave_id, int) or wave_id < 1:
        raise PlannerError("wave_id", "positive integer required")
    if not isinstance(validated, Sequence) or isinstance(validated, (str, bytes)):
        raise PlannerError("validated", "expected a sequence")
    runnable = _runnable(store, goal)
    now = _now()
    selected_queries = []
    selected_pages = []
    decision_ids = []
    reservation_ids = []
    exhausted = []
    with store.tx() as conn:
        goal_row = _one(
            conn,
            """SELECT id, state, epoch, current_contract_version, current_strategy_version
                 FROM goals WHERE id=?""",
            (runnable.goal.id,),
        )
        if goal_row is None:
            raise PlannerError("goal", "missing")
        binding = {
            "goal_id": int(goal_row["id"]),
            "contract_version": int(goal_row["current_contract_version"]),
            "strategy_version": int(goal_row["current_strategy_version"]),
            "epoch": int(goal_row["epoch"]),
        }
        _check_binding(conn, binding)
        if int(runnable.contract.version) != binding["contract_version"]:
            raise PlannerError("contract_version", "runnable contract is stale")
        grant = runnable.grant
        if int(grant.id) <= 0 or grant.state != "active":
            raise PlannerError("grant", "active grant required")
        limits = grant.limits
        max_depth = int(limits["max_depth"])
        max_queue = int(limits["max_queue"])
        per_domain = int(limits["per_domain_inflight"])
        max_hypotheses = int(limits["max_hypotheses_per_wave"])
        share = _share_value(limits)
        _ensure_wave(conn, wave_id, now)
        unknown = _unknown_critical(conn, runnable)
        critical_gaps = any(fields for fields in unknown.values())
        critical_total = max(len(_critical_fields(runnable.contract)), 1)
        remaining = _headroom_fetches(store, int(grant.id), now)
        room = max(0, max_queue - _open_queue(conn, binding["goal_id"]))
        page_slots = min(remaining, room)
        exploration_cap = _share_limit(share, page_slots, critical_gaps)
        for stale in _stale_tasks(conn, binding["goal_id"], binding["epoch"]):
            payload = _payload_ids(stale["payload"])
            node_kind = "page" if stale["kind"] == "fetch" else "query"
            node_id = payload.get("page_id") if node_kind == "page" else payload.get("query_id")
            decision_ids.append(_insert_decision(
                conn,
                wave_id=wave_id,
                goal_id=binding["goal_id"],
                strategy_version=binding["strategy_version"],
                node_kind=node_kind,
                node_id=int(node_id or 0),
                decision="dropped",
                score=None,
                components={"stale_epoch": int(stale["epoch"]), "epoch": binding["epoch"], "paid": 0},
                reason=f"dropped: stale epoch {int(stale['epoch'])} is not current epoch {binding['epoch']}",
                now=now,
            ))
        prepared = []
        dropped_early = []
        for index, item in enumerate(validated):
            if not isinstance(item, ValidatedProposal):
                raise PlannerError("validated", "expected ValidatedProposal")
            if item.decision != "valid":
                dropped_early.append((index, item))
                continue
            prepared.append(_prepare(conn, runnable, binding, item, index, now, unknown, critical_total))
        for index, item in dropped_early:
            proposal = item.proposal if isinstance(item.proposal, Proposal) else Proposal("", None, None, None, None, "", "")
            node_kind = "page" if proposal.kind == "fetch" else "query"
            decision_ids.append(_insert_decision(
                conn,
                wave_id=wave_id,
                goal_id=binding["goal_id"],
                strategy_version=binding["strategy_version"],
                node_kind=node_kind,
                node_id=0,
                decision="dropped",
                score=None,
                components={"paid": 0, "node_key": proposal.url or proposal.query or "", "origin": proposal.origin},
                reason="dropped: " + (item.reason or "rejected"),
                now=now,
            ))
        prepared.sort(key=lambda row: (-row["score"], -row["novelty"], row["node_key"]))
        hypotheses_used = 0
        exploration_used = 0
        admitted = 0
        for row in prepared:
            decision, reason, components, reservations, query_id, page_id = _admit_one(
                conn, store, runnable, binding, row, wave_id, now, max_depth, per_domain,
                max_hypotheses, exploration_cap, hypotheses_used, exploration_used, critical_gaps,
                page_slots, share,
            )
            if decision == "selected" and row["origin"] == "hypothesis":
                hypotheses_used += 1
            if decision == "selected" and row["goal_value"] == 0.6 and row["kind"] == "fetch":
                exploration_used += 1
            if decision == "selected":
                admitted += 1
            if reason.startswith("grant exhausted for "):
                exhausted.append(reason.split(" for ", 1)[1].split(":", 1)[0])
            node_id = page_id if row["kind"] == "fetch" else query_id
            decision_ids.append(_insert_decision(
                conn,
                wave_id=wave_id,
                goal_id=binding["goal_id"],
                strategy_version=binding["strategy_version"],
                node_kind="page" if row["kind"] == "fetch" else "query",
                node_id=int(node_id or 0),
                decision=decision,
                score=row["score"],
                components=components,
                reason=reason,
                now=now,
            ))
            reservation_ids.extend(reservations)
            if decision == "selected" and query_id is not None:
                selected_queries.append(int(query_id))
            if decision == "selected" and page_id is not None:
                selected_pages.append(int(page_id))
        substate, outcome_reason = _outcome(conn, runnable, admitted, len(prepared), exhausted, critical_gaps)
        _commit_strategy(conn, runnable, wave_id, substate, outcome_reason, exhausted, now)
    return SchedulePlan(tuple(selected_queries), tuple(selected_pages), tuple(decision_ids), tuple(reservation_ids))


def _prepare(conn, runnable, binding, item: ValidatedProposal, index: int, now: int, unknown: dict, critical_total: int) -> dict:
    proposal = item.proposal
    program = _program(conn, proposal.program_key) if proposal.program_key else None
    role = _program_role(conn, binding["goal_id"], binding["contract_version"], proposal.program_key) if proposal.program_key else None
    canon = _canonical_url(proposal.url) if proposal.url else None
    page = _page_row(conn, canon) if canon else None
    page_id = None if page is None else int(page["id"])
    host = ""
    kind = None
    if page is not None:
        host = page["domain"] or ""
        kind = page["kind"] or page["predicted_kind"]
    elif canon:
        host = (urlsplit(canon).hostname or "").lower()
    official = program.get("official_domain") if program else None
    authority = _authority(host, official, kind) if proposal.kind == "fetch" else (1.0 if official else 0.5)
    missing = unknown.get(proposal.program_key, []) if proposal.program_key else []
    targets_gap = bool(proposal.field_key and proposal.field_key in missing) or bool(
        proposal.kind == "fetch" and official and host and authority == 1.0 and missing
    )
    shortlisted = role is not None and role["role"] == "candidate"
    if shortlisted and targets_gap:
        goal_value = 1.0
    elif proposal.program_key is None or proposal.origin == "hypothesis":
        goal_value = 0.6
    else:
        goal_value = 0.2
    unknown_n = len(missing) if proposal.program_key else 0
    uncertainty = 0.1 + 0.9 * (unknown_n / critical_total)
    freshness = _freshness(conn, page_id, now)
    estimated_tokens = 0
    paid = 0
    cost = 1 + estimated_tokens / 100000 + paid
    score = _score(goal_value, uncertainty, authority, freshness, cost)
    novelty = _novelty(conn, page_id) if page_id is not None else 0.0
    node_key = canon or (_norm_query(proposal.query) if proposal.query else f"item-{index}")
    return {
        "index": index,
        "item": item,
        "kind": proposal.kind,
        "origin": proposal.origin,
        "program_key": proposal.program_key,
        "field_key": proposal.field_key,
        "canon": canon,
        "page_id": page_id,
        "host": host,
        "goal_value": goal_value,
        "uncertainty": uncertainty,
        "authority": authority,
        "freshness_need": freshness,
        "cost": cost,
        "paid": paid,
        "estimated_tokens": estimated_tokens,
        "score": score,
        "novelty": novelty,
        "node_key": node_key,
        "rationale": proposal.rationale,
    }


def _components(row: Mapping[str, Any], *, page_slots: int, exploration_cap: int, critical_gaps: bool, share: Decimal, extra: Optional[dict] = None) -> dict:
    payload = {
        "goal_value": row["goal_value"],
        "uncertainty": row["uncertainty"],
        "authority": row["authority"],
        "freshness_need": row["freshness_need"],
        "cost": row["cost"],
        "paid": 0,
        "estimated_tokens": 0,
        "novelty": row["novelty"],
        "score": row["score"],
        "node_key": row["node_key"],
        "origin": row["origin"],
        "page_slots": page_slots,
        "exploration_cap": exploration_cap,
        "critical_gaps": critical_gaps,
        "exploration_share": str(share),
        "model_rationale": row["rationale"][:_RATIONALE_MAX],
    }
    if extra:
        payload.update(extra)
    return payload


def _admit_one(
    conn, store, runnable, binding, row, wave_id, now, max_depth, per_domain,
    max_hypotheses, exploration_cap, hypotheses_used, exploration_used, critical_gaps,
    page_slots, share,
):
    proposal = row["item"].proposal
    base = _components(row, page_slots=page_slots, exploration_cap=exploration_cap, critical_gaps=critical_gaps, share=share)
    if proposal.program_key and proposal.field_key and _cooldown_active(
        conn, binding["goal_id"], binding["contract_version"], proposal.program_key, proposal.field_key, now
    ):
        return "deferred", "deferred: program-field cooldown is active", base, [], None, row["page_id"]
    if row["origin"] == "hypothesis" and hypotheses_used >= max_hypotheses:
        return "deferred", "deferred: hypothesis cap reached", base, [], None, row["page_id"]
    if row["kind"] == "fetch" and row["goal_value"] == 0.6 and critical_gaps and exploration_used >= exploration_cap:
        return "deferred", "deferred: exploration share exhausted while critical fields remain", base, [], None, row["page_id"]
    stale = _stale_for_node(conn, binding["goal_id"], binding["epoch"], row)
    if stale is not None:
        return "dropped", f"dropped: stale epoch {stale} is not current epoch {binding['epoch']}", base, [], None, row["page_id"]
    if proposal.kind == "fetch" and row["canon"] and not _discovered(conn, binding["goal_id"], row["canon"]) and row["page_id"] is None:
        return "dropped", "dropped: url is not a discovered public source", base, [], None, None
    op_key = _operation_key(binding["goal_id"], wave_id, "page" if proposal.kind == "fetch" else "query", row["node_key"])
    existing = _one(
        conn,
        "SELECT id, state FROM resource_operations WHERE goal_id=? AND operation_key=?",
        (binding["goal_id"], op_key),
    )
    if existing is not None:
        return "deferred", "deferred: admission replay is not a new dispatch", base, [], None, row["page_id"]
    save = f"planner_item_{int(row['index'])}"
    conn.execute(f"SAVEPOINT {save}")
    try:
        topic_id = _topic_id(conn, store, runnable)
        query_id, page_id, depth = _link(conn, store, runnable, binding, proposal, row, topic_id, now)
        if proposal.kind == "fetch" and page_id is not None:
            eligibility = _eligibility(conn, page_id, _now(), per_domain)
            if eligibility is not None:
                conn.execute(f"ROLLBACK TO SAVEPOINT {save}")
                conn.execute(f"RELEASE SAVEPOINT {save}")
                decision = "deferred" if eligibility.startswith("deferred") else "dropped"
                return decision, eligibility, base, [], None, page_id if decision == "deferred" else page_id
            path = urlsplit(row["canon"] or "").path or "/"
            robots = _one(conn, "SELECT robots_txt FROM domains WHERE domain=?", (row["host"],))
            if robots and robots["robots_txt"] and _robots_disallow(robots["robots_txt"], path):
                conn.execute(f"ROLLBACK TO SAVEPOINT {save}")
                conn.execute(f"RELEASE SAVEPOINT {save}")
                return "dropped", "dropped: robots disallow the page", base, [], None, page_id
        if depth > max_depth:
            conn.execute(f"ROLLBACK TO SAVEPOINT {save}")
            conn.execute(f"RELEASE SAVEPOINT {save}")
            return "dropped", "dropped: depth exceeds max_depth", {**base, "depth": depth}, [], query_id, page_id
        node_kind = "page" if proposal.kind == "fetch" else "query"
        node_key = row["canon"] if node_kind == "page" else _norm_query(proposal.query or "")
        task_id = _insert_task(conn, runnable, binding, proposal, wave_id, now, op_key, query_id, page_id, row["score"])
        lease = secrets.token_hex(16)
        lease_until = _now() + LEASE_SEC
        amounts = {"fetches": 1, "queued_nodes": 1} if node_kind == "page" else {"queries": 1, "queued_nodes": 1}
        reserve_binding = {
            "goal_id": binding["goal_id"],
            "contract_version": binding["contract_version"],
            "strategy_version": binding["strategy_version"],
            "epoch": binding["epoch"],
            "node_kind": node_kind,
            "node_key": node_key,
            "phase": "queued",
            "depth": int(depth),
        }
        if node_kind == "page":
            reserve_binding.update({
                "domain": row["host"],
                "task_id": task_id,
                "lease_token": lease,
                "lease_until": lease_until,
            })
        reserved = _ledger().reserve_batch(store, int(runnable.grant.id), op_key, amounts, reserve_binding)
        conn.execute("UPDATE tasks SET status='claimed' WHERE id=? AND status='pending'", (task_id,))
        if proposal.program_key and proposal.field_key:
            _remember_cooldown(conn, binding["goal_id"], binding["contract_version"], proposal.program_key, proposal.field_key, now)
        conn.execute(f"RELEASE SAVEPOINT {save}")
        field = proposal.field_key or "coverage"
        name = row["program_key"] or "a new candidate"
        reason = f"selected: {field} unknown for {name}, authority {row['authority']}, paid 0"
        return "selected", reason, {**base, "depth": depth}, [int(item) for item in reserved], query_id, page_id
    except _ledger().GrantExhausted as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {save}")
        conn.execute(f"RELEASE SAVEPOINT {save}")
        return (
            "deferred",
            f"deferred: grant exhausted for {exc.counter}: used {exc.used} of {exc.ceiling}",
            base, [], None, row["page_id"],
        )
    except _goal().ValidationError as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {save}")
        conn.execute(f"RELEASE SAVEPOINT {save}")
        text = exc.message
        if "query window" in text:
            return "deferred", "deferred: query window is locked", base, [], None, row["page_id"]
        if exc.field == "depth" or "depth" in text:
            return "dropped", "dropped: depth exceeds max_depth", base, [], None, row["page_id"]
        return "deferred", f"deferred: reserve denied: {text}", base, [], None, row["page_id"]
    except _goal().IdempotencyConflict:
        conn.execute(f"ROLLBACK TO SAVEPOINT {save}")
        conn.execute(f"RELEASE SAVEPOINT {save}")
        return "deferred", "deferred: admission replay is not a new dispatch", base, [], None, row["page_id"]


def _stale_for_node(conn, goal_id: int, epoch: int, row: Mapping[str, Any]) -> Optional[int]:
    for stale in _stale_tasks(conn, goal_id, epoch):
        payload = _payload_ids(stale["payload"])
        if row["page_id"] is not None and payload.get("page_id") == row["page_id"]:
            return int(stale["epoch"])
        if payload.get("node_key") == row["node_key"]:
            return int(stale["epoch"])
    return None


def _link(conn, store, runnable, binding, proposal: Proposal, row, topic_id: int, now: int):
    locks = _orch().LockEngine(store)
    query_id = None
    page_id = row["page_id"]
    if proposal.query:
        locked = locks.query_lock(
            conn, proposal.query, QUERY_SOURCE, 7, topic_id, via=proposal.origin, priority=float(row["score"])
        )
        if locked is None:
            raise _goal().ValidationError("query", "query window is locked")
        query_id = int(locked)
        columns = {info["name"] for info in conn.execute("PRAGMA table_info(queries)")}
        if "program_key" in columns:
            conn.execute(
                "UPDATE queries SET program_key=?, field_key=? WHERE id=?",
                (proposal.program_key, proposal.field_key, query_id),
            )
        conn.execute(
            """INSERT OR IGNORE INTO goal_queries
                 (goal_id, contract_version, query_id, program_key, field_key, origin, visibility)
               VALUES (?, ?, ?, ?, ?, ?, 'public')""",
            (binding["goal_id"], binding["contract_version"], query_id, proposal.program_key, proposal.field_key, proposal.origin),
        )
    depth = 0
    if proposal.kind == "fetch" and row["canon"]:
        if page_id is None:
            created = locks.enqueue_url(conn, row["canon"], topic_id, proposal.origin, 7, depth=0)
            if created is not None:
                page_id = int(created)
            else:
                found = _page_row(conn, row["canon"])
                page_id = None if found is None else int(found["id"])
        if page_id is None:
            raise _goal().ValidationError("url", "discovered url did not enqueue")
        depth = _assign_depth(conn, binding["goal_id"], binding["contract_version"], page_id)
        parent = _one(conn, "SELECT parent_page_id FROM pages WHERE id=?", (page_id,))
        parent_id = None if parent is None else parent["parent_page_id"]
        if depth == 0:
            parent_id = None
        conn.execute(
            """INSERT OR IGNORE INTO goal_pages
                 (goal_id, contract_version, page_id, parent_page_id, depth, origin, visibility, queued_at)
               VALUES (?, ?, ?, ?, ?, ?, 'public', ?)""",
            (
                binding["goal_id"], binding["contract_version"], page_id, parent_id, depth,
                proposal.origin, now,
            ),
        )
    return query_id, page_id, depth


def _insert_task(conn, runnable, binding, proposal: Proposal, wave_id: int, now: int, operation_key: str, query_id, page_id, score: float) -> int:
    payload = {
        "origin": proposal.origin,
        "program_key": proposal.program_key,
        "field_key": proposal.field_key,
        "query_id": query_id,
        "page_id": page_id,
        "node_key": proposal.url or proposal.query,
    }
    cur = conn.execute(
        """INSERT INTO tasks(
             kind, payload, priority, status, next_attempt_at, created_at, wave_id,
             goal_id, contract_version, strategy_version, epoch, grant_id, operation_key)
           VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "fetch" if proposal.kind == "fetch" else "search",
            _canonical(payload),
            float(score),
            now,
            now,
            wave_id,
            binding["goal_id"],
            binding["contract_version"],
            binding["strategy_version"],
            binding["epoch"],
            int(runnable.grant.id),
            operation_key,
        ),
    )
    return int(cur.lastrowid)


__all__ = [
    "DEFAULT_CRITICAL",
    "PLAN_MAX_HYPOTHESES",
    "PROGRAM_FIELD_QUERY_COOLDOWN_SEC",
    "PlannerError",
    "PlannerOutage",
    "Proposal",
    "SchedulePlan",
    "ValidatedProposal",
    "coverage_fingerprint",
    "gap_proposals",
    "planner_proposals",
    "public_coverage_summary",
    "schedule",
    "validate_proposals",
    "work_fingerprint",
]
