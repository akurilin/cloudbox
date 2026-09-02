"""Verify bounded activity logs without exposing credentials or reasoning."""

import base64
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "cloudbox"))

import supervisor

RUN_ID = "80c7a578-1d79-4c99-b08a-4741eaf3b795"
SECRET = "runtime/key+with=symbols"


class ActivityTraceTests(unittest.TestCase):
    def records(self, events, secrets=()):
        stream = io.StringIO()
        reader = supervisor.PiEvents(RUN_ID, secrets)
        with patch("sys.stdout", stream):
            for event in events:
                reader.accept(event)
        lines = stream.getvalue().splitlines()
        return [json.loads(line) for line in lines], lines

    def test_visible_messages_commands_results_and_errors_are_logged(self):
        records, _ = self.records([
            {"type": "message_end", "message": {"role": "assistant", "timestamp": 1,
                "stopReason": "toolUse", "content": [{"type": "text", "text": "I will inspect the failing test."}]}},
            {"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "bash",
             "args": {"command": "python -m unittest tests.test_example", "timeout": 20}},
            {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "bash", "isError": True,
             "result": {"content": [{"type": "text", "text": "AssertionError: expected 2"}],
                        "details": {"fullOutputPath": "/tmp/test-output.txt"}}},
            {"type": "message_end", "message": {"role": "assistant", "timestamp": 2,
                "stopReason": "stop", "content": [{"type": "text", "text": '{"status":"completed"}'}]}},
        ])
        self.assertEqual("I will inspect the failing test.", records[0]["text"])
        self.assertEqual("python -m unittest tests.test_example", records[1]["arguments"]["command"])
        self.assertEqual("error", records[2]["outcome"])
        self.assertEqual("AssertionError: expected 2", records[2]["result"]["content"][0]["text"])
        self.assertEqual("/tmp/test-output.txt", records[2]["result"]["details"]["fullOutputPath"])
        self.assertEqual('{"status":"completed"}', records[3]["text"])

    def test_reasoning_signatures_images_and_streaming_deltas_are_omitted(self):
        records, lines = self.records([
            {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "PRIVATE-DELTA"}},
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse", "content": [
                {"type": "thinking", "thinking": "PRIVATE-THOUGHT", "thinkingSignature": "PRIVATE-SIGNATURE"},
                {"type": "text", "text": "Visible progress.", "textSignature": "PRIVATE-TEXT-SIGNATURE"},
                {"type": "image", "data": "PRIVATE-IMAGE"},
            ]}},
            {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "read", "isError": False,
             "result": {"content": [{"type": "image", "data": "PRIVATE-IMAGE"},
                                     {"type": "text", "text": "visible output"}],
                        "details": {"nested": {"reasoning": "PRIVATE-DETAIL", "signature": "PRIVATE-SIGNATURE",
                                               "image": {"data": "PRIVATE-IMAGE"}, "count": 2}}}},
        ])
        self.assertEqual(2, len(records))
        self.assertNotIn("PRIVATE-", "".join(lines))
        self.assertEqual("Visible progress.", records[0]["text"])
        self.assertEqual(2, records[1]["result"]["details"]["nested"]["count"])

    def test_known_credentials_are_redacted_in_nested_fields_and_output(self):
        values = [SECRET, quote(SECRET, safe=""), base64.b64encode(SECRET.encode()).decode()]
        records, lines = self.records([
            {"type": "tool_execution_start", "toolCallId": SECRET, "toolName": "bash",
             "args": {"command": f"echo {SECRET}", "nested": [{"value": values[1]}]}},
            {"type": "tool_execution_end", "toolCallId": SECRET, "toolName": "bash", "isError": True,
             "result": {"content": [{"type": "text", "text": " ".join(values)}],
                        "details": {"unexpected": SECRET}}},
        ], [SECRET])
        for value in values:
            self.assertNotIn(value, "".join(lines))
        self.assertIn("[redacted]", json.dumps(records))

    def test_common_secret_fields_and_patterns_are_redacted(self):
        secrets = ["unknown-password", "opaque-access-token", "authorization-value",
                   "sk-or-v1-" + "a" * 40, "ghs_" + "b" * 36, "AKIA" + "C" * 16,
                   "pem-private-value", "url-password", "signed-url-secret"]
        _, lines = self.records([{
            "type": "tool_execution_start", "toolCallId": "call", "toolName": "bash", "args": {
                "password": secrets[0], "nested": {"access_token": secrets[1]},
                "command": (f'Authorization: Bearer {secrets[2]}\n'
                            f'{secrets[3]} {secrets[4]} {secrets[5]}\n'
                            f'-----BEGIN PRIVATE KEY-----\n{secrets[6]}\n-----END PRIVATE KEY-----\n'
                            f'https://name:{secrets[7]}@example.test/file\n'
                            f'https://example.test?X-Amz-Signature={secrets[8]}'),
            },
        }])
        for value in secrets:
            self.assertNotIn(value, "".join(lines))

    def test_redaction_precedes_truncation_and_records_stay_bounded(self):
        # The credential starts before the text limit and ends after it.
        long_secret = "s" * (supervisor.MAX_TRACE_TEXT_BYTES * 2)
        _, lines = self.records([{
            "type": "tool_execution_start", "toolCallId": "call", "toolName": "write",
            "args": {"path": "/tmp/file", "content": long_secret + "\n" + "🧪\\\"" * 20_000},
        }], [long_secret])
        self.assertEqual(1, len(lines))
        self.assertLessEqual(len(lines[0].encode()), supervisor.MAX_TRACE_RECORD_BYTES)
        self.assertIn("truncated", lines[0])
        self.assertNotIn("s" * 100, lines[0])

    def test_large_nested_or_unsupported_values_use_safe_fallbacks(self):
        nested = {"value": "visible"}
        for _ in range(100):
            nested = {"next": nested}
        _, lines = self.records([{
            "type": "tool_execution_start", "toolCallId": "call", "toolName": "edit",
            "args": {"deep": nested, "many": ["x"] * 1000, "binary": b"private-binary", "unknown": object()},
        }])
        self.assertLessEqual(len(lines[0].encode()), supervisor.MAX_TRACE_RECORD_BYTES)
        self.assertNotIn("private-binary", lines[0])
        self.assertNotIn("object at", lines[0])
        self.assertIn("truncated", lines[0])

    def test_repeated_message_end_does_not_duplicate_text_or_usage(self):
        message = {"role": "assistant", "timestamp": 1, "stopReason": "stop",
                   "usage": {"input": 4}, "content": [{"type": "text", "text": "visible"}]}
        records, _ = self.records([{"type": "message_end", "message": message}] * 2)
        self.assertEqual(1, len(records))
        self.assertEqual({"input": 4}, records[0]["usage"])

    def test_model_error_message_is_sanitized_without_provider_payload(self):
        records, lines = self.records([{"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error", "content": [],
            "errorMessage": f"Rate limit for {SECRET}; retry later.",
            "providerPayload": {"reasoning": "PRIVATE-PROVIDER-DATA"},
        }}], [SECRET])
        self.assertEqual("Rate limit for [redacted]; retry later.", records[0]["error_message"])
        self.assertNotIn("PRIVATE-PROVIDER-DATA", "".join(lines))

    def test_runtime_credential_collection_uses_sdk_and_safe_fallback(self):
        session = Mock()
        session.get_credentials.return_value.get_frozen_credentials.return_value = Mock(
            access_key="runtime-access", secret_key="runtime-secret", token="runtime-session",
        )
        data = {"AccessKeyId": "data-access", "SecretAccessKey": "data-secret", "SessionToken": "data-session"}
        values = supervisor.runtime_trace_secrets(session, data)
        self.assertEqual(set(data.values()) | {"runtime-access", "runtime-secret", "runtime-session"}, set(values))
        session.get_credentials.side_effect = RuntimeError("credential error")
        self.assertEqual(set(data.values()), set(supervisor.runtime_trace_secrets(session, data)))

    def test_run_pi_filters_model_github_data_and_container_credentials(self):
        secrets = ("model-value", "github-value", "data-value", "runtime-value", "container-value")
        process = Mock()
        process.stdout = io.StringIO(json.dumps({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": " ".join(secrets)}],
        }}) + "\n")
        process.stderr = io.StringIO("")
        process.stdin = Mock()
        process.returncode = 0
        output = io.StringIO()
        with patch.object(supervisor.subprocess, "Popen", return_value=process), \
                patch.object(supervisor, "stop_process"), patch("sys.stdout", output), \
                patch.dict(os.environ, {"AWS_CONTAINER_AUTHORIZATION_TOKEN": secrets[4]}):
            supervisor.run_pi({"model": "test-model", "prompt": "test prompt"}, secrets[0], Path("/tmp/run"),
                              time.monotonic() + 10, RUN_ID, {"GH_TOKEN": secrets[1]}, secrets[2:4])
        for value in secrets:
            self.assertNotIn(value, output.getvalue())
        self.assertIn("[redacted]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
