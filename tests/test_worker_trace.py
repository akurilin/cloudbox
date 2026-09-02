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

# Load the worker after adding its runtime import paths.
import supervisor  # noqa: E402

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

    def test_reasoning_signatures_images_and_streaming_deltas_are_omitted(self):
        records, lines = self.records(
            [
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "thinking_delta",
                        "delta": "PRIVATE-DELTA",
                    },
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "PRIVATE-THOUGHT",
                                "thinkingSignature": "PRIVATE-SIGNATURE",
                            },
                            {
                                "type": "text",
                                "text": "Visible progress.",
                                "textSignature": "PRIVATE-TEXT-SIGNATURE",
                            },
                            {"type": "image", "data": "PRIVATE-IMAGE"},
                        ],
                    },
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call-1",
                    "toolName": "read",
                    "isError": False,
                    "result": {
                        "content": [
                            {"type": "image", "data": "PRIVATE-IMAGE"},
                            {"type": "text", "text": "visible output"},
                        ],
                        "details": {
                            "nested": {
                                "reasoning": "PRIVATE-DETAIL",
                                "signature": "PRIVATE-SIGNATURE",
                                "image": {"data": "PRIVATE-IMAGE"},
                                "count": 2,
                            }
                        },
                    },
                },
            ]
        )
        self.assertEqual(2, len(records))
        self.assertNotIn("PRIVATE-", "".join(lines))
        self.assertEqual("Visible progress.", records[0]["text"])
        self.assertEqual(2, records[1]["result"]["details"]["nested"]["count"])

    def test_known_credentials_are_redacted_in_nested_fields_and_output(self):
        values = [
            SECRET,
            quote(SECRET, safe=""),
            base64.b64encode(SECRET.encode()).decode(),
        ]
        records, lines = self.records(
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": SECRET,
                    "toolName": "bash",
                    "args": {
                        "command": f"echo {SECRET}",
                        "nested": [{"value": values[1]}],
                    },
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": SECRET,
                    "toolName": "bash",
                    "isError": True,
                    "result": {
                        "content": [{"type": "text", "text": " ".join(values)}],
                        "details": {"unexpected": SECRET},
                    },
                },
            ],
            [SECRET],
        )
        for value in values:
            self.assertNotIn(value, "".join(lines))
        self.assertIn("[redacted]", json.dumps(records))

    def test_redaction_precedes_truncation_and_records_stay_bounded(self):
        # The credential starts before the text limit and ends after it.
        long_secret = "s" * (supervisor.MAX_TRACE_TEXT_BYTES * 2)
        _, lines = self.records(
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call",
                    "toolName": "write",
                    "args": {
                        "path": "/tmp/file",
                        "content": long_secret + "\n" + '🧪\\"' * 20_000,
                    },
                }
            ],
            [long_secret],
        )
        self.assertEqual(1, len(lines))
        self.assertLessEqual(len(lines[0].encode()), supervisor.MAX_TRACE_RECORD_BYTES)
        self.assertIn("truncated", lines[0])
        self.assertNotIn("s" * 100, lines[0])

    def test_run_pi_filters_model_github_data_and_container_credentials(self):
        secrets = (
            "model-value",
            "github-value",
            "data-value",
            "runtime-value",
            "container-value",
        )
        process = Mock()
        process.stdout = io.StringIO(
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": " ".join(secrets)}],
                    },
                }
            )
            + "\n"
        )
        process.stderr = io.StringIO("")
        process.stdin = Mock()
        process.returncode = 0
        output = io.StringIO()
        with (
            patch.object(supervisor.subprocess, "Popen", return_value=process),
            patch.object(supervisor, "stop_process"),
            patch("sys.stdout", output),
            patch.dict(os.environ, {"AWS_CONTAINER_AUTHORIZATION_TOKEN": secrets[4]}),
        ):
            supervisor.run_pi(
                {"model": "test-model", "prompt": "test prompt"},
                secrets[0],
                Path("/tmp/run"),
                time.monotonic() + 10,
                RUN_ID,
                {"GH_TOKEN": secrets[1]},
                secrets[2:4],
            )
        for value in secrets:
            self.assertNotIn(value, output.getvalue())
        self.assertIn("[redacted]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
