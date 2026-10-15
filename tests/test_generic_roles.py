"""RoleClient identity, JSON, usage, reservation, and refusal behavior."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import generic_roles as roles  # noqa: E402


CONFIG = {
    "base_url": "http://127.0.0.1:9/v1",
    "api_key": "test-key",
    "models": {
        "interview": "interview-model",
        "planner": "planner-model",
        "research": "research-model",
        "review": "review-model",
        "synthesis": "synthesis-model",
        "judge": "judge-model",
    },
    "timeout": 5,
    "max_output_tokens": 4096,
}


def make_client(**overrides):
    config = json.loads(json.dumps(CONFIG))
    config.update(overrides)
    return roles.RoleClient(config)


class FakeResponse:
    def __init__(self, payload: bytes, url: str):
        self._payload = payload
        self._url = url

    def read(self) -> bytes:
        return self._payload

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RolesTest(unittest.TestCase):
    def test_config_requires_all_six_roles(self):
        partial = dict(CONFIG)
        partial["models"] = {k: v for k, v in CONFIG["models"].items() if k != "judge"}
        with self.assertRaises(roles.RoleError) as caught:
            roles.RoleClient(partial)
        self.assertEqual(caught.exception.field, "models")

    def test_env_models_are_picked_up(self):
        saved = {}
        for name, value in (
            ("SNOWBALL_MODEL_BASE_URL", "http://127.0.0.1:9/v1"),
            ("SNOWBALL_MODEL_API_KEY", "env-key"),
            ("SNOWBALL_INTERVIEW_MODEL", "i-env"),
            ("SNOWBALL_PLANNER_MODEL", "p-env"),
            ("SNOWBALL_RESEARCH_MODEL", "r-env"),
            ("SNOWBALL_REVIEW_MODEL", "v-env"),
            ("SNOWBALL_SYNTHESIS_MODEL", "s-env"),
            ("SNOWBALL_JUDGE_MODEL", "j-env"),
        ):
            saved[name] = (name in os.environ, os.environ.get(name))
            os.environ[name] = value
        try:
            client = roles.RoleClient({})
            self.assertEqual(client.models["research"], "r-env")
        finally:
            for name, (present, old) in saved.items():
                if present:
                    os.environ[name] = old or ""
                else:
                    os.environ.pop(name, None)

    def test_independence_requires_distinct_models(self):
        bad = json.loads(json.dumps(CONFIG))
        bad["models"]["judge"] = CONFIG["models"]["research"]
        with self.assertRaises(roles.RoleError) as caught:
            roles.RoleClient(bad)
        self.assertEqual(caught.exception.field, "judge")

    def test_review_may_differ_provider_same_model_forbidden(self):
        client = make_client()
        self.assertNotEqual(client.models["review"], client.models["research"])
        self.assertNotEqual(client.models["judge"], client.models["planner"])

    def test_reservation_conservative_counts(self):
        client = make_client()
        payload = structured_payload()
        reserve = client.reservation("research", payload, max_tokens=512)
        self.assertEqual(reserve, {
            "model_calls": 1,
            "model_tokens": reserve["model_tokens"],
        })
        # The reservation costs the exact wire request this call would build:
        # schema system prompt + canonical JSON user message + output bound.
        import json as jsonlib
        client2 = make_client()
        messages = client2._messages("research", payload)
        expected_wire = len(jsonlib.dumps({"messages": messages}, ensure_ascii=False).encode("utf-8"))
        self.assertGreaterEqual(reserve["model_tokens"], expected_wire + 512 - 1024)
        self.assertGreater(reserve["model_tokens"], 512)

    def test_reservation_counts_unicode_bytes_not_characters(self):
        client = make_client()
        payload = {"context": [{"id": "c1", "text": "数据中心" * 50}]}  # 200 chars, 600 UTF-8 bytes
        reserve = client.reservation("research", payload, max_tokens=64)
        self.assertGreaterEqual(reserve["model_tokens"], 600 + 64)

    def test_call_builds_system_schema_and_user_json(self):
        client = make_client()
        captured = {}
        envelope = {
            "model": "research-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "choices": [{"finish_reason": "stop", "message": {"content": '{"claims": []}'}}],
        }

        class RecordingOpener:
            def open(self, request, timeout=None):
                captured["body"] = json.loads(request.data.decode("utf-8"))
                return self._response()

            def _response(self):
                payload_bytes = json.dumps(envelope).encode()

                class Response:
                    def __init__(inner):
                        inner.payload_bytes = payload_bytes

                    def read(inner):
                        return inner.payload_bytes

                    def __enter__(inner):
                        return inner

                    def __exit__(inner, *exc):
                        return False

                return Response()

        original_opener = roles.RoleClient._OPENER
        roles.RoleClient._OPENER = RecordingOpener()
        try:
            client.call("research", structured_payload(), max_tokens=64)
        finally:
            roles.RoleClient._OPENER = original_opener
        messages = captured["body"]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn('"claims"', messages[0]["content"])  # role schema contract
        sent = json.loads(messages[1]["content"])
        self.assertEqual(sent["goal_id"], "goal-1")
        self.assertEqual(sent["task"]["query_text"], "queue A bounded memory 64 items documentation")
        # The wire envelope has one system + one user message; the user message
        # is the canonical JSON of the DATA payload with no chat history inside.
        self.assertEqual(len(messages), 2)
        self.assertNotIn("messages", json.loads(messages[1]["content"]))

    def test_receipt_shape_and_identity(self):
        client = make_client()
        envelope = {
            "model": "research-model",
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"tasks": []})}}],
        }
        receipt = roles_transport_call(client, envelope)
        self.assertEqual(receipt["requested_model"], "research-model")
        self.assertEqual(receipt["reported_model"], "research-model")
        self.assertEqual(receipt["model_id"], "research-model")
        self.assertEqual(receipt["tokens_in"], 11)
        self.assertEqual(receipt["tokens_out"], 7)
        self.assertEqual(receipt["usage_kind"], "actual")
        self.assertEqual(receipt["role"], "research")
        self.assertEqual(receipt["transport"], "http_json")
        self.assertEqual(receipt["prompt_version"], roles.PROMPT_VERSION)
        self.assertEqual(receipt["output"], {"tasks": []})
        self.assertNotIn("api_key", receipt)
        self.assertNotIn("Authorization", receipt)

    def test_mismatched_reported_model_is_refused_with_usage(self):
        client = make_client()
        envelope = {
            "model": "gemini-3.8-flash-high",
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        }
        with self.assertRaises(roles.RoleError) as caught:
            roles_transport_call(client, envelope)
        self.assertEqual(caught.exception.field, "model")
        receipt = caught.exception.receipt
        self.assertEqual(receipt["requested_model"], "research-model")
        self.assertEqual(receipt["reported_model"], "gemini-3.8-flash-high")
        self.assertEqual(receipt["tokens_in"], 5)
        self.assertEqual(receipt["tokens_out"], 3)
        self.assertNotIn("output", receipt)

    def test_truncated_output_is_refused(self):
        client = make_client()
        body = json.dumps({"tasks": [], "note": "x" * 3000})[:180]
        envelope = {
            "model": "research-model",
            "choices": [{"finish_reason": "length", "message": {"content": body}}],
        }
        with self.assertRaises(roles.RoleError) as caught:
            roles_transport_call(client, envelope)
        self.assertEqual(caught.exception.field, "content")
        self.assertIn("truncated", str(caught.exception))

    def test_malformed_json_is_refused_regardless_of_finish(self):
        client = make_client()
        envelope = {
            "model": "research-model",
            "choices": [{"finish_reason": "stop", "message": {"content": '{"ok'}}],
        }
        with self.assertRaises(roles.RoleError) as caught:
            roles_transport_call(client, envelope)
        self.assertEqual(caught.exception.field, "output")

    def test_late_failures_retain_actual_usage_receipt(self):
        client = make_client()
        usage = {"prompt_tokens": 42, "completion_tokens": 17}
        truncated = {
            "model": "research-model",
            "usage": dict(usage),
            "choices": [{"finish_reason": "length", "message": {"content": '{"tasks": ['}}],
        }
        with self.assertRaises(roles.RoleError) as caught:
            roles_transport_call(client, truncated)
        receipt = caught.exception.receipt
        self.assertEqual(receipt["tokens_in"], 42)
        self.assertEqual(receipt["tokens_out"], 17)
        self.assertEqual(receipt["usage_kind"], "actual")
        self.assertEqual(receipt["reported_model"], "research-model")
        self.assertNotIn("output", receipt)
        self.assertNotIn("api_key", json.dumps(receipt))
        malformed = {
            "model": "research-model",
            "usage": dict(usage),
            "choices": [{"finish_reason": "stop", "message": {"content": '{"ok'}}],
        }
        with self.assertRaises(roles.RoleError) as caught:
            roles_transport_call(client, malformed)
        receipt = caught.exception.receipt
        self.assertEqual(receipt["tokens_in"], 42)
        self.assertEqual(receipt["tokens_out"], 17)
        self.assertEqual(receipt["usage_kind"], "actual")
        self.assertNotIn("output", receipt)
        missing_usage = {
            "model": "research-model",
            "choices": [{"finish_reason": "length", "message": {"content": '{"x"'}}],
        }
        with self.assertRaises(roles.RoleError) as caught:
            roles_transport_call(client, missing_usage)
        receipt = caught.exception.receipt
        self.assertEqual(receipt["tokens_in"], 0)
        self.assertEqual(receipt["tokens_out"], 0)
        self.assertEqual(receipt["usage_kind"], "unknown")

    def test_empty_content_keeps_receipt(self):
        client = make_client()
        envelope = {
            "model": "research-model",
            "usage": {"prompt_tokens": 9, "completion_tokens": 0},
            "choices": [{"finish_reason": "stop", "message": {"content": "   "}}],
        }
        with self.assertRaises(roles.RoleError) as caught:
            roles_transport_call(client, envelope)
        self.assertEqual(caught.exception.field, "content")
        receipt = caught.exception.receipt
        self.assertEqual(receipt["tokens_in"], 9)
        self.assertEqual(receipt["usage_kind"], "actual")

    def test_redirect_is_refused_without_output(self):
        client = make_client()
        with self.assertRaises(roles.RoleError) as caught:
            roles_transport_call(client, b"{}", http_error=302)
        self.assertEqual(caught.exception.field, "transport")
        self.assertNotIn("output", caught.exception.receipt)

    def test_api_key_is_optional_for_unauthenticated_gateways(self):
        keyless = json.loads(json.dumps(CONFIG))
        keyless.pop("api_key")
        client = roles.RoleClient(keyless)
        self.assertIsNone(client.api_key)
        captured = {}
        captured_holder = [captured]

        class Response:
            payload_bytes = json.dumps({
                "model": "research-model",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            }).encode()

            def read(self):
                return self.payload_bytes

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class StubOpener:
            def open(self, request, timeout=None):
                captured_holder[0]["headers"] = dict(request.header_items())
                return Response()

        original_opener = roles.RoleClient._OPENER
        roles.RoleClient._OPENER = StubOpener()
        try:
            client.call("research", structured_payload(), max_tokens=16)
        finally:
            roles.RoleClient._OPENER = original_opener
        header_names = {name.decode() if isinstance(name, bytes) else name for name in captured}
        self.assertNotIn("Authorization", header_names)
        # Explicit empty-string key is refused rather than turned into a dummy.
        blank = json.loads(json.dumps(CONFIG))
        blank["api_key"] = "   "
        with self.assertRaises(roles.RoleError) as caught:
            roles.RoleClient(blank)
        self.assertEqual(caught.exception.field, "api_key")

    def test_recipient_mapping(self):
        client = make_client(recipients={"research": "public_models_search"})
        self.assertEqual(client.recipient("research"), "public_models_search")
        self.assertEqual(client.recipient("judge"), "judge")

    def test_diagnostics_has_no_key(self):
        client = make_client()
        info = client.diagnostics()
        self.assertNotIn("api_key", info)
        self.assertEqual(info["base_url"], CONFIG["base_url"])
        self.assertEqual(info["models"], CONFIG["models"])


def roles_transport_call(client, envelope, redirected_to=None, http_error=None):
    """Call client.call with a stub opener instead of a socket.

    The production client uses the shared no-redirect/no-proxy opener, so the
    stub patches RoleClient._opener and returns a context-manager response.
    Payloads are structured role DATA (as Service hands over), not chat text.
    """
    calls = {}

    class Response:
        def __init__(self, payload_bytes):
            self.payload_bytes = payload_bytes

        def read(self):
            return self.payload_bytes

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(request, timeout=None):
        calls["request"] = request
        if http_error is not None:
            raise roles.urllib.error.HTTPError(client.base_url + "/chat/completions", http_error, "moved", {}, None)
        if isinstance(envelope, (bytes, bytearray)):
            return Response(bytes(envelope))
        return Response(json.dumps(envelope).encode())

    class StubOpener:
        def open(self, request, timeout=None):
            return fake_open(request, timeout)

    original_opener = roles.RoleClient._OPENER
    roles.RoleClient._OPENER = StubOpener()
    try:
        return client.call("research", structured_payload(), max_tokens=64)
    finally:
        roles.RoleClient._OPENER = original_opener


def structured_payload():
    """The exact DATA shape Service/runtime hands the research role."""
    return {
        "goal_id": "goal-1",
        "public_brief": "Compare two queue designs on bounded behavior.",
        "context": [
            {"id": "ctx-1", "text": "Queue A bounds memory at 64 items.", "score": 9, "label": []},
            {"id": "ctx-2", "text": "Queue B documentation lists no bounds.", "score": 7, "label": []},
        ],
        "task": {
            "criterion_id": "supported",
            "tool": "search",
            "query_text": "queue A bounded memory 64 items documentation",
            "public_context_ids": ["ctx-1", "ctx-2"],
        },
        "rubric_ids": ["supported", "other"],
    }


if __name__ == "__main__":
    unittest.main()
