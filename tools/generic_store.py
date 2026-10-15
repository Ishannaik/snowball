"""Durable generic goal store.

Owns the goal lifecycle (draft -> approved -> granted -> running -> ...),
immutable versions, version-lifetime resource admission (used/reserved/lost
across every grant), domain leases, runner ownership, immutable evidence with
independent review, and the complete read view. Approval never grants or
starts. Stale bindings preserve raw bytes but never commit current semantics.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APPLICATION_ID = 0x4745_4E31  # "GEN1"
SCHEMA_MARKER = "generic_schema"
SCHEMA_VALUE = "1"

REVIEW_STATES = ("kept", "disputed", "rejected", "unknown", "superseded")
PROVENANCES = frozenset({"public_fetch", "human_note", "legacy_lead"})

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS generic_goals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  state TEXT NOT NULL,
  row_version INTEGER NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  current_body_hash TEXT,
  outcome TEXT,
  reason TEXT,
  overrun INTEGER NOT NULL DEFAULT 0,
  overrun_json TEXT,
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS generic_versions (
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  public_id TEXT NOT NULL,
  public_hash TEXT NOT NULL,
  body_json TEXT NOT NULL,
  status TEXT NOT NULL,
  parent_body_hash TEXT,
  revision INTEGER,
  PRIMARY KEY (goal_id, body_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS generic_versions_public_id
  ON generic_versions(goal_id, public_id);
CREATE TABLE IF NOT EXISTS generic_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  limits_json TEXT NOT NULL,
  used_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS generic_usage (
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  counter TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  reserved INTEGER NOT NULL DEFAULT 0,
  lost INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (goal_id, body_hash, counter)
);
CREATE TABLE IF NOT EXISTS generic_operations (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  grant_id INTEGER,
  kind TEXT NOT NULL,
  operation_key TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  costs_json TEXT NOT NULL,
  actual_json TEXT,
  binding_json TEXT NOT NULL,
  state TEXT NOT NULL,
  domain TEXT,
  host TEXT,
  pid INTEGER,
  created_at INTEGER NOT NULL DEFAULT 0,
  UNIQUE (goal_id, body_hash, operation_key, attempt)
);
CREATE INDEX IF NOT EXISTS generic_operations_goal ON generic_operations(goal_id, body_hash, state);
CREATE TABLE IF NOT EXISTS generic_domain_leases (
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  domain TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  lease_until INTEGER NOT NULL,
  PRIMARY KEY (goal_id, body_hash, domain)
);
CREATE TABLE IF NOT EXISTS generic_runner_leases (
  goal_id TEXT PRIMARY KEY,
  token TEXT NOT NULL,
  host TEXT NOT NULL,
  pid INTEGER NOT NULL,
  heartbeat INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS generic_drafts (
  goal_id TEXT PRIMARY KEY,
  draft_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS generic_snapshots (
  goal_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  provenance TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  label_json TEXT NOT NULL DEFAULT '[]',
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  raw BLOB,
  raw_hash TEXT,
  content_type TEXT NOT NULL DEFAULT '',
  snapshot_hash TEXT NOT NULL,
  fetched_at INTEGER,
  operation_id TEXT,
  binding_json TEXT,
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (goal_id, source_id)
);
CREATE TABLE IF NOT EXISTS generic_claims (
  goal_id TEXT NOT NULL,
  id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  source_id TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  quote TEXT NOT NULL,
  start INTEGER NOT NULL,
  end INTEGER NOT NULL,
  quote_hash TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'unknown',
  provenance TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'source',
  criterion_ids TEXT NOT NULL DEFAULT '[]',
  basis_ids TEXT NOT NULL DEFAULT '[]',
  score REAL,
  text TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  label_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  producer TEXT,
  producer_recipient TEXT,
  produced_at INTEGER,
  binding_json TEXT,
  review_json TEXT,
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (goal_id, id)
);
CREATE TABLE IF NOT EXISTS generic_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  verdict TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  binding_json TEXT,
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS generic_judgments (
  goal_id TEXT NOT NULL,
  id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  recipient_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  invalid INTEGER NOT NULL DEFAULT 0,
  label_json TEXT NOT NULL DEFAULT '[]',
  model_id TEXT,
  binding_json TEXT,
  created_at INTEGER NOT NULL DEFAULT 0,
  body_json TEXT NOT NULL,
  PRIMARY KEY (goal_id, id)
);
CREATE TABLE IF NOT EXISTS generic_draft_tasks (
  task_key TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  task_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  task_json TEXT NOT NULL,
  result_json TEXT,
  binding_json TEXT,
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS generic_tasks_fingerprint
  ON generic_draft_tasks(goal_id, body_hash, fingerprint);
CREATE TABLE IF NOT EXISTS generic_results (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  label_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  model_id TEXT,
  binding_json TEXT,
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS generic_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS generic_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id TEXT NOT NULL,
  body_hash TEXT,
  stale INTEGER NOT NULL DEFAULT 0,
  binding_json TEXT,
  receipt TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS generic_turns (
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  turns INTEGER NOT NULL DEFAULT 0,
  context_version INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (goal_id, body_hash)
);
CREATE TABLE IF NOT EXISTS generic_context_items (
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  context_version INTEGER NOT NULL,
  item_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'claim',
  PRIMARY KEY (goal_id, body_hash, context_version, item_id)
);
CREATE TABLE IF NOT EXISTS generic_graph_edges (
  goal_id TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'supports',
  PRIMARY KEY (goal_id, body_hash, src, dst, kind)
);
"""


