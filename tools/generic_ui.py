"""Generic five-view console: Goal, Results, Activity, Evidence, Graph (last).

``render`` returns the authenticated app shell only — no fixture rows, no demo
content. All data is fetched by ``/assets/app.js`` from the session-authenticated
API and rendered with ``textContent`` (source text is never injected as HTML).
Assets are downloaded once and served by the API from this module's constants.
"""
from __future__ import annotations

import html
import json
from typing import Any

VIEWS = ("Goal", "Results", "Activity", "Evidence", "Graph")


def _esc(value: Any) -> str:
    return html.escape(str(value))


def render(state: dict[str, Any] | None = None, *, nonce: str | None = None) -> str:
    """Server shell. ``state`` (an actual store view) may pre-seed the first render."""
    attrs = ""
    if state is not None:
        seed = html.escape(json.dumps(state, ensure_ascii=False).replace("</", "<\\/"), quote=True)
        attrs = f' data-seed="{seed}"'
    nonce_attr = f' nonce="{_esc(nonce)}"' if nonce else ""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Snowball console</title>\n"
        f'<link rel="stylesheet" href="/assets/app.css">\n'
        "</head>\n<body>\n"
        '<a class="skip" href="#main">Skip to content</a>\n'
        '<header class="topbar">\n'
        '  <span class="wordmark">Snowball</span>\n'
        '  <nav class="views" aria-label="Console views">\n'
        + "".join(
            f'    <button class="view-tab" role="tab" data-view="{name.lower()}" '
            f'aria-selected="false" aria-controls="view-{name.lower()}">{name}</button>\n'
            for name in VIEWS
        )
        + "  </nav>\n"
        '  <button id="logout" class="ghost" hidden>Log out</button>\n'
        "</header>\n"
        '<main id="main" class="console" tabindex="-1"' + attrs + ">\n"
        + "".join(
            f'  <section id="view-{name.lower()}" class="view" data-view="{name.lower()}" role="tabpanel" hidden></section>\n'
            for name in VIEWS
        )
        + "</main>\n"
        '<div id="toast" role="status" aria-live="polite"></div>\n'
        f'<script src="/assets/app.js"{nonce_attr}></script>\n'
        "</body>\n</html>\n"
    )


def _json_escape(text: str) -> str:
    return json.dumps(text).replace("</", "<\\/")


