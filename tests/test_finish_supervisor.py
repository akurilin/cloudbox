"""Check finish reports, runtime failures, and supervisor-owned storage."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from cloudbox.cli import Runs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "cloudbox"))

# Load the worker after adding its runtime import paths.
import supervisor  # noqa: E402

RUN_ID = "45e3a9d8-f176-4f28-bd66-622bcd744272"
REPORT = {
    "status": "completed",
    "summary": "Calculation checked.",
    "result": {"answer": 42},
}


def finish(
    events,
    report=REPORT,
    *,
    is_error=False,
    tool_id="finish-1",
    start=True,
    terminate=True,
):
    if start:
        events.accept(
            {
                "type": "tool_execution_start",
                "toolName": "finish",
                "toolCallId": tool_id,
                "args": report,
            }
        )
    events.accept(
        {
            "type": "tool_execution_end",
            "toolName": "finish",
            "toolCallId": tool_id,
            "isError": is_error,
            "result": {
                "content": [],
                "details": {"report": report},
                "terminate": terminate,
            },
        }
    )


class FinishEventsTests(unittest.TestCase):
    def setUp(self):
        self.events = supervisor.PiEvents(RUN_ID)
        self.logger = patch.object(supervisor, "emit")
        self.logger.start()
        self.addCleanup(self.logger.stop)

    def test_old_json_reply_cannot_complete_a_new_run(self):
        self.events.accept(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": '{"status":"completed"}'}],
                },
            }
        )
        self.assertEqual(("failed", "missing_finish"), self.events.completion())

    def test_failed_or_unmatched_finish_does_not_complete(self):
        for options in ({"is_error": True}, {"start": False}, {"terminate": False}):
            with self.subTest(options=options):
                events = supervisor.PiEvents(RUN_ID)
                finish(events, **options)
                self.assertEqual("failed", events.completion()[0])
                self.assertIsNone(events.report)

    def test_finish_cannot_complete_with_an_unfinished_tool(self):
        self.events.accept(
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "toolCallId": "push",
                "args": {"command": "git push"},
            }
        )
        finish(self.events)
        self.assertEqual("failed", self.events.completion()[0])
        self.assertIsNone(self.events.report)

    def test_stringified_result_can_be_corrected_before_completion(self):
        # A cloud run returned JSON text; reject it before accepting completion.
        finish(self.events, {**REPORT, "result": '{"answer":42}'})
        self.assertIsNone(self.events.report)
        finish(self.events, tool_id="finish-2")
        self.assertEqual(REPORT, self.events.report)

    def test_terminal_model_error_wins_over_report(self):
        finish(self.events)
        self.events.final_message = {"stopReason": "error"}
        self.assertEqual(("failed", "agent_terminal_error"), self.events.completion())


class FinishStorageTests(unittest.TestCase):
    def run_worker(
        self, *, report=REPORT, exit_code=0, timed_out=False, save_error=None
    ):
        events = supervisor.PiEvents(RUN_ID)
        with patch.object(supervisor, "emit"):
            finish(events, report)
        spec = {
            "schema_version": 3,
            "prompt": "Calculate a value.",
            "model": "test/model",
            "timeout_seconds": 600,
            "image_arn": "image",
            "image_version": "1.0",
        }
        payload = {
            "schema_version": 3,
            "run_id": RUN_ID,
            "bucket_name": "bucket",
            "aws_region": "us-east-1",
            "log_group_name": "logs",
            "openrouter_secret_arn": "secret",
            "data_credentials": {
                "AccessKeyId": "access",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            },
        }
        s3, runtime = Mock(), Mock()
        s3.get_object.return_value = {"Body": io.BytesIO(json.dumps(spec).encode())}

        def save(**arguments):
            if save_error and arguments["Key"].endswith("/result.json"):
                raise save_error

        s3.put_object.side_effect = save
        secret = Mock()
        secret.get_secret_value.return_value = {"SecretString": "model-key"}
        compute = Mock()
        runtime.client.side_effect = lambda name, **kwargs: {
            "s3": s3,
            "secretsmanager": secret,
            "lambda-microvms": compute,
        }[name]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(supervisor, "WORKSPACE_ROOT", Path(directory)),
            patch.object(supervisor.boto3, "Session", return_value=runtime),
            patch.object(supervisor, "run_script"),
            patch.object(
                supervisor, "run_pi", return_value=(exit_code, timed_out, events)
            ),
            patch.object(supervisor, "emit") as logger,
        ):
            supervisor.supervise("vm", payload)
        compute.terminate_microvm.assert_called_once_with(microvmIdentifier="vm")
        reports = [
            call.kwargs
            for call in s3.put_object.call_args_list
            if call.kwargs["Key"].endswith("/result.json")
        ]
        self.assertEqual(1, len(reports))
        saved = reports[0]
        self.assertEqual(f"runs/{RUN_ID}/result.json", saved["Key"])
        self.assertEqual("*", saved["IfNoneMatch"])
        return json.loads(saved["Body"]), saved["Body"], logger

    def test_maximum_report_survives_storage_and_download(self):
        # One report crosses the event, storage, and download size boundaries.
        report = {**REPORT, "result": {"text": ""}}
        overhead = len(json.dumps(report, separators=(",", ":")).encode())
        report["result"]["text"] = "x" * (supervisor.MAX_REPORT_BYTES - overhead)
        result, body, _ = self.run_worker(report=report)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(report, result["report"])
        self.assertGreater(len(body), supervisor.MAX_REPORT_BYTES)

        runs = Runs.__new__(Runs)
        runs.bucket = "bucket"
        runs.s3 = Mock()
        runs.status = Mock(return_value={"task_status": "succeeded"})

        def get_object(**arguments):
            if arguments["Key"] == f"runs/{RUN_ID}/result.json":
                return {"Body": io.BytesIO(body)}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        runs.s3.get_object.side_effect = get_object
        self.assertEqual(result, runs.record(RUN_ID, "result.json"))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "download"
            downloaded = runs.download(RUN_ID, destination)
            self.assertFalse(downloaded["incomplete"])
            self.assertEqual(body, (destination / "result.json").read_bytes())

    def test_blocked_preserves_summary_and_partial_result(self):
        report = {
            "status": "blocked",
            "summary": "Repository access is missing.",
            "result": {"completed_steps": []},
        }
        result, _, _ = self.run_worker(report=report)
        self.assertEqual(
            ("failed", "agent_blocked"), (result["status"], result["reason"])
        )
        self.assertEqual(report, result["report"])

    def test_crash_and_timeout_win_over_completed_claim(self):
        for options, status in (
            ({"exit_code": 1}, "failed"),
            ({"timed_out": True}, "timed_out"),
        ):
            with self.subTest(options=options):
                result, _, _ = self.run_worker(**options)
                self.assertEqual(status, result["status"])
                self.assertEqual(REPORT, result["report"])

    def test_failed_save_is_logged_and_vm_still_stops(self):
        _, _, logger = self.run_worker(save_error=OSError("storage unavailable"))
        types = [call.args[1] for call in logger.call_args_list]
        self.assertIn("result_upload_error", types)
        self.assertNotIn("result_saved", types)


if __name__ == "__main__":
    unittest.main()
