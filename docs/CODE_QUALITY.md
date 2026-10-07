# Snowball Code Quality & Architectural Integrity Standards

**Goal**: Prevent spaghetti code, maintain strict architectural boundaries, and enforce deterministic testability across the autonomous 24/7 research loop.

---

## 1. The Core Architectural Invariants

| Invariant | Rule | Enforcement Mechanism |
|---|---|---|
| **I1: Code Seeds, The Web Supplies** | Models never invent topics. The crawler follows entities and hubs discovered on the web. |  drops any candidate topic without a web-discovered source URL. |
| **I2: The LLM Never Judges Novelty** | Deterministic code decides if a page, query, entity, or claim is new. | 5-Layer Lock Engine executes before any LLM sees the data. |
| **I3: Zero Tools for Inference Models** | Extraction and verification models receive plain text and return validated JSON schemas. | Model API calls strip tool definitions; models cannot invoke shell/web tools. |
| **I4: Verbatim Citation Guard** | Every claim must contain a verbatim quote matching substring in raw fetched HTML/text. | Mechanical substring matching in  drops unverified claims. |
| **I5: Egress Security (SSRF Guard)** | Outbound web fetches must resolve DNS pre-socket and reject internal/cloud IP ranges. |  pins IPs; rejects RFC 1918, loopback, and . |

---

## 2. Anti-Spaghetti Structural Boundaries



1. **Strict Layer Decoupling**:
   - The **Orchestrator** coordinates DAG waves; it never performs raw HTTP requests directly.
   - The **Model Engine Layer** is stateless: engines accept strings, call configured APIs, and return typed data. They never mutate the database directly.
   - The **Storage Layer** handles all state mutations exclusively through  transactions to prevent SQLite concurrency deadlocks.
2. **No Circular or Shadowed Imports**:
   - Internal modules in  must never shadow Python standard library modules (e.g. , , ).
   - All modules use  and explicit type annotations.

---

## 3. Static Analysis & Linting Configuration

Snowball enforces strict static analysis via **Ruff** in :
- **Selected Rulesets**:
  - , : PEP 8 style conventions.
  - : Pyflakes (undefined variables, unused imports, duplicate keys).
  - : isort (deterministic import order).
  - : flake8-bugbear (common design gotchas, mutable default arguments).
  - : flake8-comprehensions (optimized list/dict comprehensions).
  - : pyupgrade (modern Python 3.10+ syntax).
  - : flake8-simplify (streamlined control flow).
- **Complexity Threshold**:
  - McCabe Cyclomatic Complexity capped at **18**. Functions exceeding this complexity must be broken down into modular sub-routines.
- **Line Length**: 120 characters maximum.

---

## 4. Testing & Verification Requirements

Every pull request and commit must pass three gates:
1. **Unit & Isolation Tests ()**:
   - Offline tests with mocked engines verifying URL normalization, SSRF blocking, substring quote guards, and lock behaviors.
2. **CLI Contract Verification**:
   -  must execute successfully without mutating the live database.
3. **Prompt Injection Quarantine**:
   -  scans untrusted input handling against prompt injection patterns.