APP_JS = r"""'use strict';
/* Snowball generic console. Fetches authenticated state; renders with textContent only. */
(() => {
  const VIEWS = ["goal", "results", "activity", "evidence", "graph"];
  const state = { goals: [], current: null, view: "goal", csrf: null, busy: false, runActive: false, grantIds: new Map() };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined && text !== null) n.textContent = String(text); return n; };

  function toast(message, isError) {
    const t = $("#toast");
    t.textContent = message || "";
    t.classList.toggle("error", Boolean(isError));
    if (message) { clearTimeout(t._timer); t._timer = setTimeout(() => { t.textContent = ""; }, 4000); }
  }

  class ApiError extends Error {
    constructor(payload, status) { super((payload && payload.error && payload.error.message) || (payload && payload.error && payload.error.code) || ("HTTP " + status)); this.payload = payload; this.status = status; this.field = payload && payload.error && payload.error.field; }
  }

  async function api(method, path, body, opts) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (opts && opts.csrf) headers["X-CSRF-Token"] = opts.csrf;
    if (opts && opts.idem) headers["Idempotency-Key"] = opts.idem;
    const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
    let payload = null;
    try { payload = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) throw new ApiError(payload, res.status);
    return payload;
  }

  const idem = () => (crypto.randomUUID ? crypto.randomUUID() : "k" + Date.now() + "-" + Math.random().toString(36).slice(2));

  // --- bootstrap ---------------------------------------------------------

  async function boot() {
    const seed = $("#main").dataset.seed ? JSON.parse($("#main").dataset.seed) : null;
    let session = null;
    try { session = await api("GET", "/api/session"); }
    catch (e) { renderLogin(); return; }
    state.csrf = session.csrf;
    await loadGoals();
    if (seed && seed.goal) { state.current = await refreshGoal(seed.goal.id).catch(() => null) || seed; }
    renderAll();
  }

  async function loadGoals() {
    const data = await api("GET", "/api/goals");
    state.goals = data.goals || [];
    if (!state.current && state.goals.length) await refreshGoal(state.goals[0].id).catch(() => {});
  }

  async function refreshGoal(goalId) {
    const view = await api("GET", "/api/goals/" + encodeURIComponent(goalId));
    state.current = view;
    return view;
  }

  async function act(action, body, opts) {
    if (!state.current) return null;
    const goalId = goalIdOf(state.current);
    try {
      state.busy = true;
      const result = await api("POST", "/api/goals/" + encodeURIComponent(goalId) + "/" + action, body || {}, Object.assign({ csrf: state.csrf, idem: idem() }, opts || {}));
      applyResult(result);
      toast(describe(action, true));
      return result;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) { renderLogin(); return; }
      toast(friendly(err), true);
      if (err instanceof ApiError) state.lastError = err.payload;
    } finally { state.busy = false; renderAll(); }
  }

  function goalIdOf(view) { return (view.goal && view.goal.id) || view.goal_id || (view.draft && view.draft.goal_id); }

  function applyResult(result) {
    if (result && result.goal && result.draft !== undefined) { state.current = Object.assign({}, state.current, result); }
    else if (result && result.goal) { state.current = Object.assign({}, state.current, result); }
    else if (result) { state.current = Object.assign({}, state.current, result); }
  }

  function describe(action, ok) {
    const verbs = { answer: "Answer saved", consent: "Disclosure consent recorded", interview: "Interview followed up", propose: "Proposal drafted", approve: "Contract approved — review the grant next", grant: "Grant issued — start when ready", start: "Research started", pause: "Pause requested — draining", resume: "Resumed", stop: "Stopped", revise: "Revision opened — epoch advanced", run: "Research run dispatched" };
    return verbs[action] || (ok ? "Done" : "Failed");
  }

  function friendly(err) {
    if (!(err instanceof ApiError)) return "Network error — is the console server running?";
    const map = { session_required: "Sign in again with a bootstrap token.", invalid_token: "That bootstrap token is not valid or was already used.", csrf: "Security token mismatch — reload the page.", origin: "Request came from another origin.", stale_row_version: "Someone changed this goal first — reload and redo your edit.", budget_exceeded: "A grant limit was reached.", goal_not_found: "That goal no longer exists.", run_in_progress: "A run is already active for this goal.", not_running: "Start the goal first, then run.", invalid_state: "That action is not allowed in the current state.", grant_state: "The grant is not in a state for that action.", recipient: "Approve a model recipient for that stage first.", privacy: "That would send material to a recipient without clearance." };
    const code = err.payload && err.payload.error && err.payload.error.code;
    return map[code] || err.message;
  }

  // --- rendering ---------------------------------------------------------

  function renderAll() {
    const authed = Boolean(state.csrf);
    $("#logout").hidden = !authed;
    for (const name of VIEWS) {
      const section = document.querySelector('#view-' + name);
      section.hidden = name !== state.view;
      if (name === state.view) renderView(name, section);
    }
    document.querySelectorAll(".view-tab").forEach(tab => {
      const active = tab.dataset.view === state.view;
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    });
  }

  function switchView(name) { state.view = name; renderAll(); $("#view-" + name).focus ? null : null; document.querySelector('#view-' + name).setAttribute('tabindex', '-1'); document.querySelector('#view-' + name).focus(); }

  function renderView(name, section) {
    section.replaceChildren();
    if (!state.current) {
      renderGoalPicker(section);
      return;
    }
    if (name === "goal") renderGoal(section);
    else if (name === "results") renderResults(section);
    else if (name === "activity") renderActivity(section);
    else if (name === "evidence") renderEvidence(section);
    else if (name === "graph") renderGraph(section);
  }

  function renderGoalPicker(section) {
    const head = el("h2", null, "Goals");
    section.append(head);
    if (!state.goals.length) {
      section.append(el("p", "empty", "No goals yet. Name a research goal to open it."));
    }
    const list = el("ul", "goal-list");
    for (const g of state.goals) {
      const li = el("li");
      const btn = el("button", "goal-link", g.title || g.id);
      btn.addEventListener("click", async () => { try { await refreshGoal(g.id); renderAll(); } catch (e) { toast(friendly(e), true); } });
      li.append(btn, el("span", "badge", g.state));
      list.append(li);
    }
    section.append(list);
    const form = el("form", "new-goal");
    const label = el("label", null, "New goal title");
    const input = el("input"); input.name = "title"; input.required = true; input.placeholder = "What should the research establish?";
    const submit = el("button", "primary", "Open goal");
    form.append(label, input, submit);
    form.addEventListener("submit", async ev => {
      ev.preventDefault();
      try {
        const result = await api("POST", "/api/goals", { title: input.value }, { csrf: state.csrf, idem: idem() });
        await loadGoals();
        await refreshGoal(result.goal.id);
        renderAll();
      } catch (e) { toast(friendly(e), true); }
    });
    section.append(form);
  }

  function renderGoal(section) {
    const view = state.current;
    const goal = view.goal || {};
    section.append(el("h2", null, goal.title || goal.id));
    const meta = el("p", "meta");
    meta.append(el("span", "badge state-" + goal.state, goal.state));
    if (goal.epoch) meta.append(el("span", "meta-item", "epoch " + goal.epoch));
    meta.append(el("span", "meta-item", "row " + goal.row_version));
    if (goal.outcome) meta.append(el("span", "badge outcome", goal.outcome));
    section.append(meta);

    //_version body summary
    const version = view.version;
    if (version && version.body) {
      const b = version.body;
      const card = el("section", "card");
      card.append(el("h3", null, "Approved contract"));
      const obj = el("dl", "kv");
      obj.append(el("dt", null, "Objective"), el("dd", null, b.objective));
      obj.append(el("dt", null, "Deliverable"), el("dd", null, b.deliverable));
      obj.append(el("dt", null, "Contract hash"), el("dd mono", b.body_hash ? b.body_hash.slice(0, 16) + "…" : (view.body_hash || "").slice(0, 16) + "…"));
      card.append(obj);
      const rub = el("h4", null, "Rubric");
      card.append(rub);
      const rubList = el("ul", "rubric");
      for (const c of (b.rubric && b.rubric.criteria) || []) {
        const li = el("li");
        li.append(el("span", "crit-id", c.id), el("span", "crit-desc", c.description), c.required ? el("span", "badge req", "required") : el("span", "badge opt", "optional"));
        rubList.append(li);
      }
      card.append(rubList);
      section.append(card);
    }

    //draft: questions, answers, consent, proposal, approval editor
    const draft = view.draft;
    if (draft) { renderDraft(section, draft, view); }

    //controls tied to durable state
    const controls = el("section", "card controls");
    controls.append(el("h3", null, "Controls"));
    const rowV = goal.row_version;
    const addBtn = (parent, labelText, cls, handler, disabled) => {
      const b = el("button", cls, labelText);
      b.disabled = Boolean(disabled) || state.busy;
      b.addEventListener("click", handler);
      parent.append(b);
      return b;
    };
    const grid = el("div", "btn-grid");
    if (goal.state === "draft" || goal.state === "revising") {
      addBtn(grid, "Run interview follow-ups", "secondary", () => act("interview", {}));
      addBtn(grid, "Propose contract", "secondary", () => act("propose", {}));
    }
    if (goal.state === "approved") addBtn(grid, "Issue grant", "primary", async () => {
      // The grant response carries the actual grant_id; starting uses exactly that.
      const result = await act("grant", {});
      const gid = result && result.grant_id;
      if (gid != null) state.grantIds.set(goalIdOf(state.current), gid);
    });
    if (goal.state === "granted") {
      // After refresh/reload the view exposes the active issued id; use it.
      const gid = state.grantIds.get(goalIdOf(state.current)) ?? view.grant_id;
      if (gid != null) addBtn(grid, "Start research", "primary", () => act("start", { grant_id: gid }));
      else {
        const note = el("p", "empty", "A grant is issued but its id was not returned by the view. Reload to pick it up from the goal record.");
        grid.append(note);
      }
    }
    if (goal.state === "running") {
      addBtn(grid, "Pause", "secondary", () => act("pause", {}));
      addBtn(grid, "Stop", "danger", () => act("stop", {}));
      const runBtn = addBtn(grid, state.runActive ? "Run wave… active" : "Run research wave", "primary", async () => {
        try {
          const res = await api("POST", "/api/goals/" + encodeURIComponent(goalIdOf(state.current)) + "/run", {}, { csrf: state.csrf, idem: idem() });
          if (res && res.run === "started") { state.runActive = true; toast("Research run dispatched"); pollRun(); }
        } catch (e) { toast(friendly(e), true); }
      }, state.runActive);
    }
    if (goal.state === "paused") addBtn(grid, "Resume", "primary", () => act("resume", {}));
    if (["running", "paused", "granted", "approved"].includes(goal.state)) addBtn(grid, "Revise contract", "secondary", () => act("revise", {}));
    controls.append(grid);
    section.append(controls);

    //recipient permissions
    if (version && version.body && version.body.recipients) {
      const r = el("section", "card");
      r.append(el("h3", null, "Recipient permissions"));
      const dl = el("dl", "kv");
      for (const [rid, rec] of Object.entries(version.body.recipients)) {
        dl.append(el("dt", null, rid), el("dd", null, (rec.kind || "?") + (rec.attested ? " · attested" : "") + (rec.clearance && rec.clearance.length ? " · " + rec.clearance.join(", ") : " · public only")));
      }
      r.append(dl);
      section.append(r);
    }
    if (draft && draft.proposal) renderApprovalEditor(section, draft, view);
  }

  function renderDraft(section, draft, view) {
    const card = el("section", "card");
    card.append(el("h3", null, "Interview"));
    const qs = draft.questions || [];
    const answers = draft.answers || [];
    const answerBy = {};
    for (const row of answers) answerBy[row.question_id] = row;
    if (!qs.length) card.append(el("p", "empty", "The interview opens when you answer the first question."));
    for (const q of qs) {
      const row = answerBy[q.id];
      const qwrap = el("div", "question type-" + (q.input_type || "text"));
      const lab = el("label", null, q.prompt);
      if (q.required) lab.append(el("span", "badge req", "required"));
      if (q.label && q.label.length) lab.append(el("span", "badge priv", q.label.join(", ")));
      qwrap.append(lab);
      const cur = answerBy[q.id];
      const form = el("form", "answer-form");
      const makeControl = () => {
        const t = q.input_type;
        if (t === "boolean") { const wrap = el("div"); const yes = el("button", "chip", "Yes"); const no = el("button", "chip", "No"); yes.type = no.type = "button"; yes.addEventListener("click", () => setAnswer(q, { value: true, state: "confirmed", origin: "user_stated" })); no.addEventListener("click", () => setAnswer(q, { value: false, state: "confirmed", origin: "user_stated" })); wrap.append(yes, no); return wrap; }
        if (t === "choice") { const sel = el("select"); for (const c of q.choices || []) { const o = el("option", null, c); o.value = c; sel.append(o); } return sel; }
        if (t === "integer") { const i = el("input"); i.type = "number"; i.step = "1"; return i; }
        if (t === "decimal") { const i = el("input"); i.type = "text"; i.inputMode = "decimal"; i.placeholder = "e.g. 12.5"; return i; }
        if (t === "list" || t === "object") { const t2 = el("textarea"); t2.rows = t === "list" ? 3 : 5; t2.placeholder = "JSON value"; return t2; }
        const i = el("textarea"); i.rows = 2; return i;
      };
      const control = makeControl();
      form.append(control);
      const actions = el("div", "q-actions");
      const submit = el("button", "chip primary", cur ? "Update answer" : "Answer");
      submit.type = "submit";
      actions.append(submit);
      if (!q.required) {
        const decline = el("button", "chip", "Decline");
        decline.type = "button"; decline.addEventListener("click", () => setAnswer(q, { state: "declined", origin: "user_stated" }));
        const unknown = el("button", "chip", "Mark unknown");
        unknown.type = "button"; unknown.addEventListener("click", () => setAnswer(q, { state: "unknown", origin: "user_stated" }));
        actions.append(decline, unknown);
      }
      form.append(actions);
      if (cur) {
        const shown = cur.state === "confirmed" ? (typeof cur.value === "object" ? JSON.stringify(cur.value) : String(cur.value)) : cur.state;
        const curEl = el("p", "current", "Current: " + shown);
        qwrap.append(curEl);
      }
      form.addEventListener("submit", async ev => {
        ev.preventDefault();
        const t = q.input_type;
        let value;
        if (t === "boolean") value = control._choice;
        else if (t === "choice") value = control.value;
        else if (t === "integer") value = control.value === "" ? null : Number(control.value);
        else if (t === "decimal") value = control.value;
        else if (t === "list" || t === "object") { try { value = JSON.parse(control.value); } catch (e) { toast("Answers of this type must be valid JSON", true); return; } }
        else value = control.value;
        await setAnswer(q, { value, state: "confirmed", origin: "user_stated" });
      });
      qwrap.append(form);
      card.append(qwrap);
    }
    //consent: finite interview allowances per recipient
    const consentCard = el("div", "consent");
    consentCard.append(el("h4", null, "Interview model consent"));
    const refs = el("input"); refs.placeholder = "Question IDs the recipient may see (comma-separated)";
    const calls = el("input"); calls.type = "number"; calls.placeholder = "Max interview calls"; calls.min = "1";
    const tokens = el("input"); tokens.type = "number"; tokens.placeholder = "Max tokens (optional)"; tokens.min = "1";
    const rec = el("input"); rec.placeholder = "Recipient id";
    const grantConsent = el("button", "chip primary", "Record consent");
    grantConsent.type = "button";
    grantConsent.addEventListener("click", async () => {
      const recipients = (state.current.version && state.current.version.body && state.current.version.body.recipients) || {};
      const recipient = rec.value.trim() || Object.keys(recipients)[0] || "interview";
      const refsList = refs.value.split(",").map(s => s.trim()).filter(Boolean);
      const allowance = { model_calls: Number(calls.value || 1) };
      if (tokens.value) allowance.model_tokens = Number(tokens.value);
      try {
        await act("consent", { recipient, refs: refsList, allowance, recipients: recipients[recipient] ? recipients : { [recipient]: draft.recipients && draft.recipients[recipient] ? draft.recipients[recipient] : { kind: "model", attested: false, clearance: [] } } });
      } catch (e) { toast(friendly(e), true); }
    });
    const cform = el("div", "consent-form");
    cform.append(rec, refs, calls, tokens, grantConsent);
    consentCard.append(cform);
    if (draft.consent) {
      const line = el("p", "current", "Consent: " + draft.consent.recipient + " · used " + (draft.consent.used || 0) + "/" + (draft.consent.allowance && draft.consent.allowance.model_calls));
      consentCard.append(line);
    }
    card.append(consentCard);
    section.append(card);
  }

  async function setAnswer(q, answer) {
    try {
      await act("answer", { question_id: q.id, answer });
    } catch (e) { toast(friendly(e), true); }
  }

  let activePreview = null; // { payload, body_hash, body } — invalidated on any edit

  function renderApprovalEditor(section, draft, view) {
    const proposal = draft.proposal || {};
    const payload = proposal.payload || proposal;
    const card = el("section", "card approval");
    card.append(el("h3", null, "Proposed contract — review before approval"));
    const summary = el("p", "summary");
    summary.textContent = "Objective: " + (payload.objective || "") + (payload.revision ? " · revision " + payload.revision : "");
    card.append(summary);
    const editor = el("textarea", "json-editor");
    editor.rows = 14;
    editor.spellcheck = false;
    editor.value = JSON.stringify(payload, null, 2);
    editor.setAttribute("aria-label", "Contract JSON editor");
    card.append(editor);
    const reviewBtn = el("button", "secondary", "Review edited JSON");
    reviewBtn.type = "button";
    const approveBtn = el("button", "primary", "Approve reviewed version");
    approveBtn.type = "button";
    approveBtn.disabled = true;
    const status = el("p", "edit-status", "Review computes the exact canonical hash on the server; approve uses that reviewed version.");
    const showPreview = (preview) => {
      // Frozen server response: {goal, payload, version:{body, body_hash, ...}}.
      activePreview = { payload: preview.payload, body: preview.version.body, body_hash: preview.version.body_hash };
      status.textContent = "Reviewed body hash: " + activePreview.body_hash.slice(0, 32) + "…";
      // The canonical built body is what approval will hash; show it read-only.
      editor.value = JSON.stringify(preview.version.body, null, 2);
      approveBtn.disabled = false;
    };
    reviewBtn.addEventListener("click", async () => {
      let parsed;
      try { parsed = JSON.parse(editor.value); } catch (e) { toast("The JSON editor still has a syntax error", true); return; }
      try {
        const preview = await api("POST", "/api/goals/" + encodeURIComponent(goalIdOf(state.current)) + "/preview",
          { payload: parsed, row_version: (state.current.goal && state.current.goal.row_version) || 0 },
          { csrf: state.csrf, idem: idem() });
        showPreview(preview);
      } catch (err) { toast(friendly(err), true); }
    });
    editor.addEventListener("input", () => {
      // Any edit invalidates the reviewed version; approval locks until re-review.
      activePreview = null;
      approveBtn.disabled = true;
      status.textContent = "Edited — review again to recompute the canonical version.";
    });
    approveBtn.addEventListener("click", async () => {
      if (!activePreview) return;
      try {
        await act("approve", { payload: activePreview.payload, body_hash: activePreview.body_hash, row_version: (state.current.goal && state.current.goal.row_version) || 0 });
        activePreview = null;
        await refreshGoal(goalIdOf(state.current)).catch(() => {});
        renderAll();
      } catch (e) { /* act toasts already shown */ }
    });
    const row = el("div", "btn-row");
    row.append(reviewBtn, approveBtn);
    card.append(row, status);
    section.append(card);
  }

  async function pollRun() {
    const goal = state.current && state.current.goal;
    if (!goal || !state.runActive) return;
    const checker = setInterval(async () => {
      try {
        const v = await api("GET", "/api/goals/" + encodeURIComponent(goalIdOf(state.current)));
        state.current = v;
        const stillRunning = v.goal && v.goal.state === "running" && (v.goal.outcome || null) === null;
        renderAll();
        if (!stillRunning) { state.runActive = false; clearInterval(checker); toast("Run wave finished — check Results"); }
      } catch (e) { clearInterval(checker); state.runActive = false; }
    }, 2500);
    setTimeout(() => { clearInterval(checker); }, 15 * 60 * 1000);
  }

  function renderResults(section) {
    const view = state.current;
    if (!view) { renderGoalPicker(section); return; }
    section.append(el("h2", null, "Results"));
    const result = view.result;
    if (!result || !result.text) {
      const g = view.goal || {};
      if (g.outcome && g.outcome !== "progressing") {
        section.append(el("p", "empty", "Outcome: " + g.outcome + (g.reason ? " — " + g.reason : "") + ". No supported deliverable was recorded."));
      } else {
        section.append(el("p", "empty", "No supported deliverable yet. Results appear after a run produces cited evidence and the judge accepts it."));
      }
      renderJudgments(section, view);
      return;
    }
    const card = el("section", "card");
    card.append(el("h3", null, "Deliverable"));
    const body = el("div", "result-text");
    // textContent, never innerHTML
    body.textContent = result.text;
    card.append(body);
    const ev = el("p", "meta");
    ev.append(el("span", "badge", (result.evidence_ids || []).length + " cited claims"));
    if (result.model_id) ev.append(el("span", "meta-item", "model " + result.model_id));
    card.append(ev);
    const citeList = el("ul", "citations");
    for (const id of result.evidence_ids || []) {
      const li = el("li");
      const btn = el("button", "linklike", id);
      btn.addEventListener("click", () => { state.view = "evidence"; renderAll(); });
      li.append(btn);
      citeList.append(li);
    }
    card.append(citeList);
    section.append(card);
    renderJudgments(section, view);
  }

  function renderJudgments(section, view) {
    const judgments = view.judgments || [];
    if (!judgments.length) return;
    const card = el("section", "card");
    card.append(el("h3", null, "Independent judgments"));
    for (const j of judgments) {
      const row = el("div", "judgment outcome-" + j.outcome);
      row.append(el("span", "badge", j.outcome));
      const body = j.body || j;
      if (body.rows) {
        const dl = el("dl", "kv");
        for (const r of body.rows) {
          dl.append(el("dt", null, r.criterion_id), el("dd", null, (r.verdict || "?") + (r.reason ? " — " + r.reason : "")));
        }
        row.append(dl);
      }
    card.append(row);
    }
    section.append(card);
  }

  function renderActivity(section) {
    const view = state.current;
    if (!view) { renderGoalPicker(section); return; }
    section.append(el("h2", null, "Activity"));
    const usage = view.usage || {};
    const card = el("section", "card");
    card.append(el("h3", null, "Budgets"));
    const dl = el("dl", "kv budgets");
    for (const unit of ["expansion_tasks", "sources", "source_bytes", "model_calls", "tool_calls", "model_tokens", "elapsed_sec"]) {
      const lim = usage.limits && usage.limits[unit];
      if (lim === undefined) continue;
      dl.append(
        el("dt", null, unit.replace(/_/g, " ")),
        el("dd", null, (usage.used && usage.used[unit] || 0) + " used · " + (usage.reserved && usage.reserved[unit] || 0) + " reserved · " + (usage.lost && usage.lost[unit] || 0) + " lost · of " + lim)
      );
    }
    if (!dl.childElementCount) card.append(el("p", "empty", "No execution grant yet — budgets appear after you issue one."));
    card.append(dl);
    section.append(card);
    if (state.runActive) section.append(el("p", "meta", "A research run is active; the view refreshes automatically."));
    const ev = view.events || [];
    const tcard = el("section", "card");
    tcard.append(el("h3", null, "Events"));
    if (!ev.length) tcard.append(el("p", "empty", "No events recorded yet."));
    const timeline = el("ul", "timeline");
    for (const e of ev.slice().reverse()) {
      const li = el("li", "event kind-" + e.kind);
      li.append(el("span", "kind", e.kind));
      const when = el("span", "when", e.at ? new Date(e.at).toLocaleString() : "");
      li.append(when);
      const bodyText = e.body && typeof e.body === "object" ? JSON.stringify(e.body) : String(e.body === undefined ? "" : e.body);
      li.append(el("span", "event-body", bodyText));
      timeline.append(li);
    }
    tcard.append(timeline);
    section.append(tcard);
    const tasks = view.tasks || [];
    if (tasks.length) {
      const t2 = el("section", "card");
      t2.append(el("h3", null, "Tasks"));
      const tl = el("ul", "timeline");
      for (const t of tasks) {
        const li = el("li", "task status-" + t.status);
        li.append(el("span", "badge", t.status), el("span", "event-body", (t.task && (t.task.tool || "?") + " " + (t.task.query_text || t.task.url || t.task.source_id || "")) || ""));
        tl.append(li);
      }
      t2.append(tl);
      section.append(t2);
    }
  }

  function renderEvidence(section) {
    const view = state.current;
    if (!view) { renderGoalPicker(section); return; }
    section.append(el("h2", null, "Evidence"));
    const snaps = view.snapshots || [];
    const claims = view.claims || [];
    if (!snaps.length && !claims.length) {
      section.append(el("p", "empty", "No evidence yet. Fetched sources and exact-quote claims appear here after research runs."));
      return;
    }
    const scard = el("section", "card");
    scard.append(el("h3", null, "Sources (" + snaps.length + ")"));
    const slist = el("ul", "sources");
    for (const s of snaps) {
      const li = el("li", "source");
      li.append(el("span", "badge", s.provenance || "source"));
      if (s.url) { const a = el("span", "url", s.url); li.append(a); }
      const excerpt = el("blockquote", "source-text");
      excerpt.textContent = (s.text || "").slice(0, 280);
      li.append(excerpt);
      slist.append(li);
    }
    scard.append(slist);
    section.append(scard);
    const ccard = el("section", "card");
    ccard.append(el("h3", null, "Claims (" + claims.length + ")"));
    const clist = el("ul", "claims");
    for (const c of claims) {
      const li = el("li", "claim state-" + c.state);
      li.append(el("span", "badge", c.state));
      const q = el("blockquote", "claim-quote");
      // exact quoted span only; textContent keeps any markup inert
      q.textContent = c.quote || "";
      li.append(q);
      const srcline = el("span", "meta-item", "source " + (c.source_id || "?"));
      li.append(srcline);
      if (c.start !== undefined && c.end !== undefined) li.append(el("span", "meta-item mono", "chars " + c.start + "–" + c.end));
      clist.append(li);
    }
    ccard.append(clist);
    section.append(ccard);
  }

  function renderGraph(section) {
    const view = state.current;
    if (!view) { renderGoalPicker(section); return; }
    section.append(el("h2", null, "Graph"));
    const graph = view.graph || { nodes: [], edges: [] };
    const nodes = graph.nodes || [];
    const edges = graph.edges || [];
    if (!nodes.length) {
      section.append(el("p", "empty", "The graph fills in as evidence, tasks and judgments accumulate. Nothing to draw yet."));
      return;
    }
    const card = el("section", "card");
    const list = el("ul", "graph-nodes");
    for (const n of nodes) {
      const li = el("li", "graph-node kind-" + (n.kind || "node"));
      li.append(el("span", "badge", n.kind || "node"), el("span", null, n.label || n.id));
      list.append(li);
    }
    card.append(list);
    const eWrap = el("div", "graph-edges");
    eWrap.append(el("h4", null, "Links (" + edges.length + ")"));
    const eList = el("ul");
    for (const e of edges) eList.append(el("li", null, (e.from || "?") + " → " + (e.to || "?")));
    eWrap.append(eList);
    card.append(eWrap);
    section.append(card);
  }

  // --- login shell ---------------------------------------------------------

  function renderLogin() {
    document.querySelectorAll(".view").forEach(s => { s.hidden = s.dataset.view !== "goal"; });
    const section = $("#view-goal");
    section.replaceChildren();
    section.append(el("h2", null, "Sign in"));
    section.append(el("p", "empty", "Paste the one-use bootstrap token printed when the console server started."));
    const form = el("form", "login-form");
    const input = el("input"); input.type = "password"; input.autocomplete = "off"; input.placeholder = "Bootstrap token"; input.required = true;
    const submit = el("button", "primary", "Open console");
    form.append(input, submit);
    form.addEventListener("submit", async ev => {
      ev.preventDefault();
      try {
        const session = await api("POST", "/api/session", { token: input.value });
        state.csrf = session.csrf;
        await loadGoals();
        renderAll();
        toast("Welcome");
      } catch (e) { toast(friendly(e), true); }
    });
    section.append(form);
    state.csrf = null;
    state.current = null;
    state.goals = [];
  }

  // --- events --------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".view-tab").forEach(tab => {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
      tab.addEventListener("keydown", ev => {
        const tabs = [...document.querySelectorAll(".view-tab")];
        const i = tabs.indexOf(tab);
        if (ev.key === "ArrowRight") { tabs[(i + 1) % tabs.length].focus(); tabs[(i + 1) % tabs.length].click(); }
        else if (ev.key === "ArrowLeft") { tabs[(i - 1 + tabs.length) % tabs.length].focus(); tabs[(i - 1 + tabs.length) % tabs.length].click(); }
      });
    });
    $("#logout").addEventListener("click", async () => {
      try { await api("POST", "/api/logout", {}, { csrf: state.csrf }); } catch (e) { /* session already gone */ }
      renderLogin();
      toast("Signed out");
    });
    boot();
  });
})();
"""

