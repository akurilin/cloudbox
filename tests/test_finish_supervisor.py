"""Check finish reports, runtime failures, and supervisor-owned storage."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "cloudbox"))

import supervisor

RUN_ID = "45e3a9d8-f176-4f28-bd66-622bcd744272"
REPORT = {"status": "completed", "summary": "Calculation checked.", "result": {"answer": 42}}


def finish(events, report=REPORT, *, is_error=False, tool_id="finish-1", start=True, terminate=True):
    if start:
        events.accept({"type": "tool_execution_start", "toolName": "finish",
                       "toolCallId": tool_id, "args": report})
    events.accept({"type": "tool_execution_end", "toolName": "finish", "toolCallId": tool_id,
                   "isError": is_error, "result": {"content": [], "details": {"report": report},
                                                   "terminate": terminate}})


class FinishEventsTests(unittest.TestCase):
    def setUp(self):
        self.events = supervisor.PiEvents(RUN_ID)
        self.logger = patch.object(supervisor, "emit")
        self.logger.start()
        self.addCleanup(self.logger.stop)

    def test_finish_needs_no_final_message_or_output_file(self):
        finish(self.events)
        self.assertEqual(("succeeded", "completed"), self.events.completion())
        self.assertEqual(REPORT, self.events.report)

    def test_prose_does_not_override_an_accepted_report(self):
        finish(self.events)
        self.events.accept({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Done."}]}})
        self.assertEqual(("succeeded", "completed"), self.events.completion())

    def test_old_json_reply_cannot_complete_a_new_run(self):
        self.events.accept({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop",
            "content": [{"type": "text", "text": '{"status":"completed"}'}]}})
        self.assertEqual(("failed", "missing_finish"), self.events.completion())

    def test_failed_or_unmatched_finish_does_not_complete(self):
        for options in ({"is_error": True}, {"start": False}, {"terminate": False}):
            with self.subTest(options=options):
                events = supervisor.PiEvents(RUN_ID)
                finish(events, **options)
                self.assertEqual("failed", events.completion()[0])
                self.assertIsNone(events.report)

    def test_finish_cannot_complete_with_an_unfinished_tool(self):
        self.events.accept({"type": "tool_execution_start", "toolName": "bash",
                            "toolCallId": "push", "args": {"command": "git push"}})
        finish(self.events)
        self.assertEqual("failed", self.events.completion()[0])
        self.assertIsNone(self.events.report)

    def test_invalid_report_can_be_corrected(self):
        finish(self.events, {**REPORT, "summary": " "})
        self.assertIsNone(self.events.report)
        finish(self.events, tool_id="finish-2")
        self.assertEqual(REPORT, self.events.report)

    def test_result_requires_an_object_before_completion(self):
        for value in ('{"answer":42}', None, [42], 42, True):
            with self.subTest(value=value):
                events = supervisor.PiEvents(RUN_ID)
                finish(events, {**REPORT, "result": value})
                self.assertIsNone(events.report)
                finish(events, tool_id="finish-2")
                self.assertEqual(REPORT, events.report)

    def test_terminal_model_error_wins_over_report(self):
        finish(self.events)
        for stop_reason in ("error", "aborted", "length"):
            with self.subTest(stop_reason=stop_reason):
                self.events.final_message = {"stopReason": stop_reason}
                self.assertEqual(("failed", "agent_terminal_error"), self.events.completion())

    def test_report_is_not_truncated_like_its_log(self):
        report = {**REPORT, "result": {"text": "x" * (supervisor.MAX_TRACE_TEXT_BYTES + 1)}}
        finish(self.events, report)
        self.assertEqual(report, self.events.report)

    def test_validate_report_rejects_bad_schema_and_non_json_values(self):
        bad = [None, [], {}, {**REPORT, "status": "timed_out"}, {**REPORT, "summary": ""},
               {**REPORT, "summary": 42}, {**REPORT, "extra": True},
               {**REPORT, "result": {"value": float("nan")}}, {**REPORT, "result": {1: "numeric key"}},
               {**REPORT, "result": {"tuple": (1, 2)}}]
        for report in bad:
            with self.subTest(report=report), self.assertRaises(ValueError):
                supervisor.validate_report(report)

    def test_result_fields_accept_json_scalars_arrays_and_null(self):
        for value in (None, False, 1, "text", [1, {"ok": True}]):
            report = {**REPORT, "result": {"value": value}}
            with self.subTest(value=value):
                self.assertEqual(report, supervisor.validate_report(report))

    def test_report_size_uses_utf8(self):
        report = {**REPORT, "result": {"text": "\u754c" * (supervisor.MAX_REPORT_BYTES // 3)}}
        with self.assertRaises(ValueError):
            supervisor.validate_report(report)

    def test_report_rejects_lone_surrogates_and_excess_nesting(self):
        nested = None
        for _ in range(supervisor.MAX_REPORT_DEPTH):
            nested = [nested]
        for result in ("\ud800", {"\ud800": "value"}, nested):
            with self.subTest(result_type=type(result).__name__), self.assertRaises(ValueError):
                supervisor.validate_report({**REPORT, "result": {"value": result}})


class FinishStorageTests(unittest.TestCase):
    def run_worker(self, *, report=REPORT, exit_code=0, timed_out=False, save_error=None):
        events = supervisor.PiEvents(RUN_ID)
        with patch.object(supervisor, "emit"):
            finish(events, report)
        spec = {"schema_version": 3, "prompt": "Calculate a value.", "model": "test/model",
                "timeout_seconds": 600, "image_arn": "image", "image_version": "1.0"}
        payload = {"schema_version": 3, "run_id": RUN_ID, "bucket_name": "bucket",
                   "aws_region": "us-east-1", "log_group_name": "logs", "openrouter_secret_arn": "secret",
                   "data_credentials": {"AccessKeyId": "access", "SecretAccessKey": "secret", "SessionToken": "token"}}
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
            "s3": s3, "secretsmanager": secret, "lambda-microvms": compute}[name]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(supervisor, "WORKSPACE_ROOT", Path(directory)), \
                patch.object(supervisor.boto3, "Session", return_value=runtime), \
                patch.object(supervisor, "run_script"), \
                patch.object(supervisor, "run_pi", return_value=(exit_code, timed_out, events)), \
                patch.object(supervisor, "emit") as logger:
            supervisor.supervise("vm", payload)
        compute.terminate_microvm.assert_called_once_with(microvmIdentifier="vm")
        reports = [call.kwargs for call in s3.put_object.call_args_list
                   if call.kwargs["Key"].endswith("/result.json")]
        self.assertEqual(1, len(reports))
        saved = reports[0]
        self.assertEqual(f"runs/{RUN_ID}/result.json", saved["Key"])
        self.assertEqual("*", saved["IfNoneMatch"])
        return json.loads(saved["Body"]), logger

    def test_store_one_report_without_an_artifact_file(self):
        result, _ = self.run_worker()
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(REPORT, result["report"])
        self.assertFalse({"artifact_key", "artifact_complete"} & result.keys())

    def test_unicode_report_fits_the_client_record_limit(self):
        from cloudbox.common import MAX_RECORD_BYTES
        report = {**REPORT, "result": {"text": "\U0001f9ea" * 200_000}}
        result, _ = self.run_worker(report=report)
        self.assertEqual(report, result["report"])
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertLessEqual(len(encoded), MAX_RECORD_BYTES)

    def test_blocked_preserves_summary_and_partial_result(self):
        report = {"status": "blocked", "summary": "Repository access is missing.", "result": {"completed_steps": []}}
        result, _ = self.run_worker(report=report)
        self.assertEqual(("failed", "agent_blocked"), (result["status"], result["reason"]))
        self.assertEqual(report, result["report"])

    def test_crash_and_timeout_win_over_completed_claim(self):
        for options, status in (({"exit_code": 1}, "failed"), ({"timed_out": True}, "timed_out")):
            with self.subTest(options=options):
                result, _ = self.run_worker(**options)
                self.assertEqual(status, result["status"])
                self.assertEqual(REPORT, result["report"])

    def test_failed_save_is_logged_and_vm_still_stops(self):
        _, logger = self.run_worker(save_error=OSError("storage unavailable"))
        types = [call.args[1] for call in logger.call_args_list]
        self.assertIn("result_upload_error", types)
        self.assertNotIn("result_saved", types)


if __name__ == "__main__":
    unittest.main()
