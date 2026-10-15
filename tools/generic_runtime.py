"""Goal-scoped research orchestration. Models propose; durable code admits and commits."""
from __future__ import annotations

import hashlib
import json
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import generic_contract as contract
from tools import generic_context as context
from tools import generic_interview as interview
from tools import generic_store as storage


class RuntimeError(ValueError):
    def __init__(self, field: str, message: str):
        self.field, self.message = field, message
        super().__init__(message)


class OperatorStopped(Exception):
    """A process stop request leaves the user's goal unchanged."""


def _id(value: Any) -> str:
    return hashlib.sha256(contract.canonical(value).encode("utf-8")).hexdigest()[:24]


def _visible(rows, clearance):
    return [row for row in rows if set(row.get("label") or []) <= set(clearance)]


_INTERNAL_FIELDS = frozenset({"body_hash", "current_body_hash", "goal_id", "epoch", "grant_id", "operation_id", "binding", "body_json", "label_json", "raw_bytes"})


def _outbound(value):
    if isinstance(value, dict):
        return {key: _outbound(item) for key, item in value.items() if key not in _INTERNAL_FIELDS}
    if isinstance(value, list):
        return [_outbound(item) for item in value]
    return value


def _labels(value):
    if isinstance(value, dict):
        return set(value.get("label") or []) | set().union(*(_labels(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_labels(item) for item in value))
    return set()

class Service:
    def __init__(self, db_path: Path, roles=None, acquisition=None):
        self.db_path = Path(db_path)
        self._roles = roles
        self._acquisition = acquisition

    @property
    def roles(self):
        if self._roles is None:
            from tools.generic_roles import RoleClient
            self._roles = RoleClient()
        return self._roles

    @property
    def acquisition(self):
        if self._acquisition is None:
            from tools.generic_acquisition import Acquisition
            self._acquisition = Acquisition()
        return self._acquisition

    def _open(self, *, initialize=False, readonly=False):
        return storage.open_store(self.db_path, initialize=initialize, readonly=readonly)

    def create(self, title: str, goal_id: str | None = None):
        store = self._open(initialize=True)
        try:
            goal_id = goal_id or "goal-" + secrets.token_hex(6)
            store.create_goal(goal_id, title)
            store.save_draft(goal_id, interview.new_draft(goal_id))
            return store.view(goal_id)
        finally:
            store.close()

    def view(self, goal_id):
        store = self._open(readonly=True)
        try:
            return store.view(goal_id)
        finally:
            store.close()

    def list(self):
        if not self.db_path.exists():
            return []
        store = self._open(readonly=True)
        try:
            return store.list_goals()
        finally:
            store.close()

    def answer(self, goal_id, question_id, answer, row_version=None):
        store = self._open()
        try:
            draft = store.read_draft(goal_id) or interview.new_draft(goal_id)
            question = next((q for q in draft["questions"] if q["id"] == question_id), None)
            if question is None:
                raise RuntimeError("question_id", "question does not belong to this draft")
            interview.apply_answer(draft, question, answer)
            draft.pop("proposal", None)
            store.save_draft(goal_id, draft, row_version)
            return store.view(goal_id)
        finally:
            store.close()

    def consent(self, goal_id, recipient, refs, allowance, recipients, row_version=None):
        store = self._open()
        try:
            draft = store.read_draft(goal_id) or interview.new_draft(goal_id)
            interview.record_consent(draft, recipient, refs, allowance, recipients)
            draft["recipients"] = recipients
            store.save_draft(goal_id, draft, row_version)
            return store.view(goal_id)
        finally:
            store.close()

    def interview(self, goal_id):
        store = self._open()
        try:
            current = store.goal(goal_id)
            draft = store.read_draft(goal_id)
            if current["state"] not in {"draft", "revising"}:
                raise RuntimeError("state", "interview requires an editable draft")
            recipient = self.roles.recipient("interview")
            recipients = draft.get("recipients", {})
            payload = interview.authorized_inputs(draft, recipient, recipients)
            max_tokens = getattr(self.roles, "max_output_tokens", 2048)
            costs = self.roles.reservation("interview", payload, max_tokens)
            consent = draft.get("consent") or {}
            allowance = consent.get("allowance") or {}
            reserved = costs.get("model_tokens", 0)
            if consent.get("tokens_used", 0) + reserved > allowance.get("model_tokens", 0):
                raise RuntimeError("model_tokens", "interview token allowance is insufficient")
            interview.consume_consent(draft, recipient)
            draft["consent"]["tokens_used"] = consent.get("tokens_used", 0) + reserved
            saved = store.save_draft(goal_id, draft, current["row_version"])
            expected = saved["row_version"]
        finally:
            store.close()
        # Durable allowance was consumed before external I/O; failed calls stay charged.
        receipt = self.roles.call("interview", payload, max_tokens=max_tokens)
        output = receipt["output"]
        store = self._open()
        try:
            current = store.goal(goal_id)
            if current["row_version"] != expected or current["state"] not in {"draft", "revising"}:
                store.event(goal_id, "stale_interview", {"role": "interview"})
                raise RuntimeError("row_version", "draft changed during the interview call")
            draft = store.read_draft(goal_id)
            labels = sorted(_labels(payload.get("inputs", {})))
            followups = interview.validate_followups(output.get("questions", []), existing_ids=[q["id"] for q in draft["questions"]], input_labels=labels)
            draft["questions"].extend(followups)
            draft["suggestion"] = {"rubric": output.get("rubric"), "brief": output.get("brief"), "model_id": receipt.get("model_id"), "label": labels}
            store.save_draft(goal_id, draft, expected)
            store.event(goal_id, "interview_call", {k: v for k, v in receipt.items() if k != "output"})
            return store.view(goal_id)
        finally:
            store.close()

    def propose(self, goal_id, options=None):
        options = options or {}
        view = self.view(goal_id)
        draft = view.get("draft") or {}
        if not draft.get("suggestion") and not (options.get("rubric") and options.get("brief")):
            self.interview(goal_id)
        store = self._open()
        try:
            current = store.goal(goal_id)
            draft = store.read_draft(goal_id)
            suggestion = draft.get("suggestion") or {}
            rubric = options.get("rubric") or suggestion.get("rubric")
            brief = options.get("brief") or suggestion.get("brief")
            if not rubric or not brief:
                raise RuntimeError("proposal", "interview did not propose a rubric and brief")
            # Rewriting from private inputs is not declassification.
            if suggestion.get("label") and "brief" not in options:
                brief = {**brief, "label": sorted(set(brief.get("label", [])) | set(suggestion["label"]))}
            prior = store.version(goal_id) if current.get("current_body_hash") else None
            proposal = interview.propose_contract(
                draft, public_id=secrets.token_hex(16), rubric=rubric, brief=brief,
                ceilings=options.get("ceilings", draft.get("ceilings")),
                recipients=options.get("recipients", draft.get("recipients", {})),
                seeds=options.get("seeds", draft.get("seeds", [])),
                concurrency=options.get("concurrency", 1),
                stopping_policy=options.get("stopping_policy", {"on_exhaustion": "partial", "on_empty_frontier": "partial", "stall_checkpoints": 2}),
                revision=(prior["body"]["revision"] + 1) if prior else 1,
                parent_body_hash=prior["body_hash"] if prior else None,
            )
            draft["proposal"] = proposal
            store.save_draft(goal_id, draft, current["row_version"])
            return {**store.view(goal_id), **proposal}
        finally:
            store.close()

    def preview(self, goal_id, payload, row_version=None):
        store = self._open(readonly=True)
        try:
            current = store.goal(goal_id)
            if current["state"] not in {"draft", "revising"}:
                raise RuntimeError("state", "only an editable draft can be reviewed")
            if row_version is not None and row_version != current["row_version"]:
                raise RuntimeError("row_version", "draft changed before review")
            if not isinstance(payload, dict) or payload.get("goal_id") != goal_id:
                raise RuntimeError("goal_id", "proposal belongs to a different goal")
            version = contract.build_version(payload)
            return {"goal": current, "payload": payload, "version": version}
        finally:
            store.close()

    def approve(self, goal_id, payload, row_version, body_hash):
        store = self._open()
        try:
            store.approve(goal_id, payload, row_version, body_hash)
            return store.view(goal_id)
        finally:
            store.close()

    def grant(self, goal_id, limits=None, row_version=None):
        store = self._open()
        try:
            grant = store.grant(goal_id, limits if limits is not None else store.approved_ceilings(goal_id), row_version)
            return {**store.view(goal_id), **grant}
        finally:
            store.close()

    def _transition(self, method, goal_id, row_version=None, grant_id=None):
        store = self._open()
        try:
            if method == "start":
                store.start(goal_id, grant_id, row_version)
            else:
                getattr(store, method)(goal_id, row_version)
            return store.view(goal_id)
        finally:
            store.close()

    def start(self, goal_id, row_version=None, grant_id=None):
        return self._transition("start", goal_id, row_version, grant_id)

    def pause(self, goal_id, row_version=None):
        return self._transition("pause", goal_id, row_version)

    def resume(self, goal_id, row_version=None):
        return self._transition("resume", goal_id, row_version)

    def stop(self, goal_id, row_version=None):
        return self._transition("stop", goal_id, row_version)

    def revise(self, goal_id, row_version=None):
        return self._transition("revise", goal_id, row_version)

    def _recipient(self, version, role):
        recipient_id = self.roles.recipient(role)
        recipient = version["body"]["recipients"].get(recipient_id)
        if not recipient or recipient.get("kind") != "model":
            raise RuntimeError("recipient", f"approve the configured {role} recipient before dispatch")
        return recipient_id, recipient.get("clearance", [])

    def _payload(self, store, goal_id, role, extra=None):
        version = store.version(goal_id)
        recipient, clearance = self._recipient(version, role)
        if role in {"planner", "research", "review"}:
            # Public acquisition producers must never ingest private context,
            # even when their configured model has broader permissions.
            clearance = []
        criteria = _visible(version["body"]["rubric"]["criteria"], clearance) if clearance else version["public"]["criteria"]
        # Private context requires an explicitly approved recipient, never a public summary shortcut.
        brief = version["body"]["public_brief"]
        if not set(brief.get("label") or []) <= set(clearance):
            raise RuntimeError("clearance", "recipient cannot receive the research brief")
        payload = {"version_id": version["public_id"], "public_hash": version["public_hash"], "brief": brief["text"], "rubric": {"criteria": criteria}}
        if role in {"synthesis", "judge"}:
            requested = next(row for row in version["body"]["answers"] if row["question_id"] == "deliverable")
            if not set(requested["label"]) <= set(clearance):
                raise RuntimeError("clearance", "this role cannot assess the locked requested deliverable")
            payload["requested_deliverable"] = {"text": version["body"]["deliverable"], "label": requested["label"]}
        claims = _visible(store.claims(goal_id), clearance)
        outstanding = [c["id"] for c in criteria]
        selection = context.select_context(claims, 40, 12000, outstanding, clearance=clearance)
        selected = set(selection["selected_ids"])
        payload["claims"] = [row for row in claims if row["id"] in selected]
        payload["context_version"] = store.view(goal_id).get("context_version", 0)
        payload["deferred_context"] = selection.get("deferred", [])
        if extra:
            payload.update(extra)
        labels = _labels(payload)
        if not labels <= set(clearance):
            raise RuntimeError("clearance", "derived artifact exceeds this recipient's clearance")
        payload["input_label"] = sorted(labels)
        outbound = _outbound(payload)
        if len(contract.canonical(outbound).encode("utf-8")) > 64000:
            raise RuntimeError("context_limit", "outbound context exceeds the 64000-byte hard bound")
        return recipient, outbound

    def _dispatch(self, store, operation, binding):
        if (ROOT / "STOP").exists():
            store.settle(operation["id"], {})
            raise OperatorStopped()
        if operation.get("binding") != binding or not store.check_binding(binding):
            store.settle(operation["id"], {})
            raise RuntimeError("binding", "reservation belongs to a different execution")
        dispatched = store.dispatch(operation["id"])
        if dispatched != binding:
            store.settle(operation["id"], {})
            raise RuntimeError("binding", "dispatch belongs to a different execution")
        if (ROOT / "STOP").exists():
            store.settle(operation["id"], {})
            raise OperatorStopped()

    def _call(self, store, goal_id, role, payload, *, binding, max_tokens=None):
        max_tokens = max_tokens or getattr(self.roles, "max_output_tokens", 2048)
        def audit(receipt):
            return {
                **{k: v for k, v in receipt.items() if k != "output"},
                "context_version": payload.get("context_version", 0),
                "source_ids": [s["source_id"] for s in payload.get("sources", [])],
                "claim_ids": [c["id"] for c in payload.get("claims", [])],
                "version_id": payload.get("version_id"), "label": payload.get("input_label", []),
            }
        if not store.check_binding(binding):
            raise RuntimeError("binding", "research binding is no longer runnable")
        costs = self.roles.reservation(role, payload, max_tokens)
        operation = store.reserve(goal_id, costs, "model:" + role, secrets.token_hex(16))
        self._dispatch(store, operation, binding)
        try:
            receipt = self.roles.call(role, payload, max_tokens=max_tokens)
        except Exception as exc:
            receipt = getattr(exc, "receipt", None)
            actual = None
            usage_known = bool(receipt) and receipt.get("usage_kind") in {"actual", "actual_reasoning"}
            if usage_known:
                actual = {"model_calls": 1, "model_tokens": (receipt.get("tokens_in") or 0) + (receipt.get("tokens_out") or 0)}
            store.settle(operation["id"], actual, lost=not usage_known)
            if receipt:
                store.record_call(goal_id, audit(receipt), binding=binding)
            raise
        actual = {"model_calls": 1, "model_tokens": (receipt.get("tokens_in") or 0) + (receipt.get("tokens_out") or 0)}
        usage_known = receipt.get("usage_kind") in {"actual", "actual_reasoning"}
        if not usage_known:
            actual = None
        valid = store.settle(operation["id"], actual, lost=not usage_known)
        store.record_call(goal_id, audit(receipt), binding=binding)
        if not valid or not store.check_binding(binding):
            raise RuntimeError("binding", "model response belongs to a retired execution")
        return receipt

    def _public_context(self, version, sources):
        public = {"brief": version["public"]["brief"] or ""}
        public.update({"criterion:" + row["id"]: row["description"] for row in version["public"]["criteria"]})
        remaining = max(0, 12000 - len(contract.canonical(public).encode("utf-8")))
        for row in sources:
            if row.get("label") or row.get("provenance") != "public_fetch" or remaining <= 100:
                continue
            excerpt = row["text"].encode("utf-8")[:min(2000, remaining - 100)].decode("utf-8", "ignore")
            public["source:" + row["source_id"]] = excerpt
            remaining -= len(excerpt.encode("utf-8")) + len(row["source_id"]) + 16
        return public

    def _task(self, task, version, public_context, known_urls, snapshots):
        from tools.generic_acquisition import canonical_url
        if not isinstance(task, dict) or task.get("tool") not in {"search", "fetch", "read"}:
            raise RuntimeError("tool", "only search, public fetch and stored source reads are available")
        criterion = task.get("criterion_id")
        if criterion not in {r["id"] for r in version["public"]["criteria"]}:
            raise RuntimeError("criterion_id", "task must target an approved public criterion")
        tool = task["tool"]
        refs = task.get("public_context_ids") or ["brief"]
        if not isinstance(refs, list) or any(ref not in public_context for ref in refs):
            raise RuntimeError("public_context_ids", "task context is outside the public projection")
        reason = task.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 2000:
            raise RuntimeError("reason", "task rationale must be bounded text")
        clean = {"criterion_id": criterion, "tool": tool, "public_context_ids": refs, "reason": reason}
        if tool == "fetch":
            url = canonical_url(task.get("url", ""))
            if not url or url not in known_urls:
                raise RuntimeError("url", "fetch target is not a seed or discovered public link")
            existing = next((row for row in snapshots if canonical_url(row.get("url", "")) == url and not row.get("label")), None)
            if existing:
                clean.update(tool="read", source_id=existing["source_id"])
            else:
                clean["url"] = url
        elif tool == "search":
            query = task.get("query_text")
            if not isinstance(query, str) or not query.strip() or len(query) > 1000:
                raise RuntimeError("query_text", "search requires a bounded public-derived query")
            clean["query_text"] = query.strip()
        else:
            source_id = task.get("source_id")
            if source_id not in {s["source_id"] for s in snapshots if not s.get("label")}:
                raise RuntimeError("source_id", "stored source is unavailable in public context")
            clean["source_id"] = source_id
        clean["id"] = "task-" + _id({key: clean.get(key) for key in ("tool", "url", "query_text", "source_id")})
        return clean

    def _tools(self, store, goal_id, tasks, binding, known_urls):
        from tools.generic_acquisition import canonical_url
        from urllib.parse import urlparse
        version = store.version(goal_id)
        fresh_sources, pending = [], []
        seen, domains = set(), set()
        for task in tasks:
            if task["id"] in seen:
                continue
            seen.add(task["id"])
            if (ROOT / "STOP").exists():
                for _, op, _ in pending:
                    store.settle(op["id"], {})
                raise OperatorStopped()
            existing = store.record_task(goal_id, task, binding=binding)
            if existing.get("status") in {"done", "failed", "running"}:
                continue
            kind = task["tool"]
            permission = version["body"]["recipients"].get("fetch" if kind == "fetch" else "search")
            if kind != "read" and (not permission or permission.get("kind") != kind or permission.get("clearance")):
                for _, op, _ in pending:
                    store.settle(op["id"], {})
                raise RuntimeError("recipient", "public acquisition recipient permission is missing")
            domain = urlparse(task.get("url", "")).hostname if kind == "fetch" else None
            if domain and domain in domains:
                store.event(goal_id, "task_deferred", {"task_id": task["id"], "reason": "domain_busy"})
                continue
            headroom = store.headroom(goal_id)
            remaining_fetches = max(1, sum(t["tool"] == "fetch" and t["id"] not in {p[0]["id"] for p in pending} for t in tasks))
            max_bytes = min(5242880, headroom.get("source_bytes", 0) // remaining_fetches)
            costs = {"expansion_tasks": 1, "tool_calls": 1}
            if kind == "fetch":
                if max_bytes < 1:
                    for _, held, _ in pending:
                        store.settle(held["id"], {})
                    raise RuntimeError("source_bytes", "no remaining source-byte allowance")
                costs.update({"sources": 1, "source_bytes": max_bytes})
            try:
                op = store.reserve(goal_id, costs, "tool:" + kind, task["id"], domain=domain)
                if op.get("binding") != binding:
                    store.settle(op["id"], {})
                    raise RuntimeError("binding", "tool reservation belongs to a replacement execution")
            except Exception as exc:
                if getattr(exc, "field", None) in {"domain", "domain_busy", "concurrency"}:
                    store.event(goal_id, "task_deferred", {"task_id": task["id"], "reason": getattr(exc, "field")})
                    continue
                for _, held, _ in pending:
                    store.settle(held["id"], {})
                raise
            pending.append((task, op, max_bytes))
            if domain:
                domains.add(domain)
        if not pending:
            return [], 0

        def acquire(item):
            task, _, max_bytes = item
            if task["tool"] == "search":
                return self.acquisition.search(task["query_text"], limit=5)
            if task["tool"] == "fetch":
                return self.acquisition.fetch(task["url"], max_bytes=max_bytes)
            return next(row for row in self.view(goal_id)["snapshots"] if row["source_id"] == task["source_id"])

        failure = None
        with ThreadPoolExecutor(max_workers=min(len(pending), version["body"]["concurrency"])) as executor:
            futures = {}
            for index, item in enumerate(pending):
                try:
                    self._dispatch(store, item[1], binding)
                    futures[executor.submit(acquire, item)] = item
                except BaseException as exc:
                    failure = exc
                    # Already-submitted work still drains through the settlement loop.
                    for remaining in pending[index:]:
                        store.settle(remaining[1]["id"], {})
                    break
            for future in as_completed(futures):
                task, op, _ = futures[future]
                settled = False
                try:
                    result = future.result()
                    costs = {"expansion_tasks": 1, "tool_calls": 1}
                    if task["tool"] == "fetch":
                        costs.update({"sources": 1, "source_bytes": result.get("bytes", len(result["text"].encode("utf-8")))})
                    valid = store.settle(op["id"], costs)
                    settled = True
                    if task["tool"] == "fetch":
                        source_id = "source-" + _id({"url": result["url"], "text": result["text"]})
                        source = store.add_snapshot(goal_id, source_id, result["text"], result["provenance"], result["source_kind"], url=result["url"], label=[], binding=binding, operation_id=op["id"], raw_bytes=result.get("raw_bytes"), content_type=result.get("content_type", ""))
                        if valid and store.check_binding(binding):
                            fresh_sources.append({**source, "source_id": source_id, "text": result["text"], "url": result["url"], "label": []})
                            for url in result.get("links", [])[:200]:
                                normalized = canonical_url(url)
                                if normalized:
                                    known_urls.add(normalized)
                    elif task["tool"] == "search" and valid:
                        for hit in result:
                            normalized = canonical_url(hit["url"])
                            if normalized:
                                known_urls.add(normalized)
                    elif valid:
                        fresh_sources.append(result)
                    if valid and store.check_binding(binding):
                        store.finish_task(goal_id, task["id"], "done", {"links": sorted(known_urls)}, binding=binding)
                except BaseException as exc:
                    if not settled:
                        store.settle(op["id"], None, lost=True)
                    if store.check_binding(binding):
                        store.finish_task(goal_id, task["id"], "failed", {"error": type(exc).__name__}, binding=binding)
                    store.event(goal_id, "acquisition_failure", {"task_id": task["id"], "error": type(exc).__name__})
                    if isinstance(exc, (KeyboardInterrupt, OperatorStopped)):
                        failure = exc
        if failure is not None:
            raise failure
        return fresh_sources, len(futures)

    def _claims(self, store, goal_id, sources, binding, known_urls):
        excerpt_bytes = max(1, 24000 // max(1, len(sources)))
        excerpts = [{**{k: v for k, v in source.items() if k != "raw_bytes"}, "text": source["text"].encode("utf-8")[:excerpt_bytes].decode("utf-8", "ignore"), "offset": 0} for source in sources]
        recipient, payload = self._payload(store, goal_id, "research", {"sources": excerpts})
        receipt = self._call(store, goal_id, "research", payload, binding=binding)
        proposed = receipt["output"].get("claims")
        if not isinstance(proposed, list):
            raise RuntimeError("claims", "research response did not contain a claim list")
        valid_source_ids = {s["source_id"] for s in sources}
        created = []
        for raw in proposed[:40]:
            if not isinstance(raw, dict) or raw.get("source_id") not in valid_source_ids:
                raise RuntimeError("source_id", "claim cites a source outside this research task")
            source = next(s for s in sources if s["source_id"] == raw["source_id"])
            quote = raw.get("quote")
            if not isinstance(quote, str) or not quote or source["text"].count(quote) != 1:
                raise RuntimeError("quote", "a source claim requires a unique exact quotation")
            start = source["text"].index(quote)
            claim = {**raw, "start": start, "end": start + len(quote), "id": "claim-" + _id({"source": raw["source_id"], "quote": quote, "text": raw.get("text")}), "model_id": receipt["model_id"], "producer": receipt["model_id"], "producer_recipient": recipient, "label": []}
            created.append(store.add_claim(goal_id, claim, binding=binding))
        suggestions = receipt["output"].get("tasks", [])
        if not isinstance(suggestions, list):
            raise RuntimeError("tasks", "research follow-up hypotheses must be a list")
        version = store.version(goal_id)
        snapshots = store.snapshots(goal_id)
        public_context = self._public_context(version, snapshots)
        for suggestion in suggestions[:20]:
            task = self._task(suggestion, version, public_context, known_urls, snapshots)
            store.record_task(goal_id, task, binding=binding)
        turns = store.record_turn(goal_id, binding=binding)
        if context.refresh_due(turns):
            store.event(goal_id, "context_refresh_due", {"turn": turns})
        if not created:
            return
        reviewer, payload = self._payload(store, goal_id, "review", {"claims": created, "sources": excerpts})
        review = self._call(store, goal_id, "review", payload, binding=binding)
        if review["model_id"] == receipt["model_id"]:
            raise RuntimeError("reviewer", "evidence review requires an independent model")
        rows = review["output"].get("reviews")
        if not isinstance(rows, list):
            raise RuntimeError("reviews", "review output did not contain explicit claim verdicts")
        known = {c["id"] for c in created}
        seen = set()
        for row in rows:
            cid = row.get("claim_id")
            if cid not in known or cid in seen:
                raise RuntimeError("claim_id", "review names an unknown or duplicate claim")
            seen.add(cid)
            store.review_claim(goal_id, cid, row["verdict"], reviewer=review["model_id"], reason=row.get("reason", ""), binding=binding)
        # Omitted verdicts stay unknown, never positional fallback.

    def _refresh(self, store, goal_id, binding):
        claims = store.claims(goal_id)
        criteria = store.version(goal_id)["public"]["criteria"]
        selected = context.select_context(claims, 40, 12000, [c["id"] for c in criteria], clearance=[])
        store.save_context(goal_id, selected["selected_ids"], binding=binding)

    def _assess(self, store, goal_id, binding):
        _, payload = self._payload(store, goal_id, "synthesis")
        payload["claims"] = [c for c in payload["claims"] if c["state"] == "kept"]
        if not payload["claims"]:
            store.event(goal_id, "missing_reviewed_support", {})
            return False
        synthesis = self._call(store, goal_id, "synthesis", payload, binding=binding)
        result = synthesis["output"]
        ids = result.get("evidence_ids")
        allowed = {c["id"] for c in payload["claims"]}
        if not isinstance(result.get("text"), str) or not isinstance(ids, list) or not ids or not set(ids) <= allowed:
            raise RuntimeError("evidence_ids", "deliverable requires kept authorized citations")
        deliverable = {"text": result["text"], "evidence_ids": ids, "label": sorted(_labels(payload)), "model_id": synthesis["model_id"]}
        store.save_result(goal_id, deliverable, binding=binding)
        recipient, payload = self._payload(store, goal_id, "judge", {"deliverable": deliverable})
        judged = self._call(store, goal_id, "judge", payload, binding=binding)
        outcome = store.add_judgment(goal_id, {**judged["output"], "label": sorted(_labels(payload)), "model_id": judged["model_id"]}, recipient, binding=binding)
        if outcome["outcome"] == "rubric_met":
            store.set_outcome(goal_id, "rubric_met", "The independent judge accepted the cited research deliverable.", binding=binding)
            return True
        required = {row["id"] for row in store.version(goal_id)["body"]["rubric"]["criteria"] if row["required"]}
        if required - set(outcome["authorized_ids"]):
            store.set_outcome(goal_id, "blocked", "A required criterion needs an explicitly authorized judge recipient.", binding=binding)
            return True
        return False

    def run(self, goal_id, max_waves=5):
        if isinstance(max_waves, bool) or not isinstance(max_waves, int) or not 1 <= max_waves <= 1000:
            raise RuntimeError("max_waves", "choose a finite wave count from 1 to 1000")
        store = self._open()
        binding = None
        try:
            if store.goal(goal_id)["state"] != "running":
                return store.view(goal_id)
            binding = store.binding(goal_id)
            version = store.version(goal_id)
            from tools.generic_acquisition import canonical_url
            known_urls = {url for seed in version["public"]["seeds"] if isinstance(seed.get("value"), str) and seed["value"].startswith(("https://", "http://")) for url in [canonical_url(seed["value"])] if url}
            for task in store.tasks(goal_id):
                for url in (task.get("result") or {}).get("links", []):
                    normalized = canonical_url(url)
                    if normalized:
                        known_urls.add(normalized)
            stalls = 0
            for _ in range(max_waves):
                if not store.check_binding(binding) or (ROOT / "STOP").exists():
                    return store.view(goal_id)
                snapshots = store.snapshots(goal_id)
                public_context = self._public_context(version, snapshots)
                self._refresh(store, goal_id, binding)
                task_summary = [{k: row[k] for k in ("id", "tool", "url", "query_text", "criterion_id", "status") if k in row} for row in store.tasks(goal_id)[-100:]]
                judgments = [row for row in store.view(goal_id).get("judgments", []) if row.get("label") == []]
                _, payload = self._payload(store, goal_id, "planner", {"known_urls": sorted(known_urls)[:200], "tasks": task_summary, "public_context": public_context, "headroom": store.headroom(goal_id), "concurrency": version["body"]["concurrency"], "prior_assessment": judgments[-1] if judgments else None})
                plan = self._call(store, goal_id, "planner", payload, binding=binding)
                proposed = plan["output"].get("tasks")
                if not isinstance(proposed, list):
                    raise RuntimeError("tasks", "planner must propose a task list")
                queued = [row for row in store.tasks(goal_id) if row.get("status") == "queued"]
                candidates = queued + [self._task(t, version, public_context, known_urls, snapshots) for t in proposed[:32]]
                tasks = list({task["id"]: task for task in candidates}.values())[:version["body"]["concurrency"]]
                if not tasks:
                    store.set_outcome(goal_id, "empty_frontier", "Planner proposed no remaining research tasks.", binding=binding)
                    break
                sources, count = self._tools(store, goal_id, tasks, binding, known_urls)
                if not store.check_binding(binding):
                    break
                if sources:
                    self._claims(store, goal_id, sources, binding, known_urls)
                self._refresh(store, goal_id, binding)
                if self._assess(store, goal_id, binding):
                    break
                stalls = stalls + 1 if count == 0 else 0
                if stalls >= version["body"]["stopping_policy"].get("stall_checkpoints", 2):
                    store.set_outcome(goal_id, "stalled", "No new admitted tasks or supported evidence at repeated checkpoints.", binding=binding)
                    break
            else:
                if store.check_binding(binding):
                    store.set_outcome(goal_id, "partial", "The requested bounded run ended before all criteria were supported.", binding=binding)
            return store.view(goal_id)
        except (KeyboardInterrupt, OperatorStopped):
            # Process control must not rewrite the user's durable goal.
            return store.view(goal_id)
        except Exception as exc:
            if binding is not None and store.check_binding(binding):
                field = getattr(exc, "field", "runtime")
                limit_fields = set(contract.LIMIT_UNITS) | {"concurrency", "elapsed", "budget", "context_limit"}
                outcome = "exhausted" if field in limit_fields else "blocked" if field in {"recipient", "clearance", "binding", "url", "source_id", "public_context_ids"} else "outage"
                store.event(goal_id, "runtime_failure", {"error": type(exc).__name__, "field": field})
                store.set_outcome(goal_id, outcome, f"Research stopped at {field}; no unsupported success was recorded.", binding=binding)
                return store.view(goal_id)
            if binding is not None:
                return store.view(goal_id)
            raise
        finally:
            store.close()

    def diagnostics(self):
        return {"database": str(self.db_path), "exists": self.db_path.exists(), "roles": self.roles.diagnostics()}

    def ask(self, goal_id, query):
        if not isinstance(query, str) or not query.strip():
            raise RuntimeError("query", "a query is required")
        view = self.view(goal_id)
        words = set(query.casefold().split())
        hits = [row for row in view["claims"] if row["state"] == "kept" and words & set((row.get("text", "") + " " + row.get("quote", "")).casefold().split())]
        return {"query": query, "claims": hits, "result": view.get("result"), "mode": "local_evidence", "model_called": False}

    def export(self, goal_id, path):
        view = self.view(goal_id)
        target = Path(path)
        if target.exists():
            raise RuntimeError("path", "export refuses to overwrite an existing file")
        result = view.get("result") or {}
        sources = {s["source_id"]: s for s in view["snapshots"]}
        lines = ["# " + view["goal"]["title"], "", "Outcome: " + str(view["goal"].get("outcome")), "", result.get("text", "No supported deliverable yet."), "", "## Evidence", ""]
        for claim in view["claims"]:
            source = sources.get(claim["source_id"], {})
            lines.extend([f"- {claim['id']} ({claim['state']}): {claim.get('text', '')}", f"  Source: {source.get('url', '')}", "  Quote: " + claim.get("quote", "")])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return {"path": str(target), "outcome": view["goal"].get("outcome")}