def _sibling(name: str):
    key = f"snowball_generic_{name}_store"
    existing = sys.modules.get(key)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(key, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _contract():
    return _sibling("generic_contract")


def _evidence():
    return _sibling("generic_evidence")


def _judge():
    return _sibling("generic_judge")


class StoreError(ValueError):
    """Storage refusal with a stable field the caller can branch on."""

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


def _sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _now() -> int:
    return int(time.time())


def _digest_value(value: Any) -> str:
    return _sha256(_contract().canonical(value))


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Explicit idempotent schema bootstrap/upgrade for one open connection.

    Creates missing generic_* tables, adds missing columns to older generic
    databases, stamps the schema_meta marker, and never touches PRAGMA
    user_version or any legacy (specialist) table. Safe to call repeatedly;
    commits when any change was applied.
    """
    known = {
        "generic_goals": {"outcome": "ALTER TABLE generic_goals ADD COLUMN outcome TEXT", "reason": "ALTER TABLE generic_goals ADD COLUMN reason TEXT", "overrun": "ALTER TABLE generic_goals ADD COLUMN overrun INTEGER NOT NULL DEFAULT 0", "overrun_json": "ALTER TABLE generic_goals ADD COLUMN overrun_json TEXT", "created_at": "ALTER TABLE generic_goals ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0"},
        "generic_versions": {"parent_body_hash": "ALTER TABLE generic_versions ADD COLUMN parent_body_hash TEXT", "revision": "ALTER TABLE generic_versions ADD COLUMN revision INTEGER"},
        "generic_grants": {"epoch": "ALTER TABLE generic_grants ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0", "created_at": "ALTER TABLE generic_grants ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0"},
    }
    conn.executescript(DDL)
    for table, columns in known.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, statement in columns.items():
            if column not in existing:
                conn.execute(statement)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (SCHEMA_MARKER, SCHEMA_VALUE),
    )
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('generic_meta', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (json.dumps({"application_id": APPLICATION_ID}),),
    )
    conn.commit()


def _connect(path: Path, readonly: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{Path(path).as_posix()}?mode=ro" if readonly else path,
        uri=readonly,
        check_same_thread=True,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        # mode=ro cannot set journal/synchronous pragmas; a missing -wal file
        # makes any attempted write an OperationalError.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _public_projection(body: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the frozen public projection of an approved version body.

    Mirrors generic_contract.build_version: public brief text only when the
    brief label is public, selected public criteria, ceilings, and public seeds.
    Never contains private answers, private criteria, or the private body hash.
    """
    public = {
        "brief": body["public_brief"]["text"] if body["public_brief"]["label"] == [] else None,
        "criteria": [row for row in body["rubric"]["criteria"] if row["id"] in set(body["public_criterion_ids"])],
        "ceilings": body["ceilings"],
        "seeds": [row for row in body["seeds"] if row.get("label") == []],
    }
    return public


def open_store(path: Path, *, initialize: bool = True, readonly: bool = False) -> "Store":
    """Open the generic store.

    - readonly=True: strict read-only on an existing database; never creates,
      migrates, or writes. A missing file raises StoreError.
    - initialize=True (default): creating a brand-new database is allowed and
      an existing generic database is opened as-is; an existing nonempty but
      incompatible database is refused until generic_store.initialize_schema is
      called explicitly (the migration path backs up, then calls it).
    - initialize=False: mutations only on an existing compatible database; a
      missing file raises StoreError.
    """
    path = Path(path)
    if readonly:
        if not path.exists():
            raise StoreError("path", f"database does not exist: {path.name}")
        conn = _connect(path, readonly=True)
        store = Store(conn, readonly=True)
        store._verify_compatible(require_marker=False)
        return store
    if not path.exists():
        if not initialize:
            raise StoreError("path", f"database does not exist: {path.name}")
        conn = _connect(path, readonly=False)
        initialize_schema(conn)
        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        conn.commit()
        return Store(conn)
    conn = _connect(path, readonly=False)
    store = Store(conn)
    if store._is_empty():
        initialize_schema(conn)
        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        conn.commit()
        return store
    store._verify_compatible(require_marker=True)
    return store


class _Transaction:
    """BEGIN IMMEDIATE write transaction; commits on clean exit, rolls back on error."""

    def __init__(self, store: "Store"):
        self.store = store
        self.conn = store.conn
        self._nested = self.conn.in_transaction

    def __enter__(self) -> sqlite3.Connection:
        self.store._thread_check()
        if not self._nested:
            self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._nested:
            return
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()


class Store:
    """Thread-affine durable store. One Store per thread; never share it."""

    def __init__(self, conn: sqlite3.Connection, readonly: bool = False):
        self.conn = conn
        self.readonly = readonly
        self._owner = threading.get_ident()
        self.path = None

    # -- plumbing ---------------------------------------------------------
    def _thread_check(self) -> None:
        if threading.get_ident() != self._owner:
            raise StoreError("thread", "Store is thread-affine; open one per thread")

    def _is_empty(self) -> bool:
        row = self.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()
        return int(row[0]) == 0

    def _marker(self) -> str | None:
        try:
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key=?", (SCHEMA_MARKER,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row is not None else None

    def _is_generic(self) -> bool:
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return "generic_goals" in tables

    def _verify_compatible(self, *, require_marker: bool) -> None:
        if self._is_empty():
            return
        if not self._is_generic():
            raise StoreError(
                "schema",
                "database is not a generic store; explicit migration required",
            )
        marker = self._marker()
        if require_marker and marker != SCHEMA_VALUE:
            raise StoreError(
                "schema",
                "generic database predates explicit migration; "
                "run tools/generic_migration.migrate after a backup",
            )

    def now(self) -> int:
        """Wall clock; tests override to pin leases."""
        return _now()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @conn.setter
    def conn(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _tx(self):
        return _Transaction(self)

    def _check_writable(self) -> None:
        if self.readonly:
            raise StoreError("readonly", "store opened read-only")

    # -- goals ------------------------------------------------------------
    def _goal_row(self, conn, goal_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM generic_goals WHERE id=?", (goal_id,)
        ).fetchone()
        if row is None:
            raise StoreError("goal_id", "goal not found")
        return dict(row)

    def goal(self, goal_id: str) -> dict[str, Any]:
        return self._goal_row(self.conn, goal_id)

    def create_goal(self, goal_id: str, title: str) -> dict[str, Any]:
        self._check_writable()
        if not isinstance(goal_id, str) or not goal_id.strip():
            raise StoreError("goal_id", "goal id is required")
        if not isinstance(title, str) or not title.strip():
            raise StoreError("title", "title is required")
        with self._tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM generic_goals WHERE id=?", (goal_id,)
            ).fetchone()
            if exists is not None:
                raise StoreError("goal_id", "goal already exists")
            conn.execute(
                "INSERT INTO generic_goals(id, title, state, row_version, epoch, created_at) "
                "VALUES(?, ?, 'draft', 0, 0, ?)",
                (goal_id, title, self.now()),
            )
        return self.goal(goal_id)

    def list_goals(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, title, state, row_version, epoch, current_body_hash, outcome, reason "
            "FROM generic_goals ORDER BY created_at, id"
        ).fetchall()
        return [dict(row) for row in rows]

    # -- versions ---------------------------------------------------------
    def version(self, goal_id: str) -> dict[str, Any]:
        goal = self._goal_row(self.conn, goal_id)
        if not goal["current_body_hash"]:
            raise StoreError("state", "no approved version for this goal")
        return self._version_by_hash(goal_id, goal["current_body_hash"])

    def _maybe_version(self, goal_id: str) -> dict[str, Any] | None:
        goal = self._goal_row(self.conn, goal_id)
        if not goal["current_body_hash"]:
            return None
        return self._version_by_hash(goal_id, goal["current_body_hash"])

    def _version_by_hash(self, goal_id: str, body_hash: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM generic_versions WHERE goal_id=? AND body_hash=?",
            (goal_id, body_hash),
        ).fetchone()
        if row is None:
            raise StoreError("body_hash", "no approved version")
        return {
            "goal_id": goal_id,
            "body_hash": row["body_hash"],
            "public_id": row["public_id"],
            "public_hash": row["public_hash"],
            "body": json.loads(row["body_json"]),
            "public": _public_projection(json.loads(row["body_json"])),
            "status": row["status"],
            "parent_body_hash": row["parent_body_hash"],
            "revision": row["revision"],
        }

    def _cas_goal(self, conn, goal_id: str, expected_row_version: int | None, *, bump: bool = True) -> dict[str, Any]:
        goal = self._goal_row(conn, goal_id)
        current = int(goal["row_version"])
        if expected_row_version is not None and current != int(expected_row_version):
            raise StoreError("row_version", f"stale goal row_version {expected_row_version} vs {current}")
        if bump:
            conn.execute(
                "UPDATE generic_goals SET row_version=row_version+1 WHERE id=?", (goal_id,)
            )
        goal = self._goal_row(conn, goal_id)
        return dict(goal)

    # -- approval ---------------------------------------------------------
    def approve(self, goal_id: str, payload: dict[str, Any], expected_row_version: int, expected_body_hash: str | None = None) -> dict[str, Any]:
        self._check_writable()
        if not isinstance(payload, dict) or payload.get("goal_id") != goal_id:
            raise StoreError("goal_id", "contract is for a different goal")
        with self._tx() as conn:
            goal = self._goal_row(conn, goal_id)
            if int(goal["row_version"]) != int(expected_row_version) or goal["state"] not in {"draft", "revising"}:
                raise StoreError("row_version", "stale goal")
            prior_row = conn.execute(
                "SELECT body_hash, body_json, rowid FROM generic_versions WHERE goal_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
            version = _contract().build_version(payload)
            body = version["body"]
            if prior_row is not None:
                prior_body = json.loads(prior_row["body_json"])
                expected_revision = int(prior_body["revision"]) + 1
                if int(body["revision"]) != expected_revision:
                    raise StoreError("revision", f"revision must increment to {expected_revision}")
                if body["parent_body_hash"] != prior_row["body_hash"]:
                    raise StoreError("parent_body_hash", "parent hash must chain to the previous approved version")
                self._criterion_identity(prior_body, body)
            elif int(body["revision"]) != 1 or body["parent_body_hash"] is not None:
                raise StoreError("revision", "the first approved version is revision 1 with no parent")
            if expected_body_hash is not None and expected_body_hash != version["body_hash"]:
                raise StoreError("body_hash", "approved hash does not match the reviewed payload")
            dup = conn.execute(
                "SELECT 1 FROM generic_versions WHERE goal_id=? AND body_hash=?",
                (goal_id, version["body_hash"]),
            ).fetchone()
            if dup is not None:
                raise StoreError("body_hash", "this exact version is already approved")
            try:
                conn.execute(
                    "INSERT INTO generic_versions(goal_id, body_hash, public_id, public_hash, "
                    "body_json, status, parent_body_hash, revision) VALUES(?,?,?,?,?,?,?,?)",
                    (goal_id, version["body_hash"], version["public_id"], version["public_hash"],
                     _contract().canonical(body), "approved",
                     body["parent_body_hash"], body["revision"]),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("public_id", "public id already used for this goal") from exc
            new_state = "approved"
            conn.execute(
                "UPDATE generic_goals SET state=?, row_version=row_version+1, current_body_hash=?, "
                "outcome=NULL, reason=NULL WHERE id=? AND row_version=?",
                (new_state, version["body_hash"], goal_id, goal["row_version"]),
            )
            fresh = self._fresh_grant_goals(conn, goal_id, version["body_hash"])
            self._event(conn, goal_id, "approve", {
                "body_hash": version["body_hash"], "revision": body["revision"],
                "epoch": int(self._goal_row(conn, goal_id)["epoch"]), "replaces_retired_grants": fresh,
            })
        return {"goal": self.goal(goal_id), "version": version, "grant_id": None}

    def _fresh_grant_goals(self, conn, goal_id: str, body_hash: str) -> int:
        rows = conn.execute(
            "SELECT id, state FROM generic_grants WHERE goal_id=? AND body_hash<>? AND state IN ('active','paused')",
            (goal_id, body_hash),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE generic_grants SET state='retired' WHERE id=?", (row["id"],)
            )
        return len(rows)

    def _criterion_identity(self, prior: dict[str, Any], body: dict[str, Any]) -> None:
        old = {row["id"]: _contract().canonical(row) for row in prior["rubric"]["criteria"]}
        for row in body["rubric"]["criteria"]:
            if row["id"] in old and old[row["id"]] != _contract().canonical(row):
                raise StoreError(
                    "rubric",
                    f"criterion {row['id']} changed definition; use a new criterion id",
                )

    # -- controls ---------------------------------------------------------
    def approved_ceilings(self, goal_id: str) -> dict[str, int]:
        return dict(self.version(goal_id)["body"]["ceilings"])

    def grant(self, goal_id: str, limits: dict[str, int], expected_row_version: int | None = None) -> dict[str, Any]:
        self._check_writable()
        if not isinstance(limits, dict):
            raise StoreError("limits", "limits must be an object")
        with self._tx() as conn:
            goal = self._goal_row(conn, goal_id)
            if goal["state"] not in {"approved", "granted", "running", "paused"} or not goal["current_body_hash"]:
                raise StoreError("state", "grant requires an approved version")
            if expected_row_version is not None:
                self._cas_goal(conn, goal_id, expected_row_version, bump=False)
            ceilings = json.loads(
                conn.execute(
                    "SELECT body_json FROM generic_versions WHERE goal_id=? AND body_hash=?",
                    (goal_id, goal["current_body_hash"]),
                ).fetchone()["body_json"]
            )["ceilings"]
            clean: dict[str, int] = {}
            for key, value in limits.items():
                if key not in ceilings:
                    raise StoreError(key, "unknown limit unit")
                if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > int(ceilings[key]):
                    raise StoreError(key, f"grant exceeds the approved ceiling {ceilings[key]}")
                clean[key] = value
            cursor = conn.execute(
                "UPDATE generic_grants SET state='retired', used_json=used_json "
                "WHERE goal_id=? AND body_hash=? AND state='active'",
                (goal_id, goal["current_body_hash"]),
            )
            conn.execute(
                "INSERT INTO generic_grants(goal_id, body_hash, epoch, state, limits_json, used_json, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (goal_id, goal["current_body_hash"], int(goal["epoch"]), "active",
                 _contract().canonical(clean), "{}", self.now()),
            )
            grant_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            if goal["state"] == "approved":
                conn.execute(
                    "UPDATE generic_goals SET state='granted', row_version=row_version+1 WHERE id=?",
                    (goal_id,),
                )
            else:
                # Renewal of an already-started goal keeps its execution state.
                conn.execute(
                    "UPDATE generic_goals SET row_version=row_version+1 WHERE id=?",
                    (goal_id,),
                )
            self._event(conn, goal_id, "grant", {"grant_id": grant_id, "limits": clean})
        return {"grant_id": grant_id, "state": "granted"}

    def _transition(self, goal_id: str, expected_row_version: int | None, *, allowed: frozenset[str], target: str | None, spec: str) -> dict[str, Any]:
        self._check_writable()
        with self._tx() as conn:
            goal = self._goal_row(conn, goal_id)
            if expected_row_version is not None:
                self._cas_goal(conn, goal_id, expected_row_version, bump=False)
            if goal["state"] not in allowed:
                raise StoreError("state", spec)
            sets = ["row_version=row_version+1"]
            params: list[Any] = []
            if target is not None:
                sets.append("state=?")
                params.append(target)
            params.append(goal_id)
            conn.execute(f"UPDATE generic_goals SET {', '.join(sets)} WHERE id=?", tuple(params))
            self._event(conn, goal_id, spec.split()[0], {"from": goal["state"]})
        return self.goal(goal_id)

    def start(self, goal_id: str, grant_id: int | None = None, expected_row_version: int | None = None) -> dict[str, Any]:
        self._check_writable()
        with self._tx() as conn:
            goal = self._goal_row(conn, goal_id)
            if expected_row_version is not None:
                self._cas_goal(conn, goal_id, expected_row_version, bump=False)
            if goal["state"] != "granted":
                raise StoreError("state", "start requires a separate granted state")
            if grant_id is None:
                raise StoreError("grant_id", "grant_id is required to start")
            row = conn.execute(
                "SELECT * FROM generic_grants WHERE id=? AND goal_id=?",
                (int(grant_id), goal_id),
            ).fetchone()
            if row is None or row["state"] != "active" or row["body_hash"] != goal["current_body_hash"]:
                raise StoreError("grant_id", "start requires an active grant for the current version")
            conn.execute(
                "UPDATE generic_goals SET state='running', row_version=row_version+1 WHERE id=?",
                (goal_id,),
            )
            self._event(conn, goal_id, "start", {"grant_id": int(grant_id)})
        return self.goal(goal_id)

    def pause(self, goal_id: str, expected_row_version: int | None = None) -> dict[str, Any]:
        return self._transition(goal_id, expected_row_version, allowed=frozenset({"running"}), target="paused", spec="pause stops a running goal")

    def resume(self, goal_id: str, expected_row_version: int | None = None) -> dict[str, Any]:
        return self._transition(goal_id, expected_row_version, allowed=frozenset({"paused"}), target="running", spec="resume returns a paused goal to running")

    def stop(self, goal_id: str, expected_row_version: int | None = None) -> dict[str, Any]:
        self._check_writable()
        with self._tx() as conn:
            goal = self._goal_row(conn, goal_id)
            if expected_row_version is not None:
                self._cas_goal(conn, goal_id, expected_row_version, bump=False)
            if goal["state"] not in {"running", "paused", "granted"}:
                raise StoreError("state", "stop applies to granted/running/paused goals")
            conn.execute(
                "UPDATE generic_grants SET state='retired' WHERE goal_id=? AND state IN ('active','paused')",
                (goal_id,),
            )
            conn.execute(
                "UPDATE generic_goals SET state='stopped', row_version=row_version+1, "
                "outcome=COALESCE(outcome,'user_stopped'), reason=COALESCE(reason,'Research stopped by the user.') WHERE id=?",
                (goal_id,),
            )
            self._event(conn, goal_id, "stop", {"from": goal["state"]})
        return self.goal(goal_id)

    def revise(self, goal_id: str, expected_row_version: int | None = None) -> dict[str, Any]:
        self._check_writable()
        with self._tx() as conn:
            goal = self._goal_row(conn, goal_id)
            if expected_row_version is not None:
                self._cas_goal(conn, goal_id, expected_row_version, bump=False)
            if goal["state"] not in {"draft", "approved", "granted", "running", "paused", "stopped"}:
                raise StoreError("state", "revise is not possible from this state")
            retired = self._release_execution(conn, goal_id, goal, reason="revision")
            conn.execute(
                "UPDATE generic_goals SET state='revising', epoch=epoch+1, row_version=row_version+1, outcome=NULL, reason=NULL WHERE id=?",
                (goal_id,),
            )
            self._event(conn, goal_id, "revise", {"epoch": int(goal["epoch"]) + 1, "retired_operations": retired})
        return self.goal(goal_id)

    def _release_execution(self, conn, goal_id: str, goal: dict[str, Any], *, reason: str) -> int:
        """Cancel every undispatched reservation; dispatched work stays charged as lost."""
        body_hash = goal["current_body_hash"]
        rows = conn.execute(
            "SELECT id, state, costs_json FROM generic_operations "
            "WHERE goal_id=? AND body_hash=? AND state IN ('reserved','dispatched')",
            (goal_id, body_hash),
        ).fetchall()
        count = 0
        for row in rows:
            costs = json.loads(row["costs_json"])
            if row["state"] == "reserved":
                self._move_usage(conn, goal_id, body_hash, costs, reserved=-1)
                conn.execute(
                    "UPDATE generic_operations SET state='released', actual_json=? WHERE id=?",
                    (_contract().canonical({"reason": "released", "costs": {}}), row["id"]),
                )
            else:
                self._move_usage(conn, goal_id, body_hash, costs, reserved=-1, lost=+1)
                conn.execute(
                    "UPDATE generic_operations SET state='lost', actual_json=? WHERE id=?",
                    (_contract().canonical({"reason": reason, "lost": True}), row["id"]),
                )
            count += 1
        self._release_leases(conn, goal_id, body_hash)
        return count

    def _release_leases(self, conn, goal_id: str, body_hash: str) -> None:
        conn.execute(
            "DELETE FROM generic_domain_leases WHERE goal_id=? AND body_hash=?",
            (goal_id, body_hash),
        )

    # -- drafts -----------------------------------------------------------
    def read_draft(self, goal_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT draft_json FROM generic_drafts WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["draft_json"])

    def save_draft(self, goal_id: str, draft: dict[str, Any], expected_row_version: int | None = None) -> dict[str, Any]:
        self._check_writable()
        if not isinstance(draft, dict):
            raise StoreError("draft", "draft must be an object")
        with self._tx() as conn:
            goal = self._goal_row(conn, goal_id)
            if expected_row_version is not None:
                self._cas_goal(conn, goal_id, expected_row_version, bump=False)
                conn.execute(
                    "UPDATE generic_goals SET row_version=row_version+1 WHERE id=?", (goal_id,)
                )
            conn.execute(
                "INSERT INTO generic_drafts(goal_id, draft_json, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(goal_id) DO UPDATE SET draft_json=excluded.draft_json, updated_at=excluded.updated_at",
                (goal_id, _contract().canonical(draft), self.now()),
            )
        return self.goal(goal_id)

    # -- binding ----------------------------------------------------------
    def binding(self, goal_id: str) -> dict[str, Any]:
        goal = self._goal_row(self.conn, goal_id)
        if not goal["current_body_hash"]:
            raise StoreError("body_hash", "no approved version to bind")
        grant = self.conn.execute(
            "SELECT id FROM generic_grants WHERE goal_id=? AND body_hash=? AND state='active' ORDER BY id DESC LIMIT 1",
            (goal_id, goal["current_body_hash"]),
        ).fetchone()
        return {
            "goal_id": goal_id,
            "body_hash": goal["current_body_hash"],
            "epoch": int(goal["epoch"]),
            "grant_id": int(grant["id"]) if grant is not None else None,
        }

    def check_binding(self, binding: dict[str, Any] | None) -> bool:
        if not isinstance(binding, dict):
            return False
        try:
            goal = self._goal_row(self.conn, binding["goal_id"])
        except (KeyError, StoreError):
            return False
        return (
            goal["current_body_hash"] == binding.get("body_hash")
            and int(goal["epoch"]) == int(binding.get("epoch", -1))
            and goal["state"] == "running"
        )

    # -- admission --------------------------------------------------------
    def _move_usage(self, conn, goal_id: str, body_hash: str, costs: dict[str, int], *, used: int = 0, reserved: int = 0, lost: int = 0) -> None:
        for counter in sorted(costs):
            conn.execute(
                "INSERT INTO generic_usage(goal_id, body_hash, counter) VALUES(?,?,?) "
                "ON CONFLICT(goal_id, body_hash, counter) DO NOTHING",
                (goal_id, body_hash, counter),
            )
            conn.execute(
                "UPDATE generic_usage SET used=used+?, reserved=reserved+?, lost=lost+? "
                "WHERE goal_id=? AND body_hash=? AND counter=?",
                (int(costs[counter]) * used, int(costs[counter]) * reserved, int(costs[counter]) * lost, goal_id, body_hash, counter),
            )

    def _usage_rows(self, conn, goal_id: str, body_hash: str) -> dict[str, dict[str, int]]:
        rows = conn.execute(
            "SELECT counter, used, reserved, lost FROM generic_usage WHERE goal_id=? AND body_hash=?",
            (goal_id, body_hash),
        ).fetchall()
        return {row["counter"]: {"used": int(row["used"]), "reserved": int(row["reserved"]), "lost": int(row["lost"])} for row in rows}

    def _headroom(self, conn, goal_id: str, goal: dict[str, Any], active_grant_id: int | None) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
        body_hash = goal["current_body_hash"]
        version = conn.execute(
            "SELECT body_json FROM generic_versions WHERE goal_id=? AND body_hash=?",
            (goal_id, body_hash),
        ).fetchone()
        ceilings = json.loads(version["body_json"])["ceilings"] if version else {}
        usage = self._usage_rows(conn, goal_id, body_hash)
        counters = sorted(set(ceilings) | set(usage))
        lifetime: dict[str, int] = {}
        for counter in counters:
            row = usage.get(counter, {"used": 0, "reserved": 0, "lost": 0})
            lifetime[counter] = row["used"] + row["reserved"] + row["lost"]
        grant_limits: dict[str, int] = {}
        grant_used: dict[str, int] = {}
        if active_grant_id is not None:
            grant = conn.execute(
                "SELECT limits_json, created_at FROM generic_grants WHERE id=?", (active_grant_id,)
            ).fetchone()
            if grant is not None:
                grant_limits = json.loads(grant["limits_json"])
                now = self.now()
                elapsed = now - int(grant["created_at"])
                if "elapsed_sec" in ceilings and elapsed > int(ceilings["elapsed_sec"]):
                    raise StoreError("elapsed_sec", "approved version elapsed ceiling reached")
                if "elapsed_sec" in grant_limits and elapsed > int(grant_limits["elapsed_sec"]):
                    raise StoreError("elapsed_sec", "grant elapsed ceiling reached")
                first = conn.execute(
                    "SELECT MIN(created_at) AS first FROM generic_operations WHERE goal_id=? AND body_hash=?",
                    (goal_id, body_hash),
                ).fetchone()
                if first is not None and first["first"] is not None:
                    spent = now - int(first["first"])
                    if "elapsed_sec" in ceilings and spent > int(ceilings["elapsed_sec"]):
                        raise StoreError("elapsed_sec", "approved version elapsed ceiling reached")
        for counter, spent in lifetime.items():
            if counter in ceilings and spent > int(ceilings[counter]):
                raise StoreError(counter, "approved version lifetime ceiling reached")
        return usage, grant_limits

    def _domain_admission(self, conn, goal_id: str, body_hash: str, domain: str | None, operation_id: str) -> None:
        if not domain:
            return
        now = self.now()
        conn.execute(
            "DELETE FROM generic_domain_leases WHERE lease_until < ?", (now,)
        )
        held = conn.execute(
            "SELECT operation_id FROM generic_domain_leases WHERE goal_id=? AND body_hash=? AND domain=?",
            (goal_id, body_hash, domain),
        ).fetchone()
        if held is not None:
            raise StoreError("domain", f"another operation holds the {domain} lease")
        # Conservative per-domain admission: max 1 concurrent (lease) and 6/min.
        minute_ago = now - 60
        recent = conn.execute(
            "SELECT count(*) AS n FROM generic_operations WHERE goal_id=? AND body_hash=? AND domain=? "
            "AND state IN ('reserved','dispatched','settled','lost') AND created_at > ?",
            (goal_id, body_hash, domain, minute_ago),
        ).fetchone()
        if int(recent["n"]) >= 6:
            raise StoreError("domain", f"conservative {domain} rate reached (6 operations per minute)")
        conn.execute(
            "INSERT INTO generic_domain_leases(goal_id, body_hash, domain, operation_id, lease_until) VALUES(?,?,?,?,?)",
            (goal_id, body_hash, domain, operation_id, now + 120),
        )

    def _validate_costs(self, costs: dict[str, int]) -> dict[str, int]:
        if not isinstance(costs, dict) or not costs:
            raise StoreError("costs", "costs must be a nonempty object")
        clean: dict[str, int] = {}
        for key, value in costs.items():
            if key == "elapsed_sec" or key == "concurrency":
                raise StoreError(key, "that unit is not reservable")
            if key not in _contract().LIMIT_UNITS:
                raise StoreError(key, "unknown limit unit")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise StoreError(key, "costs must be nonnegative integers")
            if value:
                clean[key] = value
        if not clean:
            raise StoreError("costs", "costs must reserve something")
        return clean

    def reserve(self, goal_id: str, costs: dict[str, int], kind: str, operation_key: str, *, domain: str | None = None) -> dict[str, Any]:
        self._check_writable()
        if not isinstance(operation_key, str) or not operation_key or len(operation_key) > 256:
            raise StoreError("operation_key", "a stable operation key is required")
        if not isinstance(kind, str) or not kind:
            raise StoreError("kind", "operation kind is required")
        clean = self._validate_costs(costs)
        with self._tx() as conn:
            goal = self._goal_row(conn, goal_id)
            if goal["state"] != "running":
                raise StoreError("state", "admission requires a running goal")
            if int(goal["overrun"]):
                raise StoreError("overrun", "a settlement overrun stopped admission until a new version is approved")
            grant = conn.execute(
                "SELECT * FROM generic_grants WHERE goal_id=? AND body_hash=? AND state='active' ORDER BY id DESC LIMIT 1",
                (goal_id, goal["current_body_hash"]),
            ).fetchone()
            if grant is None:
                raise StoreError("grant", "admission requires an active grant")
            grant_id = int(grant["id"])
            usage, grant_limits = self._headroom(conn, goal_id, goal, grant_id)
            existing = conn.execute(
                "SELECT * FROM generic_operations WHERE goal_id=? AND body_hash=? AND operation_key=? ORDER BY attempt DESC LIMIT 1",
                (goal_id, goal["current_body_hash"], operation_key),
            ).fetchone()
            if existing is not None and existing["state"] == "reserved":
                prior = json.loads(existing["costs_json"])
                if prior != clean or existing["kind"] != kind:
                    raise StoreError("operation_key", "idempotency key reused with different costs or kind")
                return self._operation(conn, existing)
            if existing is not None and existing["state"] == "dispatched":
                raise StoreError(
                    "dispatch",
                    "operation already dispatched; a re-dispatch would repeat external work",
                )
            ceilings = json.loads(
                conn.execute(
                    "SELECT body_json FROM generic_versions WHERE goal_id=? AND body_hash=?",
                    (goal_id, goal["current_body_hash"]),
                ).fetchone()["body_json"]
            )["ceilings"]
            for counter, amount in clean.items():
                row = usage.get(counter, {"used": 0, "reserved": 0, "lost": 0})
                lifetime_total = row["used"] + row["reserved"] + row["lost"] + amount
                if lifetime_total > int(ceilings[counter]):
                    raise StoreError(counter, f"version lifetime ceiling {ceilings[counter]} reached for {counter}")
                if counter in grant_limits:
                    grant_spent = sum(
                        int(json.loads(op["costs_json"]).get(counter, 0))
                        for op in conn.execute(
                            "SELECT costs_json FROM generic_operations WHERE grant_id=? AND state IN ('reserved','dispatched','settled','lost')",
                            (grant_id,),
                        )
                    )
                    if grant_spent + amount > int(grant_limits[counter]):
                        raise StoreError(counter, f"grant limit {grant_limits[counter]} reached for {counter}")
            attempt = (int(existing["attempt"]) + 1) if existing is not None else 1
            op_hash = _sha256(_contract().canonical({
                "goal": goal_id, "body_hash": goal["current_body_hash"], "epoch": int(goal["epoch"]),
                "kind": kind, "key": operation_key, "attempt": attempt, "created": self.now(),
            }))[:24]
            operation_id = "op-" + op_hash
            binding = {
                "goal_id": goal_id,
                "body_hash": goal["current_body_hash"],
                "epoch": int(goal["epoch"]),
                "grant_id": grant_id,
            }
            host_name = os.uname().nodename if hasattr(os, "uname") else "unknown"
            conn.execute(
                "INSERT INTO generic_operations(id, goal_id, body_hash, epoch, grant_id, kind, operation_key, attempt, costs_json, binding_json, state, domain, host, pid, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, goal_id, goal["current_body_hash"], int(goal["epoch"]), grant_id, kind,
                 operation_key, attempt, _contract().canonical(clean), _contract().canonical(binding),
                 "reserved", domain, host_name,
                 os.getpid(), self.now()),
            )
            self._move_usage(conn, goal_id, goal["current_body_hash"], clean, reserved=+1)
            self._domain_admission(conn, goal_id, goal["current_body_hash"], domain, operation_id)
        return self._operation(self.conn, self.conn.execute(
            "SELECT * FROM generic_operations WHERE id=?", (operation_id,)
        ).fetchone())

    def _operation(self, _conn, row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "goal_id": row["goal_id"],
            "body_hash": row["body_hash"],
            "epoch": int(row["epoch"]),
            "grant_id": row["grant_id"],
            "kind": row["kind"],
            "operation_key": row["operation_key"],
            "attempt": int(row["attempt"]),
            "costs": json.loads(row["costs_json"]),
            "binding": json.loads(row["binding_json"]),
            "state": row["state"],
            "domain": row["domain"],
        }

    def dispatch(self, operation_id: str) -> dict[str, Any]:
        self._check_writable()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM generic_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise StoreError("operation_id", "unknown operation")
            if row["state"] == "dispatched":
                raise StoreError("dispatch", "already dispatched")
            if row["state"] != "reserved":
                raise StoreError("state", f"operation is {row['state']}; dispatch is not possible")
            changed = conn.execute(
                "UPDATE generic_operations SET state='dispatched' WHERE id=? AND state='reserved'",
                (operation_id,),
            ).rowcount
            if changed != 1:
                raise StoreError("dispatch", "operation changed state concurrently")
            self._event(conn, row["goal_id"], "dispatch", {"operation_id": operation_id, "kind": row["kind"]})
            return json.loads(row["binding_json"])

    def settle(self, operation_id: str, actual: dict[str, int] | None = None, *, lost: bool = False) -> bool:
        """Settle idempotently. Returns the binding validity after settlement."""
        self._check_writable()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM generic_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise StoreError("operation_id", "unknown operation")
            if row["state"] == "released":
                raise StoreError("operation", "a released reservation was never dispatched")
            if row["state"] in {"settled", "lost"}:
                valid = self.check_binding(json.loads(row["binding_json"]))
                return valid
            costs = json.loads(row["costs_json"])
            goal_id, body_hash = row["goal_id"], row["body_hash"]
            if lost:
                self._move_usage(conn, goal_id, body_hash, costs, reserved=-1, lost=+1)
                actual_json = _contract().canonical({"reason": "lost", "costs": {}})
                state = "lost"
            else:
                clean: dict[str, int] = {}
                if actual is None:
                    self._move_usage(conn, goal_id, body_hash, costs, reserved=-1, used=+1)
                    actual_json = _contract().canonical({"reason": "conservative", "costs": costs})
                    state = "settled"
                else:
                    bad = set(actual) - set(_contract().LIMIT_UNITS)
                    if bad:
                        raise StoreError(sorted(bad)[0], "unknown limit unit in actual usage")
                    for counter in actual:
                        if isinstance(actual[counter], bool) or not isinstance(actual[counter], int) or actual[counter] < 0:
                            raise StoreError(counter, "actual usage must be a nonnegative integer")
                    overrun: dict[str, int] = {}
                    for counter, amount in actual.items():
                        if amount:
                            clean[counter] = amount
                    for counter, amount in costs.items():
                        if counter not in clean:
                            clean[counter] = 0
                    for counter, amount in clean.items():
                        reserved_amount = costs.get(counter, 0)
                        if amount > reserved_amount:
                            overrun[counter] = amount - reserved_amount
                    move: dict[str, int] = {}
                    for counter, amount in clean.items():
                        if reserved_amount := costs.get(counter, 0):
                            move[counter] = reserved_amount
                    self._move_usage(conn, goal_id, body_hash, move, reserved=-1)
                    self._move_usage(conn, goal_id, body_hash, clean, used=+1)
                    self._move_usage(conn, goal_id, body_hash, overrun, lost=+1)
                    if overrun:
                        conn.execute(
                            "UPDATE generic_goals SET overrun=1, overrun_json=? WHERE id=? AND current_body_hash=?",
                            (_contract().canonical({"overrun": overrun, "operation_id": operation_id}), goal_id, body_hash),
                        )
                    actual_json = _contract().canonical({"actual": clean, "overrun": overrun})
                    state = "settled"
            changed = conn.execute(
                "UPDATE generic_operations SET state=?, actual_json=? WHERE id=? AND state IN ('dispatched','reserved')",
                (state, actual_json, operation_id),
            ).rowcount
            if changed != 1 and row["state"] not in {"settled", "lost"}:
                raise StoreError("operation", "settlement lost a race; recheck the operation state")
            if row["domain"]:
                self._release_leases(conn, goal_id, body_hash)
            self._event(conn, goal_id, "settle", {"operation_id": operation_id, "state": state})
        return self.check_binding(json.loads(row["binding_json"]))

    def recover(self) -> int:
        """Startup recovery: dispatched work becomeslost, undispatched released, leases cleared.

        Only touches operations whose owning process cannot still be alive
        (different host, or a process id we can literally observe as absent on
        this host); live owners on this host keep their work.
        """
        self._check_writable()
        host = os.uname().nodename if hasattr(os, "uname") else "unknown"
        with self._tx() as conn:
            now = self.now()
            rows = conn.execute(
                "SELECT id, goal_id, body_hash, state, costs_json, host, pid FROM generic_operations "
                "WHERE state IN ('reserved','dispatched')"
            ).fetchall()
            count = 0
            for row in rows:
                dead = row["host"] != host
                if not dead and row["pid"] is not None:
                    dead = not self._pid_alive(int(row["pid"]))
                if not dead:
                    continue
                costs = json.loads(row["costs_json"])
                if row["state"] == "dispatched":
                    self._move_usage(conn, row["goal_id"], row["body_hash"], costs, reserved=-1, lost=+1)
                    conn.execute(
                        "UPDATE generic_operations SET state='lost', actual_json=? WHERE id=?",
                        (_contract().canonical({"reason": "recover"}), row["id"]),
                    )
                else:
                    self._move_usage(conn, row["goal_id"], row["body_hash"], costs, reserved=-1)
                    conn.execute(
                        "UPDATE generic_operations SET state='released', actual_json=? WHERE id=?",
                        (_contract().canonical({"reason": "recover"}), row["id"]),
                    )
                count += 1
            conn.execute("DELETE FROM generic_domain_leases WHERE lease_until < ?", (now,))
            conn.execute(
                "DELETE FROM generic_runner_leases WHERE expires_at < ?",
                (now,),
            )
            self._event_all(conn, "recover", {"operations": count})
        return count

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def headroom(self, goal_id: str) -> dict[str, int]:
        goal = self._goal_row(self.conn, goal_id)
        if not goal["current_body_hash"]:
            return {}
        version = self._maybe_version(goal_id)
        ceilings = version["body"]["ceilings"]
        usage = self._usage_rows(self.conn, goal_id, goal["current_body_hash"])
        grant = self.conn.execute(
            "SELECT id FROM generic_grants WHERE goal_id=? AND body_hash=? AND state='active' ORDER BY id DESC LIMIT 1",
            (goal_id, goal["current_body_hash"]),
        ).fetchone()
        limits = dict(ceilings)
        if grant is not None:
            row = self.conn.execute(
                "SELECT limits_json FROM generic_grants WHERE id=?", (grant["id"],)
            ).fetchone()
            grant_limits = json.loads(row["limits_json"])
            limits = {k: min(int(ceilings[k]), int(grant_limits[k])) for k in ceilings if k in grant_limits}
        out: dict[str, int] = {}
        for counter, ceiling in limits.items():
            row = usage.get(counter, {"used": 0, "reserved": 0, "lost": 0})
            spent = row["used"] + row["reserved"] + row["lost"]
            out[counter] = max(0, int(ceiling) - spent)
        if "elapsed_sec" in limits and grant is not None:
            created = self.conn.execute(
                "SELECT created_at FROM generic_grants WHERE id=?", (grant["id"],)
            ).fetchone()
            spent = max(0, self.now() - int(created["created_at"]))
            first = self.conn.execute(
                "SELECT MIN(created_at) AS first FROM generic_operations WHERE goal_id=? AND body_hash=?",
                (goal_id, goal["current_body_hash"]),
            ).fetchone()
            if first is not None and first["first"] is not None:
                spent = max(spent, self.now() - int(first["first"]))
            out["elapsed_sec"] = max(0, int(limits["elapsed_sec"]) - spent)
        return out

    # -- runner lease -----------------------------------------------------
    def acquire_runner(self, goal_id: str, *, ttl: int = 900) -> str:
        self._check_writable()
        host = os.uname().nodename if hasattr(os, "uname") else "unknown"
        token = "run-" + _sha256(f"{goal_id}:{time.time_ns()}:{os.getpid()}")[:24]
        with self._tx() as conn:
            now = self.now()
            row = conn.execute(
                "SELECT * FROM generic_runner_leases WHERE goal_id=?", (goal_id,)
            ).fetchone()
            if row is not None and int(row["expires_at"]) > now:
                mine = row["host"] == host and int(row["pid"]) == os.getpid()
                if not mine:
                    raise StoreError("runner", "another runner owns this goal")
                token = row["token"]
                conn.execute(
                    "UPDATE generic_runner_leases SET expires_at=? WHERE goal_id=?",
                    (now + ttl, goal_id),
                )
                return token
            conn.execute(
                "DELETE FROM generic_runner_leases WHERE goal_id=?", (goal_id,)
            )
            conn.execute(
                "INSERT INTO generic_runner_leases(goal_id, token, host, pid, heartbeat, expires_at) VALUES(?,?,?,?,?,?)",
                (goal_id, token, host, os.getpid(), now, now + ttl),
            )
        return token

    def heartbeat_runner(self, goal_id: str, token: str, *, ttl: int = 900) -> bool:
        self._check_writable()
        with self._tx() as conn:
            changed = conn.execute(
                "UPDATE generic_runner_leases SET heartbeat=?, expires_at=? WHERE goal_id=? AND token=?",
                (self.now(), self.now() + ttl, goal_id, token),
            ).rowcount
        return changed == 1

    def release_runner(self, goal_id: str, token: str) -> None:
        self._check_writable()
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM generic_runner_leases WHERE goal_id=? AND token=?",
                (goal_id, token),
            )

    # -- snapshots --------------------------------------------------------
    def add_snapshot(self, goal_id: str, source_id: str, text: str, provenance: str, source_kind: str, *, url: str = "", label: list[str] | None = None, binding: dict[str, Any] | None = None, operation_id: str | None = None, raw_bytes: bytes | None = None, content_type: str = "", title: str = "", fetched_at: int | None = None) -> dict[str, Any]:
        self._check_writable()
        if not isinstance(source_id, str) or not source_id:
            raise StoreError("source_id", "source_id is required")
        if not isinstance(text, str):
            raise StoreError("text", "snapshot text must be text")
        if provenance not in PROVENANCES:
            raise StoreError("provenance", "unknown snapshot provenance")
        if provenance in {"human_note", "legacy_lead"} and source_kind == "public_fetch":
            raise StoreError("provenance", "a note cannot be labeled as a public fetch")
        if provenance == "public_fetch" and source_kind != "public_fetch":
            raise StoreError("source_kind", "a public fetch must keep its source kind")
        if raw_bytes is not None and not isinstance(raw_bytes, (bytes, bytearray)):
            raise StoreError("raw_bytes", "raw bytes must be bytes")
        labels = sorted(set(label or []))
        goal = self._goal_row(self.conn, goal_id)
        stale = binding is not None and not self.check_binding(binding)
        if binding is None and goal["state"] != "running":
            raise StoreError("state", "research evidence requires a running goal")
        encoded = text.encode("utf-8")
        raw = bytes(raw_bytes) if raw_bytes is not None else None
        text_hash = _sha256(encoded)
        snapshot_hash = _sha256(
            _contract().canonical({
                "source_id": source_id, "text_hash": text_hash,
                "raw_hash": _sha256(raw) if raw is not None else None,
                "url": url, "provenance": provenance, "source_kind": source_kind,
            })
        )
        body_hash = goal["current_body_hash"]
        epoch = int(goal["epoch"])
        op_state = None
        op_costs = None
        if operation_id is not None:
            row = self.conn.execute(
                "SELECT state, costs_json FROM generic_operations WHERE id=? AND goal_id=?",
                (operation_id, goal_id),
            ).fetchone()
            if row is None:
                raise StoreError("operation_id", "unknown operation")
            if row["state"] not in {"settled", "lost", "dispatched"}:
                raise StoreError("operation", "snapshot costs must be settled before or with the commit")
            op_state = row["state"]
            op_costs = json.loads(row["costs_json"])
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT source_id, snapshot_hash FROM generic_snapshots WHERE goal_id=? AND source_id=?",
                (goal_id, source_id),
            ).fetchone()
            if existing is not None:
                if existing["snapshot_hash"] == snapshot_hash:
                    return self._snapshot_view(conn, self.conn.execute(
                        "SELECT * FROM generic_snapshots WHERE goal_id=? AND source_id=?",
                        (goal_id, source_id),
                    ).fetchone(), historical=False)
                raise StoreError("source_id", "a different snapshot already uses this source id")
            charges: dict[str, int] = {}
            if operation_id is None:
                charges = {"sources": 1, "source_bytes": len(encoded) + (len(raw) if raw else 0), "tool_calls": 1}
                grant = conn.execute(
                    "SELECT id FROM generic_grants WHERE goal_id=? AND body_hash=? AND state='active' ORDER BY id DESC LIMIT 1",
                    (goal_id, body_hash),
                ).fetchone()
                if grant is None:
                    raise StoreError("grant", "charging an operation snapshot requires an active grant")
                costs_check = self._admit_charges(conn, goal_id, body_hash, charges, int(grant["id"]))
                self._move_usage(conn, goal_id, body_hash, costs_check, used=+1)
            conn.execute(
                "INSERT INTO generic_snapshots(goal_id, source_id, body_hash, epoch, url, title, provenance, source_kind, label_json, text, text_hash, raw, raw_hash, content_type, snapshot_hash, fetched_at, operation_id, binding_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (goal_id, source_id, body_hash, epoch, url, title, provenance, source_kind,
                 _contract().canonical(labels), text, text_hash, raw,
                 _sha256(raw) if raw is not None else None, content_type, snapshot_hash,
                 int(fetched_at) if fetched_at is not None else self.now(), operation_id,
                 _contract().canonical(binding) if binding else None, self.now()),
            )
            self._event(conn, goal_id, "snapshot", {"source_id": source_id, "stale": int(stale)})
        view = self.conn.execute(
            "SELECT * FROM generic_snapshots WHERE goal_id=? AND source_id=?", (goal_id, source_id)
        ).fetchone()
        return self._snapshot_view(self.conn, view, historical=stale or body_hash != goal["current_body_hash"])

    @staticmethod
    def _snapshot_view(_conn, row, *, historical: bool) -> dict[str, Any]:
        text_value = row["text"]
        return {
            "source_id": row["source_id"],
            "body_hash": row["body_hash"],
            "url": row["url"],
            "title": row["title"],
            "provenance": row["provenance"],
            "source_kind": row["source_kind"],
            "label": json.loads(row["label_json"]),
            "text": text_value,
            "text_hash": row["text_hash"],
            "raw_hash": row["raw_hash"],
            "content_type": row["content_type"],
            "snapshot_hash": row["snapshot_hash"],
            "fetched_at": row["fetched_at"],
            "operation_id": row["operation_id"],
            "historical": bool(historical),
        }

    def _admit_charges(self, conn, goal_id: str, body_hash: str, charges: dict[str, int], grant_id: int) -> dict[str, int]:
        ceiling_rows = conn.execute(
            "SELECT body_json FROM generic_versions WHERE goal_id=? AND body_hash=?",
            (goal_id, body_hash),
        ).fetchone()
        ceilings = json.loads(ceiling_rows["body_json"])["ceilings"]
        usage = self._usage_rows(conn, goal_id, body_hash)
        clean: dict[str, int] = {}
        for counter, amount in charges.items():
            row = usage.get(counter, {"used": 0, "reserved": 0, "lost": 0})
            if row["used"] + row["reserved"] + row["lost"] + amount > int(ceilings[counter]):
                raise StoreError(counter, f"version lifetime ceiling {ceilings[counter]} reached for {counter}")
            clean[counter] = amount
        return clean

    def snapshots(self, goal_id: str, include_history: bool = False) -> list[dict[str, Any]]:
        goal = self._goal_row(self.conn, goal_id)
        rows = self.conn.execute(
            "SELECT * FROM generic_snapshots WHERE goal_id=?", (goal_id,)
        ).fetchall()
        out = []
        current = goal["current_body_hash"]
        epoch = int(goal["epoch"])
        for row in rows:
            historical = row["body_hash"] != current or int(row["epoch"]) != epoch
            if historical and not include_history:
                continue
            view = self._snapshot_view(self.conn, row, historical=historical)
            out.append(view)
        return out

    def snapshot_text(self, goal_id: str, source_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT text, raw, raw_hash, content_type FROM generic_snapshots WHERE goal_id=? AND source_id=?",
            (goal_id, source_id),
        ).fetchone()
        if row is None:
            raise StoreError("source_id", "snapshot not found")
        return {
            "text": row["text"],
            "raw": row["raw"],
            "raw_hash": row["raw_hash"],
            "content_type": row["content_type"],
        }

    # -- events -----------------------------------------------------------
    def _event(self, conn, goal_id: str, kind: str, body: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO generic_events(goal_id, kind, body, created_at) VALUES(?,?,?,?)",
            (goal_id, kind, _contract().canonical(body), self.now()),
        )

    def _event_all(self, conn, kind: str, body: dict[str, Any]) -> None:
        # Recovery is global maintenance; attribute it to the most recent goal.
        conn.execute(
            "INSERT INTO generic_events(goal_id, kind, body, created_at) VALUES(?, 'maintenance', ?, ?)",
            (self._maintenance_goal(conn), _contract().canonical({"recover": body}), self.now()),
        )

    @staticmethod
    def _maintenance_goal(conn) -> str:
        row = conn.execute(
            "SELECT id FROM generic_goals ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row is not None else ""

    def event(self, goal_id: str, kind: str, body: dict[str, Any]) -> None:
        self._check_writable()
        with self._tx() as conn:
            self._goal_row(conn, goal_id)
            self._event(conn, goal_id, kind, body)

    # -- claims -----------------------------------------------------------
    def _snapshot_row(self, conn, goal_id: str, source_id: str):
        return conn.execute(
            "SELECT * FROM generic_snapshots WHERE goal_id=? AND source_id=?",
            (goal_id, source_id),
        ).fetchone()

    def _current_claim_view(self, row) -> dict[str, Any]:
        review = json.loads(row["review_json"]) if row["review_json"] else None
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "snapshot_hash": row["snapshot_hash"],
            "quote": row["quote"],
            "start": int(row["start"]),
            "end": int(row["end"]),
            "quote_hash": row["quote_hash"],
            "state": row["state"],
            "provenance": row["provenance"],
            "source_kind": row["source_kind"],
            "kind": row["kind"],
            "criterion_ids": json.loads(row["criterion_ids"]),
            "basis_ids": json.loads(row["basis_ids"]),
            "score": row["score"],
            "text": row["text"],
            "url": row["url"],
            "label": json.loads(row["label_json"]),
            "conflict": bool(json.loads(row["metadata_json"] or "{}").get("conflict", False)),
            "producer": row["producer"],
            "producer_recipient": row["producer_recipient"],
            "review": review,
        }

    def add_claim(self, goal_id: str, claim: dict[str, Any], *, binding: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_writable()
        goal = self._goal_row(self.conn, goal_id)
        if binding is None:
            if goal["state"] != "running":
                raise StoreError("state", "claims require a running goal")
        elif not self.check_binding(binding):
            raise StoreError("binding", "claims require the current running binding")
        cid = claim.get("id")
        if not isinstance(cid, str) or not cid:
            raise StoreError("id", "claim id is required")
        source_id = claim.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise StoreError("source_id", "claim source_id is required")
        row = self._snapshot_row(self.conn, goal_id, source_id)
        if row is None:
            raise StoreError("source_id", "claim has no stored snapshot")
        if row["body_hash"] != goal["current_body_hash"] or int(row["epoch"]) != int(goal["epoch"]):
            raise StoreError("body_hash", "snapshot belongs to a retired version")
        kind = claim.get("kind", "source")
        if kind not in {"source", "reasoning"}:
            raise StoreError("kind", "claim kind must be source or reasoning")
        basis_ids = [str(item) for item in (claim.get("basis_ids") or [])]
        if kind == "reasoning":
            if not basis_ids:
                raise StoreError("basis_ids", "reasoning claims must resolve to kept source claims")
            for item in basis_ids:
                basis_row = self.conn.execute(
                    "SELECT state, body_hash FROM generic_claims WHERE goal_id=? AND id=?",
                    (goal_id, item),
                ).fetchone()
                if basis_row is None or basis_row["body_hash"] != goal["current_body_hash"] or basis_row["state"] != "kept":
                    raise StoreError("basis_ids", f"basis claim {item} is not a kept claim of this version")
        # Derived outputs inherit the union of their inputs' disclosure
        # restrictions: the snapshot's label plus private provenance stay,
        # and a model cannot declassify by omitting a label.
        provenance = row["provenance"]
        source_kind = row["source_kind"]
        source_labels = set(json.loads(row["label_json"] or "[]"))
        inherited = set(source_labels)
        if provenance in {"human_note", "legacy_lead"}:
            inherited.add("private")
        labels = sorted(inherited | set(claim.get("label") or []))
        metadata = {k: v for k, v in (claim.get("metadata") or {}).items() if isinstance(claim.get("metadata"), dict)}
        if claim.get("metadata") and not isinstance(claim.get("metadata"), dict):
            metadata = {}
        claimed_verdict = claim.get("verdict")
        if claimed_verdict is not None and claimed_verdict in REVIEW_STATES:
            metadata = {**metadata, "producer_verdict_refused": True}
        quote = claim.get("quote")
        start, end = claim.get("start"), claim.get("end")
        with self._tx() as conn:
            try:
                verified = _evidence().verify_quote(row["text"], quote, start, end)
            except _evidence().EvidenceError as exc:
                raise StoreError(exc.field, exc.message) from exc
            conn.execute(
                "INSERT INTO generic_claims(goal_id, id, body_hash, epoch, source_id, snapshot_hash, quote, start, end, quote_hash, state, provenance, source_kind, kind, criterion_ids, basis_ids, score, text, url, label_json, metadata_json, producer, producer_recipient, produced_at, binding_json, review_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, 'unknown', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (goal_id, cid, goal["current_body_hash"], int(goal["epoch"]), source_id, row["snapshot_hash"],
                 quote, int(verified["start"]), int(verified["end"]), verified["quote_hash"],
                 provenance, source_kind, kind,
                 _contract().canonical(sorted({str(item) for item in (claim.get("criterion_ids") or [])})),
                 _contract().canonical(basis_ids),
                 claim.get("score") if isinstance(claim.get("score"), (int, float)) else None,
                 str(claim.get("text") or ""),
                 row["url"],
                 _contract().canonical(labels),
                 _contract().canonical(metadata),
                 claim.get("producer"), claim.get("producer_recipient"),
                 self.now(), _contract().canonical(binding), self.now()),
            )
            self._event(conn, goal_id, "claim", {"claim_id": cid, "source_id": source_id})
        return self._claim_view(goal_id, cid)

    def _claim_view(self, goal_id: str, claim_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM generic_claims WHERE goal_id=? AND id=?", (goal_id, claim_id)
        ).fetchone()
        return self._current_claim_view(row)

    def claims(self, goal_id: str) -> list[dict[str, Any]]:
        goal = self._goal_row(self.conn, goal_id)
        rows = self.conn.execute(
            "SELECT * FROM generic_claims WHERE goal_id=? AND body_hash=? ORDER BY created_at",
            (goal_id, goal["current_body_hash"]),
        ).fetchall()
        return [self._current_claim_view(row) for row in rows]

    # -- review -----------------------------------------------------------
    def review_claim(self, goal_id: str, claim_id: str, verdict: str, *, reviewer: str, reason: str = "", binding: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_writable()
        if not self.check_binding(binding):
            raise StoreError("binding", "review requires the current running binding")
        goal = self._goal_row(self.conn, goal_id)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM generic_claims WHERE goal_id=? AND id=? AND body_hash=?",
                (goal_id, claim_id, goal["current_body_hash"]),
            ).fetchone()
            if row is None:
                raise StoreError("claim_id", "claim does not belong to the current version")
            if row["producer"] and reviewer and str(row["producer"]) == str(reviewer):
                raise StoreError("reviewer", "the producing model cannot review its own claim")
            try:
                checked = _evidence().review_claim(
                    {
                        "id": row["id"], "provenance": row["provenance"],
                        "source_kind": row["source_kind"], "quote_hash": row["quote_hash"],
                        "kind": row["kind"],
                    },
                    verdict,
                    [self._current_claim_view(basis) for basis in conn.execute(
                        "SELECT * FROM generic_claims WHERE goal_id=? AND body_hash=?",
                        (goal_id, goal["current_body_hash"]),
                    ).fetchall()],
                )
            except _evidence().EvidenceError as exc:
                raise StoreError(exc.field, exc.message) from exc
            review = {
                "claim_id": claim_id, "verdict": checked["state"], "reviewer": reviewer,
                "reason": reason, "at": self.now(),
            }
            conn.execute(
                "INSERT INTO generic_reviews(goal_id, claim_id, body_hash, verdict, reviewer, reason, binding_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (goal_id, claim_id, goal["current_body_hash"], checked["state"], reviewer, reason,
                 _contract().canonical(binding), self.now()),
            )
            conn.execute(
                "UPDATE generic_claims SET state=?, review_json=? WHERE goal_id=? AND id=?",
                (checked["state"], _contract().canonical(review), goal_id, claim_id),
            )
            self._event(conn, goal_id, "review", {"claim_id": claim_id, "verdict": checked["state"], "reviewer": reviewer})
            invalidated = 0
            if checked["state"] in {"disputed", "rejected", "superseded"}:
                invalidated = self._invalidate_dependents(conn, goal_id, claim_id)
        return {"claim": self._claim_view(goal_id, claim_id), "review": review, "invalidated_judgments": invalidated}

    def _invalidate_dependents(self, conn, goal_id: str, claim_id: str) -> int:
        rows = conn.execute(
            "SELECT id, body_json FROM generic_judgments WHERE goal_id=? AND body_hash=? AND invalid=0",
            (goal_id, self._goal_row(conn, goal_id)["current_body_hash"]),
        ).fetchall()
        count = 0
        for row in rows:
            body = json.loads(row["body_json"])
            cited = set(body.get("cited_claim_ids") or [])
            if claim_id in cited:
                conn.execute(
                    "UPDATE generic_judgments SET invalid=1 WHERE goal_id=? AND id=?",
                    (goal_id, row["id"]),
                )
                count += 1
        return count

    # -- judgments --------------------------------------------------------
    def add_judgment(self, goal_id: str, result: dict[str, Any], recipient_id: str, *, binding: dict[str, Any] | None = None, label: list[str] | None = None, model_id: str | None = None) -> dict[str, Any]:
        self._check_writable()
        if not self.check_binding(binding):
            raise StoreError("binding", "judgments require the current running binding")
        goal = self._goal_row(self.conn, goal_id)
        version = self._version_by_hash(goal_id, goal["current_body_hash"])
        body = version["body"]
        recipient = body["recipients"].get(recipient_id)
        if not isinstance(recipient, dict):
            raise StoreError("recipient", "judgment recipient is not approved")
        clearance = set(recipient.get("clearance") or [])
        claims = [c for c in self.claims(goal_id) if set(c.get("label") or []) <= clearance]
        visible = claims
        all_claims = {c["id"]: c for c in self.claims(goal_id)}
        for row in (result.get("rows") or []) + ([{"evidence_ids": (result.get("deliverable") or {}).get("evidence_ids")}] if isinstance(result.get("deliverable"), dict) else []):
            for item in (row.get("support_ids") or []) + (row.get("conflict_ids") or []) + (row.get("evidence_ids") or []):
                claim = all_claims.get(item)
                if claim is not None and not set(claim.get("label") or []) <= clearance:
                    raise StoreError("clearance", f"claim {item} exceeds the recipient clearance")
        prior = self.conn.execute(
            "SELECT evidence_json FROM generic_results WHERE goal_id=? AND body_hash=? ORDER BY seq DESC LIMIT 1",
            (goal_id, goal["current_body_hash"]),
        ).fetchone()
        if prior is None:
            raise StoreError("result", "a judgment requires an actual stored deliverable")
        evidence_ids = {str(item) for item in json.loads(prior["evidence_json"])}
        deliverable = result.get("deliverable")
        try:
            judged = _judge().validate_judge_result(
                body["rubric"], result, visible, [row["id"] for row in body["rubric"]["criteria"] if set(row["label"] or []) <= clearance],
                deliverable=deliverable,
            )
        except _judge().JudgeError as exc:
            raise StoreError(exc.field, exc.message) from exc
        if deliverable is not None:
            refs = set(deliverable.get("evidence_ids") or [])
            if not refs <= evidence_ids:
                raise StoreError("evidence_ids", "deliverable cites evidence absent from the stored result")
        outcome = "rubric_met" if _judge().rubric_met(body["rubric"], judged, [row["id"] for row in body["rubric"]["criteria"] if set(row["label"] or []) <= clearance]) else "partial"
        with self._tx() as conn:
            judgment_id = "judg-" + _sha256(_contract().canonical({
                "goal": goal_id, "hash": goal["current_body_hash"], "fingerprint": judged["fingerprint"],
                "seq": conn.execute("SELECT count(*) AS n FROM generic_judgments WHERE goal_id=?", (goal_id,)).fetchone()["n"] + 1,
            }))[:24]
            judged_with_citations = {
                **judged,
                "cited_claim_ids": sorted({
                    item
                    for row in judged["rows"]
                    for item in (row.get("support_ids") or []) + (row.get("conflict_ids") or [])
                } | set(deliverable.get("evidence_ids") or [] if isinstance(deliverable, dict) else [])),
            }
            conn.execute(
                "INSERT INTO generic_judgments(goal_id, id, body_hash, epoch, recipient_id, outcome, fingerprint, invalid, label_json, model_id, binding_json, created_at, body_json) "
                "VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?)",
                (goal_id, judgment_id, goal["current_body_hash"], int(goal["epoch"]), recipient_id, outcome,
                 judged["fingerprint"], _contract().canonical(sorted(set(label or []))), model_id,
                 _contract().canonical(binding), self.now(), _contract().canonical(judged_with_citations)),
            )
            self._event(conn, goal_id, "judgment", {"judgment_id": judgment_id, "outcome": outcome})
        return {
            "judgment_id": judgment_id, "outcome": outcome,
            "fingerprint": judged["fingerprint"], "judged": judged,
            "authorized_ids": [row["id"] for row in body["rubric"]["criteria"] if set(row["label"] or []) <= clearance],
        }

    def judgments(self, goal_id: str) -> list[dict[str, Any]]:
        goal = self._goal_row(self.conn, goal_id)
        rows = self.conn.execute(
            "SELECT * FROM generic_judgments WHERE goal_id=? AND body_hash=? ORDER BY created_at",
            (goal_id, goal["current_body_hash"]),
        ).fetchall()
        out = []
        for row in rows:
            body = json.loads(row["body_json"])
            out.append({
                "id": row["id"], "outcome": row["outcome"], "fingerprint": row["fingerprint"],
                "recipient": row["recipient_id"], "model_id": row["model_id"],
                "label": json.loads(row["label_json"]), "invalid": bool(row["invalid"]),
                "rows": body.get("rows", []), "deliverable": body.get("deliverable"),
                "created_at": row["created_at"],
            })
        return out

    # -- outcomes ---------------------------------------------------------
    def set_outcome(self, goal_id: str, outcome: str, reason: str = "", *, binding: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_writable()
        if outcome not in {"progressing", "rubric_met", "partial", "exhausted", "blocked", "outage", "stalled", "empty_frontier", "user_stopped"}:
            raise StoreError("outcome", "unknown outcome")
        goal = self._goal_row(self.conn, goal_id)
        if binding is not None:
            if not self.check_binding(binding):
                raise StoreError("binding", "outcome commits require the current running binding")
            if outcome != "progressing" and binding.get("body_hash") != goal["current_body_hash"]:
                raise StoreError("binding", "stale binding cannot set a terminal outcome")
        elif goal["state"] != "running":
            raise StoreError("state", "outcomes require a running goal")
        with self._tx() as conn:
            terminal = outcome not in {"progressing", None}
            if terminal:
                conn.execute(
                    "UPDATE generic_goals SET state='idle', outcome=?, reason=?, row_version=row_version+1 WHERE id=?",
                    (outcome, reason, goal_id),
                )
                conn.execute(
                    "UPDATE generic_grants SET state='retired' WHERE goal_id=? AND state IN ('active','paused')",
                    (goal_id,),
                )
            else:
                conn.execute(
                    "UPDATE generic_goals SET outcome=?, reason=? WHERE id=?",
                    (outcome, reason, goal_id),
                )
            self._event(conn, goal_id, "outcome", {"outcome": outcome, "reason": reason})
        return self.goal(goal_id)

    # -- tasks ------------------------------------------------------------
    @staticmethod
    def _task_fingerprint(task: dict[str, Any]) -> str:
        basis = {
            "tool": task.get("tool"),
            "url": task.get("url"),
            "query_text": task.get("query_text"),
            "source_id": task.get("source_id"),
        }
        return _sha256(_contract().canonical(basis))

    def record_task(self, goal_id: str, task: dict[str, Any], *, binding: dict[str, Any] | None = None) -> dict[str, Any]:
        self._check_writable()
        if not self.check_binding(binding):
            raise StoreError("binding", "tasks require the current running binding")
        goal = self._goal_row(self.conn, goal_id)
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise StoreError("task_id", "task id is required")
        fingerprint = task.get("fingerprint") or self._task_fingerprint(task)
        with self._tx() as conn:
            self._release_expired_leases(conn)
            by_id = conn.execute(
                "SELECT * FROM generic_draft_tasks WHERE goal_id=? AND body_hash=? AND task_id=? ORDER BY rowid DESC LIMIT 1",
                (goal_id, goal["current_body_hash"], task_id),
            ).fetchone()
            by_fp = conn.execute(
                "SELECT * FROM generic_draft_tasks WHERE goal_id=? AND body_hash=? AND fingerprint=? ORDER BY rowid DESC LIMIT 1",
                (goal_id, goal["current_body_hash"], fingerprint),
            ).fetchone()
            row = by_id if by_id is not None else by_fp
            if row is not None:
                # Completed work is never redone, and a recorded failure stays
                # terminal: no hidden automatic repeats. Retry requires a new
                # explicit task occurrence from the planner.
                return {**json.loads(row["task_json"]), "status": row["status"]}
            stored = dict(task)
            stored.setdefault("fingerprint", fingerprint)
            conn.execute(
                "INSERT INTO generic_draft_tasks(task_key, goal_id, body_hash, task_id, fingerprint, status, task_json, binding_json, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"{goal_id}|{goal['current_body_hash']}|{task_id}", goal_id,
                 goal["current_body_hash"], task_id, fingerprint, "queued",
                 _contract().canonical(stored), _contract().canonical(binding),
                 self.now(), self.now()),
            )
            self._event(conn, goal_id, "task", {"task_id": task_id, "status": "queued"})
        return {**stored, "status": "queued"}

    def tasks(self, goal_id: str, include_history: bool = False) -> list[dict[str, Any]]:
        goal = self._goal_row(self.conn, goal_id)
        if include_history:
            rows = self.conn.execute(
                "SELECT * FROM generic_draft_tasks WHERE goal_id=? ORDER BY rowid", (goal_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM generic_draft_tasks WHERE goal_id=? AND body_hash=? ORDER BY rowid",
                (goal_id, goal["current_body_hash"]),
            ).fetchall()
        return [
            {
                "id": row["task_id"], "fingerprint": row["fingerprint"], "status": row["status"],
                "attempt": int(row["attempt"]),
                **{k: v for k, v in json.loads(row["task_json"]).items() if k != "id"},
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def finish_task(self, goal_id: str, task_id: str, status: str, result: dict[str, Any] | None = None, *, binding: dict[str, Any] | None = None) -> None:
        self._check_writable()
        if status not in {"done", "failed"}:
            raise StoreError("status", "task status is done or failed")
        if not self.check_binding(binding):
            raise StoreError("binding", "task completion requires the current running binding")
        goal = self._goal_row(self.conn, goal_id)
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT rowid FROM generic_draft_tasks WHERE goal_id=? AND body_hash=? AND task_id=? AND status='queued'",
                (goal_id, goal["current_body_hash"], task_id),
            ).fetchall()
            if not rows:
                raise StoreError("task_id", "no queued task to finish")
            latest = rows[-1]["rowid"]
            result_json = _contract().canonical(result) if isinstance(result, dict) else None
            cursor = conn.execute(
                "UPDATE generic_draft_tasks SET status=?, result_json=?, updated_at=? WHERE rowid=?",
                (status, result_json, self.now(), latest),
            )
            if cursor.rowcount != 1:
                raise StoreError("task_id", "task completion lost a race")
            self._event(conn, goal_id, "task_finished", {"task_id": task_id, "status": status})

    def _release_expired_leases(self, conn) -> None:
        conn.execute("DELETE FROM generic_domain_leases WHERE lease_until < ?", (self.now(),))

    # -- results ----------------------------------------------------------
    def save_result(self, goal_id: str, deliverable: dict[str, Any], *, binding: dict[str, Any] | None = None) -> None:
        self._check_writable()
        if not self.check_binding(binding):
            raise StoreError("binding", "results require the current running binding")
        if not isinstance(deliverable, dict) or not isinstance(deliverable.get("text"), str):
            raise StoreError("deliverable", "a deliverable needs text")
        goal = self._goal_row(self.conn, goal_id)
        claims = {c["id"] for c in self.claims(goal_id)}
        evidence = [item for item in (deliverable.get("evidence_ids") or []) if item in claims]
        kept = {c["id"] for c in self.claims(goal_id) if c["state"] == "kept"}
        if not set(evidence) <= kept:
            raise StoreError("evidence_ids", "deliverable evidence must cite kept claims")
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO generic_results(goal_id, body_hash, epoch, text, label_json, evidence_json, model_id, binding_json, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (goal_id, goal["current_body_hash"], int(goal["epoch"]), deliverable["text"],
                 _contract().canonical(sorted(set(deliverable.get("label") or []))),
                 _contract().canonical(evidence), deliverable.get("model_id"),
                 _contract().canonical(binding), self.now()),
            )
            self._event(conn, goal_id, "result", {"evidence_ids": evidence})

    def result(self, goal_id: str) -> dict[str, Any] | None:
        goal = self._goal_row(self.conn, goal_id)
        row = self.conn.execute(
            "SELECT * FROM generic_results WHERE goal_id=? AND body_hash=? ORDER BY seq DESC LIMIT 1",
            (goal_id, goal["current_body_hash"]),
        ).fetchone()
        if row is None:
            return None
        return {
            "text": row["text"], "label": json.loads(row["label_json"]),
            "evidence_ids": json.loads(row["evidence_json"]), "model_id": row["model_id"],
            "created_at": row["created_at"],
        }

    # -- calls / turns / context -----------------------------------------
    def record_call(self, goal_id: str, receipt: dict[str, Any], *, binding: dict[str, Any] | None = None) -> None:
        self._check_writable()
        if not isinstance(receipt, dict):
            raise StoreError("receipt", "receipt must be an object")
        goal = self._goal_row(self.conn, goal_id)
        current = self.check_binding(binding)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO generic_calls(goal_id, body_hash, stale, binding_json, receipt, created_at) VALUES(?,?,?,?,?,?)",
                (goal_id, goal["current_body_hash"], 0 if current else 1,
                 _contract().canonical(binding) if binding else None,
                 _contract().canonical(receipt), self.now()),
            )
            self._event(conn, goal_id, "call", {"stale": 0 if current else 1, "role": receipt.get("role")})

    def record_turn(self, goal_id: str, *, binding: dict[str, Any] | None = None) -> int:
        self._check_writable()
        if not self.check_binding(binding):
            raise StoreError("binding", "turns require the current running binding")
        goal = self._goal_row(self.conn, goal_id)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO generic_turns(goal_id, body_hash) VALUES(?,?) "
                "ON CONFLICT(goal_id, body_hash) DO UPDATE SET turns=turns+1",
                (goal_id, goal["current_body_hash"]),
            )
            turns = conn.execute(
                "SELECT turns FROM generic_turns WHERE goal_id=? AND body_hash=?",
                (goal_id, goal["current_body_hash"]),
            ).fetchone()["turns"]
            self._event(conn, goal_id, "turn", {"turns": int(turns)})
        return int(turns)

    def save_context(self, goal_id: str, selected_ids: list[str], *, binding: dict[str, Any] | None = None) -> int:
        self._check_writable()
        if not self.check_binding(binding):
            raise StoreError("binding", "context selection requires the current running binding")
        goal = self._goal_row(self.conn, goal_id)
        known = {c["id"] for c in self.claims(goal_id)}
        clean = [str(item) for item in selected_ids if str(item) in known]
        with self._tx() as conn:
            row = conn.execute(
                "SELECT context_version FROM generic_turns WHERE goal_id=? AND body_hash=?",
                (goal_id, goal["current_body_hash"]),
            ).fetchone()
            version = (int(row["context_version"]) if row is not None else 0) + 1
            conn.execute(
                "INSERT INTO generic_turns(goal_id, body_hash, turns, context_version) VALUES(?,?,0,?) "
                "ON CONFLICT(goal_id, body_hash) DO UPDATE SET context_version=excluded.context_version",
                (goal_id, goal["current_body_hash"], version),
            )
            conn.execute(
                "DELETE FROM generic_context_items WHERE goal_id=? AND body_hash=? AND context_version=?",
                (goal_id, goal["current_body_hash"], version),
            )
            for item in dict.fromkeys(clean):
                conn.execute(
                    "INSERT INTO generic_context_items(goal_id, body_hash, context_version, item_id) VALUES(?,?,?,?)",
                    (goal_id, goal["current_body_hash"], version, item),
                )
            self._event(conn, goal_id, "context", {"context_version": version, "selected": len(clean)})
        return version

    def turns(self, goal_id: str) -> dict[str, int]:
        goal = self._goal_row(self.conn, goal_id)
        row = self.conn.execute(
            "SELECT turns, context_version FROM generic_turns WHERE goal_id=? AND body_hash=?",
            (goal_id, goal["current_body_hash"]),
        ).fetchone()
        return {"turns": int(row["turns"]) if row is not None else 0,
                "context_version": int(row["context_version"]) if row is not None else 0}

    # -- usage view -------------------------------------------------------
    def usage(self, goal_id: str) -> dict[str, Any]:
        goal = self._goal_row(self.conn, goal_id)
        usage = self._usage_rows(self.conn, goal_id, goal["current_body_hash"]) if goal["current_body_hash"] else {}
        if goal["current_body_hash"]:
            ceilings = self._maybe_version(goal_id)["body"]["ceilings"]
        else:
            ceilings = {}
        return {
            "used": {k: v["used"] for k, v in usage.items()},
            "reserved": {k: v["reserved"] for k, v in usage.items()},
            "lost": {k: v["lost"] for k, v in usage.items()},
            "limits": dict(ceilings),
            "headroom": self.headroom(goal_id),
            "overrun": bool(int(goal["overrun"])),
        }

    # -- graph ------------------------------------------------------------
    def _graph(self, goal_id: str) -> dict[str, Any]:
        goal = self._goal_row(self.conn, goal_id)
        if not goal["current_body_hash"]:
            return {"nodes": [], "edges": []}
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        version = self._version_by_hash(goal_id, goal["current_body_hash"])
        for row in version["body"]["rubric"]["criteria"]:
            nodes[f"criterion:{row['id']}"] = {"kind": "criterion", "label": row["description"]}
        for snapshot in self.snapshots(goal_id):
            if snapshot.get("historical"):
                continue
            source_id = snapshot["source_id"]
            nodes[f"source:{source_id}"] = {"kind": "source", "label": snapshot.get("url") or snapshot.get("title") or source_id}
        for claim in self.claims(goal_id):
            nodes[f"claim:{claim['id']}"] = {"kind": "claim", "label": (claim.get("text") or claim.get("quote") or "")[:120], "state": claim["state"]}
            src = f"source:{claim['source_id']}"
            if src in nodes:
                edges.append({"src": src, "dst": f"claim:{claim['id']}", "kind": "evidences"})
            for cid in claim.get("criterion_ids") or []:
                target = f"criterion:{cid}"
                if target in nodes:
                    edges.append({"src": f"claim:{claim['id']}", "dst": target, "kind": "supports" if claim["state"] == "kept" else "critiques"})
            for item in claim.get("basis_ids") or []:
                if f"claim:{item}" in nodes:
                    edges.append({"src": f"claim:{item}", "dst": f"claim:{claim['id']}", "kind": "derives"})
        return {"nodes": list(nodes.values()), "edges": edges}

    # -- full read view ---------------------------------------------------
    def view(self, goal_id: str) -> dict[str, Any]:
        goal = self._goal_row(self.conn, goal_id)
        version = self._maybe_version(goal_id)
        grant = None
        if goal["current_body_hash"]:
            row = self.conn.execute(
                "SELECT * FROM generic_grants WHERE goal_id=? AND body_hash=? AND state='active' ORDER BY id DESC LIMIT 1",
                (goal_id, goal["current_body_hash"]),
            ).fetchone()
            if row is not None:
                grant = {"grant_id": int(row["id"]), "state": row["state"], "limits": json.loads(row["limits_json"])}
        events = [
            {"id": row["id"], "kind": row["kind"], "body": json.loads(row["body"]), "created_at": row["created_at"]}
            for row in self.conn.execute(
                "SELECT id, kind, body, created_at FROM generic_events WHERE goal_id=? ORDER BY id", (goal_id,)
            )
        ]
        calls = [
            {"id": row["id"], "stale": bool(row["stale"]), "receipt": json.loads(row["receipt"]), "created_at": row["created_at"]}
            for row in self.conn.execute(
                "SELECT id, stale, receipt, created_at FROM generic_calls WHERE goal_id=? ORDER BY id", (goal_id,)
            )
        ]
        return {
            "goal": goal,
            "version": version,
            "grant_id": grant["grant_id"] if grant is not None else None,
            "grant": grant,
            "draft": self.read_draft(goal_id),
            "tasks": self.tasks(goal_id),
            "claims": self.claims(goal_id),
            "snapshots": self.snapshots(goal_id),
            "judgments": self.judgments(goal_id),
            "events": events,
            "calls": calls,
            "result": self.result(goal_id),
            "usage": self.usage(goal_id),
            "turns": self.turns(goal_id)["turns"],
            "context_version": self.turns(goal_id)["context_version"],
            "graph": self._graph(goal_id),
        }