APP_CSS = r""":root {
  --ink: #13221f;
  --paper: #f4f6f3;
  --card: #ffffff;
  --line: #cfd8d2;
  --accent: #0e6b5c;      /* deep teal — durable research, not decoration */
  --accent-ink: #ffffff;
  --warn: #9a4a12;
  --danger: #8f2437;
  --ok: #1d6b3c;
  --mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
  --sans: "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--paper); color: var(--ink); font-size: 16px; line-height: 1.5; }
.skip { position: absolute; left: -999px; }
.skip:focus { left: 8px; top: 8px; background: var(--card); padding: 6px 10px; z-index: 10; }
.topbar { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; padding: 10px 16px; background: var(--card); border-bottom: 1px solid var(--line); }
.wordmark { font-weight: 700; letter-spacing: 0.02em; }
.views { display: flex; gap: 2px; flex-wrap: wrap; }
.view-tab { border: 1px solid transparent; background: none; padding: 6px 12px; font: inherit; border-radius: 6px 6px 0 0; cursor: pointer; }
.view-tab[aria-selected="true"] { background: var(--accent); color: var(--accent-ink); }
.view-tab:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ghost { border: 1px solid var(--line); background: none; padding: 6px 12px; font: inherit; border-radius: 6px; cursor: pointer; }
.console { max-width: 880px; margin: 0 auto; padding: 16px; }
.view:focus { outline: none; }
.view[hidden] { display: none; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; margin: 12px 0; }
h2 { margin: 8px 0 4px; font-size: 1.4rem; }
h3 { margin: 0 0 8px; font-size: 1.05rem; }
.meta { display: flex; gap: 10px; flex-wrap: wrap; align-items: baseline; color: #4c5a55; font-size: 0.9rem; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 99px; background: #e2e9e4; font-size: 0.78rem; }
.badge.state-running { background: #d8ecdf; color: var(--ok); }
.badge.outcome, .badge.state-blocked, .badge.state-outage { background: #f4ddc4; color: var(--warn); }
.badge.req { background: #f4ddc4; color: var(--warn); }
.kv { display: grid; grid-template-columns: minmax(120px, max-content) 1fr; gap: 4px 14px; margin: 8px 0; }
.kv dt { font-weight: 600; }
.kv dd { margin: 0; overflow-wrap: anywhere; }
.btn-grid { display: flex; gap: 8px; flex-wrap: wrap; }
button.primary { background: var(--accent); color: var(--accent-ink); border: none; padding: 8px 16px; border-radius: 8px; font: inherit; cursor: pointer; }
button.secondary { background: #e2e9e4; border: 1px solid var(--line); padding: 8px 14px; border-radius: 8px; font: inherit; cursor: pointer; }
button.danger { background: unset; color: var(--danger); border: 1px solid var(--danger); padding: 7px 14px; border-radius: 8px; font: inherit; cursor: pointer; }
button.chip { border: 1px solid var(--line); background: var(--card); padding: 4px 10px; border-radius: 99px; font: inherit; font-size: 0.85rem; cursor: pointer; }
button:disabled { opacity: 0.5; cursor: wait; }
button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
input, select, textarea { font: inherit; padding: 8px; border: 1px solid var(--line); border-radius: 8px; width: 100%; margin: 4px 0; background: #fff; color: inherit; }
textarea.json-editor { font-family: var(--mono); font-size: 0.85rem; white-space: pre; }
.question { border-top: 1px solid var(--line); padding: 10px 0; }
.q-head label, .question > label { font-weight: 600; display: block; margin-bottom: 4px; }
.current { color: #4c5a55; font-size: 0.9rem; margin: 4px 0; }
.empty { color: #4c5a55; background: #eef2ef; border: 1px dashed var(--line); padding: 12px 14px; border-radius: 8px; }
.timeline { list-style: none; margin: 0; padding: 0; }
.timeline li { padding: 6px 0; border-top: 1px solid #e4eae6; display: flex; gap: 10px; flex-wrap: wrap; align-items: baseline; }
.kind { font-family: var(--mono); font-size: 0.8rem; color: var(--accent); }
.when { color: #4c5a55; font-size: 0.8rem; }
.event-body, .task .event-body { overflow-wrap: anywhere; }
.blockquote, blockquote { margin: 6px 0; padding: 8px 12px; border-left: 3px solid var(--accent); background: #f2f6f3; }
.source-text, .claim-quote { max-height: 200px; overflow: auto; white-space: pre-wrap; }
ul.rubric, ul.claims, ul.sources, ul.citations, ul.graph-nodes { list-style: none; margin: 8px 0; padding: 0; }
ul.rubric li, ul.claims li { padding: 6px 0; border-top: 1px solid #e4eae6; }
.crit-id { font-family: var(--mono); font-size: 0.8rem; margin-right: 8px; }
.mono { font-family: var(--mono); font-size: 0.85em; }
#toast { position: fixed; bottom: 14px; left: 50%; transform: translateX(-50%); background: var(--ink); color: #fff; padding: 8px 16px; border-radius: 8px; max-width: 90vw; opacity: 0; transition: opacity 0.2s; pointer-events: none; }
#toast:not(:empty) { opacity: 1; }
#toast.error { background: var(--danger); }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
@media (max-width: 640px) {
  .console { padding: 10px; }
  .topbar { padding: 8px 10px; }
  .kv { grid-template-columns: 1fr; }
  .kv dd { margin: 0 0 6px; }
  .btn-grid button, button.primary, button.secondary { width: 100%; }
  textarea.json-editor { font-size: 0.78rem; }
}
"""
