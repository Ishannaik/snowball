"""Explicit OpenAI-compatible model roles. Every dispatch is a configured HTTP JSON call."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

ROLES = ("interview", "planner", "research", "review", "synthesis", "judge")
ROLE_ENV = {
    "interview": "SNOWBALL_INTERVIEW_MODEL",
    "planner": "SNOWBALL_PLANNER_MODEL",
    "research": "SNOWBALL_RESEARCH_MODEL",
    "review": "SNOWBALL_REVIEW_MODEL",
    "synthesis": "SNOWBALL_SYNTHESIS_MODEL",
    "judge": "SNOWBALL_JUDGE_MODEL",
}
INDEPENDENT_ROLES = frozenset({"review", "judge"})
PRODUCER_ROLES = frozenset({"research", "planner"})
PROMPT_VERSION = "generic-1"
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_OUTPUT_TOKENS = 4096


class RoleError(ValueError):
    """Configuration, transport, or identity failure. Never carries credential data."""

    def __init__(self, field: str, message: str, *, receipt: dict[str, Any] | None = None):
        self.field = field
        self.receipt = receipt or {}
        super().__init__(message)


def _env(name: str) -> str | None:
    import os
    value = os.environ.get(name)
    return value if isinstance(value, str) and value.strip() else None


def _env_int(name: str) -> int | None:
    value = _env(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise RoleError(name, "environment value must be an integer") from None
    return parsed


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RoleError(field, f"{field} must be a positive integer")
    return value


def _model_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoleError(field, f"{field} must be a configured model name")
    return value.strip()


class RoleClient:
    """OpenAI-compatible /chat/completions adapter with strict identity and JSON rules."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = dict(config or {})
        unknown = set(config) - {
            "base_url", "api_key", "models", "timeout", "max_output_tokens", "recipients",
        }
        if unknown:
            raise RoleError("config", f"unknown configuration field {sorted(unknown)[0]}")
        self.base_url = config.get("base_url") or _env("SNOWBALL_MODEL_BASE_URL")
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise RoleError("base_url", "SNOWBALL_MODEL_BASE_URL or config base_url is required")
        self.base_url = self.base_url.strip().rstrip("/")
        api_key = config.get("api_key") if "api_key" in config else _env("SNOWBALL_MODEL_API_KEY")
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise RoleError("api_key", "api_key must be a nonempty string or omitted")
        self.api_key = api_key.strip() if isinstance(api_key, str) else None
        models = config.get("models")
        if not isinstance(models, dict):
            fresh = {}
            for role in ROLES:
                env_value = _env(ROLE_ENV[role])
                if env_value is None:
                    raise RoleError(ROLE_ENV[role], "model role is not configured")
                fresh[role] = _model_name(env_value, ROLE_ENV[role])
            models = fresh
        if set(models) != set(ROLES):
            raise RoleError("models", "all six roles must be configured")
        self.models = {role: _model_name(models[role], role) for role in ROLES}
        for role in INDEPENDENT_ROLES:
            if self.models[role] in {self.models["research"], self.models["planner"]}:
                raise RoleError(
                    role,
                    "review and judge must use a different model than research or planner for independence",
                )
        self.timeout = _positive_int(
            config.get("timeout", DEFAULT_TIMEOUT) if config.get("timeout") is not None else DEFAULT_TIMEOUT,
            "timeout",
        )
        configured_max = config.get("max_output_tokens")
        if configured_max is None:
            configured_max = _env_int("SNOWBALL_MAX_OUTPUT_TOKENS")
        if configured_max is None:
            configured_max = DEFAULT_MAX_OUTPUT_TOKENS
        self.max_output_tokens = _positive_int(configured_max, "max_output_tokens")
        recipients = config.get("recipients") or {}
        if not isinstance(recipients, dict):
            raise RoleError("recipients", "recipients must map roles to recipient ids")
        self.recipients = dict(recipients)

    def recipient(self, role: str) -> str:
        if role not in ROLES:
            raise RoleError("role", "unknown role")
        recipient = self.recipients.get(role)
        if isinstance(recipient, str) and recipient.strip():
            return recipient.strip()
        return role

    def diagnostics(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "models": dict(self.models),
            "recipients": dict(self.recipients),
            "timeout": self.timeout,
            "max_output_tokens": self.max_output_tokens,
            "prompt_version": PROMPT_VERSION,
        }

    # ---------------------------------------------------------------- reservations

    def reservation(self, role: str, payload: dict[str, Any], max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS) -> dict[str, Any]:
        if role not in ROLES:
            raise RoleError("role", "unknown role")
        max_tokens = _positive_int(max_tokens, "max_tokens")
        # Budget keys only: Store.reserve treats any other field as an unknown
        # resource. The exact request this call would build gets costed here.
        messages = self._messages(role, payload)
        body = json.dumps({"messages": messages}, ensure_ascii=False).encode("utf-8")
        input_tokens = max(1, len(body) + len(messages) * 8 + 256)
        return {
            "model_calls": 1,
            "model_tokens": input_tokens + max_tokens,
        }

    # ---------------------------------------------------------------- prompts

    def _messages(self, role: str, payload: dict[str, Any]) -> list[dict[str, str]]:
        """Build wire messages from an explicit DATA payload.

        Callers hand over role data (brief, context, claims, rubric, ...) —
        never chat history. Code owns the instructions: a static system
        message (this role's output schema contract) plus one user message
        holding the canonical JSON of the whole payload.
        """
        if not isinstance(payload, dict):
            raise RoleError("payload", "payload must be an object")
        if not payload:
            raise RoleError("payload", "payload must not be empty")
        instruction = OUTPUT_SCHEMA_PROMPTS[role]
        user = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return [
            {"role": "system", "content": instruction},
            {"role": "user", "content": user},
        ]

    # ---------------------------------------------------------------- transport

    _OPENER: Any = None

    @classmethod
    def _opener(cls) -> Any:
        """One shared opener: no redirects followed, no proxies, no credential leak."""
        if cls._OPENER is None:
            import urllib.request as _request

            class _NoRedirect(_request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                    return None

            cls._OPENER = _request.build_opener(_NoRedirect, _request.ProxyHandler({}))
        return cls._OPENER

    def call(self, role: str, payload: dict[str, Any], *, max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS) -> dict[str, Any]:
        if role not in ROLES:
            raise RoleError("role", "unknown role")
        max_tokens = _positive_int(max_tokens, "max_tokens")
        if max_tokens > self.max_output_tokens:
            raise RoleError("max_tokens", "requested output exceeds the configured role ceiling")
        model = self.models[role]
        messages = self._messages(role, payload)
        url = self.base_url + "/chat/completions"
        body = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        receipt: dict[str, Any] = {
            "role": role,
            "requested_model": model,
            "reported_model": None,
            "model_id": None,
            "tokens_in": 0,
            "tokens_out": 0,
            "usage_kind": "unknown",
            "transport": "http_json",
            "prompt_version": PROMPT_VERSION,
            "recipient": self.recipient(role),
        }
        try:
            response = self._opener().open(request, timeout=self.timeout)
            with response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # The NoRedirect handler leaves redirect responses here: refuse them
            # before anything is followed.
            if exc.code in {301, 302, 303, 307, 308}:
                receipt["usage_kind"] = "estimated"
                raise RoleError("transport", f"model endpoint returned HTTP {exc.code} redirect; refusing to follow", receipt=receipt) from None
            receipt["usage_kind"] = "unknown"
            try:
                exc.read()[:512].decode("utf-8", "replace")
            except Exception:
                pass
            raise RoleError("http", f"model endpoint returned HTTP {exc.code}", receipt=receipt) from None
        except urllib.error.URLError as exc:
            receipt["usage_kind"] = "unknown"
            raise RoleError("transport", f"model endpoint unreachable: {exc.reason}") from None
        except TimeoutError:
            receipt["usage_kind"] = "unknown"
            raise RoleError("transport", "model endpoint timed out") from None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            receipt["usage_kind"] = "unknown"
            raise RoleError("json", "response is not valid JSON", receipt=receipt) from None
        if not isinstance(envelope, dict):
            receipt["usage_kind"] = "unknown"
            raise RoleError("json", "response envelope is not an object", receipt=receipt)
        reported = envelope.get("model")
        if not isinstance(reported, str) or not reported.strip():
            receipt["usage_kind"] = "unknown"
            raise RoleError("model", "provider did not report a model identity", receipt=receipt)
        receipt["reported_model"] = reported.strip()
        if receipt["reported_model"] != receipt["requested_model"]:
            receipt["tokens_in"], receipt["tokens_out"], receipt["usage_kind"] = self._usage(envelope.get("usage"))
            raise RoleError(
                "model",
                f"provider reported {receipt['reported_model']!r} for requested {receipt['requested_model']!r}",
                receipt=receipt,
            )
        usage = envelope.get("usage")
        receipt["tokens_in"], receipt["tokens_out"], receipt["usage_kind"] = self._usage(usage)
        try:
            content = self._content(envelope)
            receipt["model_id"] = receipt["reported_model"]
            receipt["output"] = self._parse_json(content)
        except RoleError as exc:
            # Identity and usage are already verified: the exception must carry
            # the receipt (still without output or credentials) so the runtime
            # can retain the actual reported usage instead of charging lost.
            exc.receipt = dict(receipt)
            exc.receipt.pop("output", None)
            raise
        return receipt

    @staticmethod
    def _usage(usage: Any) -> tuple[int, int, str]:
        if isinstance(usage, dict):
            tokens_in = usage.get("prompt_tokens")
            tokens_out = usage.get("completion_tokens")
            if isinstance(tokens_in, int) and isinstance(tokens_out, int) and tokens_in >= 0 and tokens_out >= 0:
                return tokens_in, tokens_out, "actual" if usage.get("reasoning_tokens") is None else "actual_reasoning"
        return 0, 0, "unknown"

    @staticmethod
    def _content(envelope: dict[str, Any]) -> str:
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RoleError("choices", "response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise RoleError("content", "response message content is empty")
        if envelope.get("finish_reason") == "length" or choices[0].get("finish_reason") == "length":
            raise RoleError("content", "output was truncated by the token bound; refusing it")
        return content

    @staticmethod
    def _parse_json(content: str) -> Any:
        text = content.strip()
        if text.startswith("```"):
            stripped = text[3:]
            if stripped.startswith("json"):
                stripped = stripped[4:]
            text = stripped.strip().rstrip("`").strip()
        if not (text.startswith("{") or text.startswith("[")):
            start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
            if start < 0:
                raise RoleError("output", "model output is not a JSON object")
            text = text[start:]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise RoleError("output", "model output is not valid JSON") from None
        if not isinstance(parsed, dict):
            raise RoleError("output", "model output must be a JSON object")
        return parsed


OUTPUT_SCHEMA_PROMPTS = {
    "interview": (
        "You are the interview role for a generic research harness. Reply with ONE JSON object, no prose:\n"
        '{"questions":[{"id":string,"prompt":string,"input_type":"text"|"boolean"|"integer"|"decimal"|'
        '"choice"|"list"|"object"|"document_reference","required":bool,"choices":optional[string array],'
        '"label":optional[string array]}],'
        '"rubric":{"criteria":[{"id":string,"description":string,"required":bool,"expected_evidence":string,'
        '"label":string array,"qualifications":[{"id":string,"text":string}]}]},'
        '"brief":{"text":string,"derived_from":[question id strings],"label":[string array]}}\n'
        "Rules: ids match [a-z0-9_.-]{1,64}; question ids unique; choices only for input_type choice; "
        "rubric criteria unique ids; label lists name approved context classes (empty list means public); "
        "derived_from cites only question ids you were shown; mention no values from labeled inputs; "
        "propose only; you never approve or grant anything."
    ),
    "planner": (
        "You are the planner role for a generic research harness. Reply with ONE JSON object, no prose:\n"
        '{"tasks":[{"criterion_id":string,"tool":"search"|"fetch"|"read","query_text":string,'
        '"public_context_ids":[string],"url":optional string,"source_id":optional string,"reason":string}],"reason":string}\n'
        "Rules: tasks carry the exact query text; public_context_ids reference only ids shown in authorized context; "
        "fetch/read tasks need url/source_id; tools are only search, fetch, read; queries must derive from the "
        "authorized inputs provided; no other fields."
    ),
    "research": (
        "You are a research worker for a generic research harness. Reply with ONE JSON object, no prose:\n"
        '{"claims":[{"source_id":string,"quote":string,"text":string,"start":optional integer,"end":optional integer,'
        '"criterion_ids":[string],"score":number}],"tasks":[{'
        '"criterion_id":string,"tool":"search"|"fetch"|"read","query_text":string,"public_context_ids":[string],'
        '"url":optional string,"source_id":optional string,"reason":string}]}\n'
        "Rules: quote must be copied character-for-character from the supplied source excerpt; offset start/end are "
        "optional and code checks the unique literal occurrence; text is your finding summary citing the quote; "
        "criterion_ids reference only rubric ids shown; score is 0..10 relevance. Never fabricate a source_id "
        "you were not shown."
    ),
    "review": (
        "You are the independent review role. Reply with ONE JSON object, no prose:\n"
        '{"reviews":[{"claim_id":string,"verdict":"kept"|"disputed"|"rejected"|"unknown","reason":string}]}\n'
        "Rules: claim_id must reference a claim shown to you; each reviewed claim appears at most once; "
        "verdicts state what the claimed support is worth; 'kept' means verified, 'disputed' means conflicting material "
        "exists, 'rejected' means unsupported or malformed, 'unknown' means cannot decide."
    ),
    "synthesis": (
        "You are the synthesis role. Reply with ONE JSON object, no prose:\n"
        '{"text":string,"evidence_ids":[string],"rubric_met":false}\n'
        "Rules: text is the proposed deliverable; cite only stored claim ids you were shown as kept evidence; "
        "you cannot mark the rubric met; rubric_met MUST be false."
    ),
    "judge": (
        "You are the independent judge role. Reply with ONE JSON object, no prose:\n"
        '{"rows":[{"criterion_id":string,"verdict":"met"|"unmet"|"unknown"|"conditional"|"contested","reason":string,'
        '"support_ids":[string],"conflict_ids":[string] | null,"missing_evidence":string | null,'
        '"next_questions":[string] | null,"qualification_id":string | null}],'
        '"deliverable":{"supported":bool,"reason":string,"evidence_ids":[string]}}\n'
        "Rules: one row per criterion given to you, none skipped, none duplicated; positive and conditional "
        "verdicts must cite kept claim ids from the evidence shown; conflicts cite disputed claim ids; "
        "'unknown' when evidence is absent; conditional requires a qualification id accepted in the rubric; "
        "the deliverable assessment is your own and never lets the producing roles decide success."
    ),
}
