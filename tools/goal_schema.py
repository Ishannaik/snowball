"""Schema-v4 goal/evidence declarations; executed only by explicit initialization/migration."""

SCHEMA_VERSION = 4

DDL = """
CREATE TABLE goals (
  id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('draft','needs_information','contract_proposed','approved','running','pausing','paused','revising','research_ready','monitoring','stopped')),
  current_contract_version INTEGER, current_strategy_version INTEGER NOT NULL DEFAULT 0,
  epoch INTEGER NOT NULL DEFAULT 0, row_version INTEGER NOT NULL DEFAULT 0,
  revision_from_state TEXT, stop_reason TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE goal_contracts (
  goal_id INTEGER NOT NULL REFERENCES goals(id), version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','proposed','approved','superseded','discarded')),
  body_json TEXT NOT NULL, body_hash TEXT NOT NULL,
  profile_snapshot_json TEXT NOT NULL, profile_snapshot_hash TEXT NOT NULL,
  frame_json TEXT, frame_hash TEXT,
  created_at INTEGER NOT NULL, approved_at INTEGER, approved_via TEXT, supersedes_version INTEGER,
  PRIMARY KEY (goal_id, version));
CREATE TABLE profile_fields (
  goal_id INTEGER NOT NULL REFERENCES goals(id), key TEXT NOT NULL, value_json TEXT,
  status TEXT NOT NULL CHECK (status IN ('known','draft','unconfirmed')),
  provenance TEXT NOT NULL CHECK (provenance IN ('user_stated','user_confirmed','imported_history','system_default')),
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','public_ok')),
  row_version INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL, PRIMARY KEY (goal_id, key));
CREATE TABLE interview_sessions (id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goals(id),
  state TEXT NOT NULL CHECK (state IN ('open','closed')), created_at INTEGER NOT NULL, closed_at INTEGER);
CREATE TABLE interview_turns (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES interview_sessions(id),
  seq INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('question','answer','note')), field_key TEXT,
  text TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE (session_id, seq));
CREATE TABLE execution_grants (
  id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goals(id), contract_version INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('research','monitoring')),
  state TEXT NOT NULL CHECK (state IN ('active','exhausted','revoked','expired')),
  limits_json TEXT NOT NULL, starts_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  row_version INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, revoked_at INTEGER, revoke_reason TEXT,
  FOREIGN KEY (goal_id, contract_version) REFERENCES goal_contracts(goal_id, version));
CREATE UNIQUE INDEX one_active_grant ON execution_grants(goal_id) WHERE state='active';
CREATE TABLE strategies (
  goal_id INTEGER NOT NULL REFERENCES goals(id), version INTEGER NOT NULL, contract_version INTEGER NOT NULL,
  body_json TEXT NOT NULL, outcome TEXT, reason TEXT, created_at INTEGER NOT NULL, PRIMARY KEY (goal_id, version));
CREATE TABLE goal_events (
  id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goals(id), at INTEGER NOT NULL,
  actor TEXT NOT NULL, kind TEXT NOT NULL, operation TEXT NOT NULL, from_state TEXT, to_state TEXT, payload_json TEXT,
  idempotency_key TEXT, request_hash TEXT, result_status INTEGER, result_json TEXT,
  UNIQUE(actor, operation, idempotency_key));

CREATE TABLE frontier_decisions (
  id INTEGER PRIMARY KEY, wave_id INTEGER NOT NULL, goal_id INTEGER NOT NULL, strategy_version INTEGER NOT NULL,
  node_kind TEXT NOT NULL CHECK (node_kind IN ('page','query')), node_id INTEGER NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('selected','deferred','dropped')),
  score REAL, components_json TEXT NOT NULL, reason TEXT NOT NULL, at INTEGER NOT NULL);

CREATE TABLE resource_ledger (
  scope_key TEXT NOT NULL,
  window_kind TEXT NOT NULL CHECK (window_kind IN ('day','lifetime')), window_key TEXT NOT NULL,
  counter TEXT NOT NULL, reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  settled INTEGER NOT NULL DEFAULT 0 CHECK (settled >= 0), lost INTEGER NOT NULL DEFAULT 0 CHECK (lost >= 0),
  PRIMARY KEY (scope_key, window_kind, window_key, counter));
CREATE TABLE resource_reservations (
  id INTEGER PRIMARY KEY, grant_id INTEGER NOT NULL REFERENCES execution_grants(id), task_id INTEGER,
  operation_key TEXT NOT NULL, scope_key TEXT NOT NULL, window_kind TEXT NOT NULL, window_key TEXT NOT NULL,
  counter TEXT NOT NULL, amount INTEGER NOT NULL CHECK (amount > 0),
  actual INTEGER CHECK (actual IS NULL OR actual >= 0), state TEXT NOT NULL CHECK (state IN ('reserved','settled','lost')),
  created_at INTEGER NOT NULL, settled_at INTEGER,
  UNIQUE (grant_id, operation_key, scope_key, window_kind, window_key, counter));

CREATE TABLE programs (
  key TEXT PRIMARY KEY, name TEXT NOT NULL, institution TEXT, country TEXT, official_domain TEXT,
  degree_level TEXT CHECK (degree_level IN ('masters','phd','other','unknown')),
  modality TEXT CHECK (modality IN ('on_campus','online','hybrid','unknown')), field_tags_json TEXT NOT NULL DEFAULT '[]',
  provenance TEXT NOT NULL CHECK (provenance IN ('observed','lead_legacy')),
  first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL);
CREATE TABLE source_snapshots (
  id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES pages(id), fetch_id INTEGER REFERENCES fetches(id),
  content_hash TEXT NOT NULL, retrieved_at INTEGER NOT NULL, text_path TEXT NOT NULL, original_raw_path TEXT,
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('public','private')),
  provenance TEXT NOT NULL DEFAULT 'unknown', source_cycle_label TEXT, applicability_json TEXT NOT NULL DEFAULT '{}',
  superseded_by INTEGER REFERENCES source_snapshots(id));
CREATE TABLE observations (
  id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(id), page_id INTEGER NOT NULL,
  program_key TEXT REFERENCES programs(key), field_key TEXT NOT NULL, value_json TEXT NOT NULL,
  subject_json TEXT NOT NULL,
  currency TEXT, period TEXT CHECK (period IN ('per_year','per_semester','per_month','one_time','total')), as_of TEXT,
  conditions_json TEXT, quote TEXT NOT NULL, quote_start INTEGER NOT NULL, quote_end INTEGER NOT NULL,
  extractor_model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  assurance TEXT NOT NULL CHECK (assurance IN ('code_checked','reviewed_kept','reviewed_dropped','review_pending','review_unavailable')),
  review_model TEXT, review_reason TEXT, task_id INTEGER, goal_id INTEGER, contract_version INTEGER, strategy_version INTEGER, epoch INTEGER,
  created_at INTEGER NOT NULL, invalidated_at INTEGER, invalidated_reason TEXT);
CREATE TABLE funding_packages (
  key TEXT PRIMARY KEY, program_key TEXT NOT NULL REFERENCES programs(key),
  cycle_label TEXT NOT NULL, award_keys_json TEXT NOT NULL, components_json TEXT NOT NULL,
  compatibility_basis_ids_json TEXT NOT NULL, cash_flow_json TEXT NOT NULL,
  basis_observation_ids_json TEXT NOT NULL, computed_at INTEGER NOT NULL);
CREATE TABLE constraint_evaluations (
  goal_id INTEGER NOT NULL, contract_version INTEGER NOT NULL, program_key TEXT NOT NULL, constraint_key TEXT NOT NULL,
  package_key TEXT NOT NULL REFERENCES funding_packages(key),
  status TEXT NOT NULL CHECK (status IN ('met','unmet','unknown','conditional')), basis_json TEXT NOT NULL, reason TEXT NOT NULL,
  evaluated_at INTEGER NOT NULL, epoch INTEGER NOT NULL, stale_for_goal INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (goal_id, contract_version, package_key, constraint_key));
CREATE TABLE contradictions (
  id INTEGER PRIMARY KEY, program_key TEXT NOT NULL, field_key TEXT NOT NULL, observation_a INTEGER NOT NULL, observation_b INTEGER NOT NULL,
  precedence_rule TEXT, winner INTEGER, resolved_at INTEGER, resolution_reason TEXT);
CREATE TABLE fx_rates (base TEXT NOT NULL, quote TEXT NOT NULL, rate_decimal TEXT NOT NULL, as_of TEXT NOT NULL,
  source_page_id INTEGER, PRIMARY KEY (base, quote, as_of));

CREATE UNIQUE INDEX one_running_goal ON goals((1)) WHERE state IN ('running','monitoring');
CREATE TABLE goal_topics (goal_id INTEGER NOT NULL REFERENCES goals(id),contract_version INTEGER NOT NULL,topic_id INTEGER NOT NULL REFERENCES topics(id),role TEXT NOT NULL CHECK(role IN ('candidate','alternative')),visibility TEXT NOT NULL DEFAULT 'public' CHECK(visibility IN ('public','private')),PRIMARY KEY(goal_id,contract_version,topic_id));
CREATE TABLE goal_pages (goal_id INTEGER NOT NULL REFERENCES goals(id),contract_version INTEGER NOT NULL,page_id INTEGER NOT NULL REFERENCES pages(id),parent_page_id INTEGER,depth INTEGER NOT NULL CHECK(depth>=0),origin TEXT NOT NULL,visibility TEXT NOT NULL CHECK(visibility IN ('public','private')),queued_at INTEGER NOT NULL,visited_at INTEGER,PRIMARY KEY(goal_id,contract_version,page_id));
CREATE TABLE goal_queries (goal_id INTEGER NOT NULL REFERENCES goals(id),contract_version INTEGER NOT NULL,query_id INTEGER NOT NULL REFERENCES queries(id),program_key TEXT,field_key TEXT,origin TEXT NOT NULL,visibility TEXT NOT NULL DEFAULT 'public',PRIMARY KEY(goal_id,contract_version,query_id));
CREATE TABLE goal_programs (goal_id INTEGER NOT NULL REFERENCES goals(id),contract_version INTEGER NOT NULL,program_key TEXT NOT NULL REFERENCES programs(key),role TEXT NOT NULL CHECK(role IN ('candidate','alternative')),visibility TEXT NOT NULL CHECK(visibility IN ('public','private')),PRIMARY KEY(goal_id,contract_version,program_key));
CREATE TABLE goal_node_identities (goal_id INTEGER NOT NULL REFERENCES goals(id),node_kind TEXT NOT NULL CHECK(node_kind IN ('page','query')),node_key TEXT NOT NULL,queued_at INTEGER,visited_at INTEGER,PRIMARY KEY(goal_id,node_kind,node_key));
CREATE TABLE resource_operations (id INTEGER PRIMARY KEY,goal_id INTEGER NOT NULL REFERENCES goals(id),grant_id INTEGER NOT NULL REFERENCES execution_grants(id),operation_key TEXT NOT NULL,request_hash TEXT NOT NULL,binding_json TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','settled','lost','cancelled')),created_at INTEGER NOT NULL,dispatched_at INTEGER,UNIQUE(goal_id,operation_key));
CREATE TABLE domain_slots (id INTEGER PRIMARY KEY,domain TEXT NOT NULL,task_id INTEGER NOT NULL REFERENCES tasks(id),operation_id INTEGER REFERENCES resource_operations(id),lease_token TEXT NOT NULL,lease_until INTEGER NOT NULL,UNIQUE(task_id,lease_token));
CREATE INDEX domain_slots_domain ON domain_slots(domain,lease_until);
CREATE TABLE model_calls (id INTEGER PRIMARY KEY,goal_id INTEGER REFERENCES goals(id),task_id INTEGER REFERENCES tasks(id),wave_id INTEGER REFERENCES waves(id),role TEXT NOT NULL,requested_model TEXT NOT NULL,reported_model TEXT,transport TEXT NOT NULL,prompt_version TEXT,public_frame_hash TEXT,source_snapshot_ids_json TEXT NOT NULL DEFAULT '[]',tokens_in INTEGER,tokens_out INTEGER,thinking_tokens INTEGER,usage_kind TEXT NOT NULL CHECK(usage_kind IN ('actual','estimated','legacy','unknown')),state TEXT NOT NULL CHECK(state IN ('pending','complete','failed','lost')),reservation_operation_id INTEGER REFERENCES resource_operations(id),started_at INTEGER NOT NULL,finished_at INTEGER,error_code TEXT);
CREATE TABLE console_tokens (id INTEGER PRIMARY KEY,token_hash TEXT NOT NULL UNIQUE,expires_at INTEGER NOT NULL,consumed_at INTEGER,created_at INTEGER NOT NULL);
CREATE TABLE console_sessions (id INTEGER PRIMARY KEY,session_hash TEXT NOT NULL UNIQUE,csrf_value TEXT NOT NULL,actor TEXT NOT NULL,expires_at INTEGER NOT NULL,revoked_at INTEGER,created_at INTEGER NOT NULL);
CREATE TABLE console_rates (scope TEXT NOT NULL,window INTEGER NOT NULL,attempts INTEGER NOT NULL,PRIMARY KEY(scope,window));
CREATE TABLE goal_runtime (goal_id INTEGER PRIMARY KEY REFERENCES goals(id),substate TEXT,reason TEXT,work_fingerprint TEXT,coverage_fingerprint TEXT,stall_count INTEGER NOT NULL DEFAULT 0,last_wave_id INTEGER REFERENCES waves(id),updated_at INTEGER NOT NULL);
CREATE TABLE program_field_cooldowns (goal_id INTEGER NOT NULL REFERENCES goals(id),contract_version INTEGER NOT NULL,program_key TEXT NOT NULL,field_key TEXT NOT NULL,next_query_at INTEGER NOT NULL,snapshot_hash TEXT,PRIMARY KEY(goal_id,contract_version,program_key,field_key));
CREATE TABLE wiki_regions (path TEXT NOT NULL,region_key TEXT NOT NULL,provenance TEXT NOT NULL CHECK(provenance IN ('source_evidence','human_note')),body TEXT NOT NULL,PRIMARY KEY(path,region_key));
"""
