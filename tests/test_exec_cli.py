"""Check blocking commands, live logs, and published file access."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError, ReadTimeoutError

from cloudbox.cli import (
    AWS_TERMINATED,
    LOG_MAX_PAGES_PER_POLL,
    MAX_ARTIFACT_BYTES,
    RunLogStream,
    Runs,
    artifact_manifest,
    main,
    terminal_text,
)
from cloudbox.common import MAX_RECORD_BYTES, CloudboxError

RUN_ID = "11111111-1111-4111-8111-111111111111"
FILE_ID = "22222222-2222-4222-8222-222222222222"
FILE_NAME = "result.txt"
FILE_KEY = f"runs/{RUN_ID}/artifacts/{FILE_ID}/{FILE_NAME}"


def saved_status(*, task="succeeded", compute=AWS_TERMINATED, result=True):
    return {
        "ok": True,
        "run_id": RUN_ID,
        "task_status": task if result else "unknown",
        "compute_state": compute,
        "exists": True,
        "launch": {"microvm_id": "vm-1"},
        "result": (
            {"status": task, "report": {"summary": "Done", "response": "385"}}
            if result
            else None
        ),
    }


def artifact(contents=b"file"):
    return {
        "name": FILE_NAME,
        "key": FILE_KEY,
        "bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
        "content_type": "text/plain",
        "url": "https://old.invalid/file",
        "expires_at": "2026-01-01T00:00:00+00:00",
    }


class Clock:
    def __init__(self):
        self.seconds = 0

    def now(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.runs = Runs.__new__(Runs)
        self.runs.status = Mock()
        self.runs.session = Mock()
        self.runs.compute = Mock()
        self.runs.deployment = {"log_group_name": "test-logs"}
        self.clock = Clock()
        for target, replacement in (
            ("cloudbox.cli.time.monotonic", self.clock.now),
            ("cloudbox.cli.time.sleep", self.clock.sleep),
        ):
            patcher = patch(target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_wait_requires_result_and_vm_stop(self):
        self.runs.status.side_effect = [
            saved_status(result=False, compute="RUNNING"),
            saved_status(compute="RUNNING"),
            saved_status(),
        ]
        result = self.runs.wait(RUN_ID)
        self.assertTrue(result["ok"])
        self.assertEqual(self.runs.status.call_count, 3)
        self.assertGreater(self.clock.seconds, 0)

    def test_terminal_outcomes_are_not_success(self):
        for status in ("failed", "blocked", "timed_out", "cancelled", "unknown"):
            with self.subTest(status=status):
                self.runs.status.return_value = saved_status(task=status)
                self.assertFalse(self.runs.wait(RUN_ID)["ok"])

    def test_missing_result_after_stop_has_a_deadline(self):
        self.runs.status.return_value = saved_status(result=False)
        result = self.runs.wait(RUN_ID)
        self.assertFalse(result["ok"])
        self.assertEqual(result["wait_error"]["code"], "missing_result")
        self.assertLess(self.clock.seconds, 100)

    def test_running_vm_after_result_has_a_deadline(self):
        self.runs.status.return_value = saved_status(compute="RUNNING")
        result = self.runs.wait(RUN_ID)
        self.assertEqual(result["wait_error"]["code"], "vm_not_stopped")
        self.assertEqual(result["result"]["report"]["response"], "385")
        self.assertLess(self.clock.seconds, 100)

    def test_missing_launch_is_bounded(self):
        current = saved_status(result=False, compute="unknown")
        current["launch"] = None
        self.runs.status.return_value = current
        result = self.runs.wait(RUN_ID)
        self.assertEqual(result["wait_error"]["code"], "launch_unknown")
        self.assertLess(self.clock.seconds, 100)

    def test_unsaved_launch_uses_submission_vm_id(self):
        current = saved_status(compute="unknown")
        current["launch"] = None
        self.runs.status.return_value = current
        self.runs.compute.get_microvm.return_value = {"state": AWS_TERMINATED}
        result = self.runs.wait(RUN_ID, launch={"microvm_id": "known-vm"})
        self.assertTrue(result["ok"])
        self.runs.compute.get_microvm.assert_called_once_with(
            microvmIdentifier="known-vm"
        )

    def test_read_errors_are_bounded(self):
        self.runs.status.side_effect = ReadTimeoutError(endpoint_url="https://invalid")
        result = self.runs.wait(RUN_ID)
        self.assertEqual(result["wait_error"]["code"], "status_unavailable")
        self.assertLess(self.runs.status.call_count, 5)

    def test_recorded_deadline_bounds_running_job(self):
        current = saved_status(result=False, compute="RUNNING")
        current.update(
            timeout_seconds=60,
            submitted_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        )
        self.runs.status.return_value = current
        result = self.runs.wait(RUN_ID)
        self.assertEqual(result["wait_error"]["code"], "wait_deadline")
        self.assertLess(self.clock.seconds, 100)

    def test_debug_logs_arrive_before_completion_and_late_logs_are_read(self):
        output = io.StringIO()
        logs = self.runs.session.client.return_value
        logs.get_log_events.side_effect = [
            {
                "events": [{"message": '{"event":"tool_execution_start"}'}],
                "nextForwardToken": "first",
            },
            {"events": [], "nextForwardToken": "first"},
            {"events": [], "nextForwardToken": "first"},
            {
                "events": [{"message": '{"source":"supervisor","event":"stop"}'}],
                "nextForwardToken": "last",
            },
            {"events": [], "nextForwardToken": "last"},
            {"events": [], "nextForwardToken": "last"},
            {"events": [], "nextForwardToken": "last"},
        ]
        calls = 0

        def status(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return saved_status(result=False, compute="RUNNING")
            self.assertIn("[agent]", output.getvalue())
            return saved_status()

        self.runs.status.side_effect = status
        with redirect_stderr(output):
            result = self.runs.wait(RUN_ID, debug_agent=True, debug_supervisor=True)
        self.assertTrue(result["ok"])
        self.assertIn("[supervisor]", output.getvalue())

    def test_log_failure_does_not_mask_saved_response(self):
        self.runs.status.return_value = saved_status()
        self.runs.session.client.return_value.get_log_events.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "GetLogEvents"
        )
        with redirect_stderr(io.StringIO()):
            result = self.runs.wait(RUN_ID, debug_agent=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["report"]["response"], "385")

    def test_log_poll_cannot_starve_status_checks(self):
        logs = self.runs.session.client.return_value
        logs.get_log_events.side_effect = (
            {"events": [], "nextForwardToken": str(index)} for index in range(100)
        )
        stream = RunLogStream(self.runs, RUN_ID, agent=True, supervisor=False)
        self.assertFalse(stream.poll())
        self.assertEqual(logs.get_log_events.call_count, LOG_MAX_PAGES_PER_POLL)

    def test_debug_filter_hides_other_source(self):
        logs = self.runs.session.client.return_value
        logs.get_log_events.return_value = {
            "events": [
                {"message": '{"source":"agent","event":"message"}'},
                {"message": '{"source":"supervisor","event":"stop"}'},
            ],
        }
        output = io.StringIO()
        with redirect_stderr(output):
            RunLogStream(self.runs, RUN_ID, agent=True, supervisor=False).poll()
        self.assertIn("[agent]", output.getvalue())
        self.assertNotIn("[supervisor]", output.getvalue())


class CommandTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("cloudbox.cli.Runs")
        self.runs = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.runs.wait.return_value = saved_status()
        for target, value in (
            ("cloudbox.cli.get_environment", SimpleNamespace(name="test")),
            ("cloudbox.cli.load_deployment", {}),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        def submit(supplied, *, on_run_id):
            on_run_id(RUN_ID)
            return {"run_id": RUN_ID, "microvm_id": "vm-1"}

        self.runs.submit.side_effect = submit

    def run_command(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--env", "test", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_exec_submits_once_and_waits_with_flags(self):
        code, stdout, stderr = self.run_command(
            "exec", "Calculate", "--debug-agent", "--debug-supervisor"
        )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "385\n")
        self.assertEqual(stderr, f"Run ID: {RUN_ID}\n")
        self.runs.submit.assert_called_once()
        self.runs.wait.assert_called_once_with(
            RUN_ID,
            debug_agent=True,
            debug_supervisor=True,
            launch={"run_id": RUN_ID, "microvm_id": "vm-1"},
        )

    def test_wait_does_not_submit(self):
        self.assertEqual(self.run_command("wait", RUN_ID)[0], 0)
        self.runs.submit.assert_not_called()

    def test_failed_run_prints_response_and_exits_nonzero(self):
        self.runs.wait.return_value = {**saved_status(task="failed"), "ok": False}
        code, stdout, stderr = self.run_command("wait", RUN_ID)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "385\n")
        self.assertIn("Run failed", stderr)

    def test_json_prints_status_envelope(self):
        code, stdout, _ = self.run_command("exec", "Calculate", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["result"]["report"]["response"], "385")

    def test_summary_is_response_fallback(self):
        self.runs.wait.return_value["result"]["report"].pop("response")
        self.assertEqual(self.run_command("exec", "Calculate")[1], "Done\n")

    def test_ctrl_c_keeps_job_and_returns_run_id(self):
        self.runs.wait.side_effect = KeyboardInterrupt
        code, stdout, stderr = self.run_command("exec", "Calculate")
        self.assertEqual(code, 130)
        self.assertEqual(stdout, "")
        self.assertIn(RUN_ID, stderr)
        self.runs.cancel.assert_not_called()

    def test_unknown_launch_is_not_resubmitted(self):
        self.runs.submit.side_effect = CloudboxError(
            "launch_unknown", "Inspect this run before resubmitting.", run_id=RUN_ID
        )
        code, stdout, stderr = self.run_command("exec", "Calculate")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(RUN_ID, stderr)
        self.runs.submit.assert_called_once()
        self.runs.wait.assert_not_called()

    def test_stdin_and_spec_use_existing_input_contract(self):
        with patch("cloudbox.cli.sys.stdin", io.StringIO("Calculate from stdin")):
            self.assertEqual(self.run_command("exec", "-")[0], 0)
        self.assertEqual(
            self.runs.submit.call_args.args[0]["prompt"], "Calculate from stdin"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spec.json"
            path.write_text(json.dumps({"prompt": "Calculate from spec"}))
            self.assertEqual(self.run_command("exec", "--spec", str(path))[0], 0)
        self.assertEqual(
            self.runs.submit.call_args.args[0]["prompt"], "Calculate from spec"
        )


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.runs = Runs.__new__(Runs)
        self.runs.bucket = "test-bucket"
        self.runs.environment = SimpleNamespace(name="test")
        self.runs.deployment = {"aws_region": "us-east-1"}
        self.runs.s3 = Mock()
        self.runs.status = Mock(return_value=saved_status())

    def set_contents(self, contents, *, manifest=None):
        self.runs.status.return_value["result"]["artifacts"] = [
            artifact(contents) if manifest is None else manifest
        ]

        def get_object(**request):
            if request["Key"] == FILE_KEY:
                return {"Body": io.BytesIO(contents)}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        self.runs.s3.get_object.side_effect = get_object

    def test_streams_files_larger_than_metadata_limit(self):
        contents = b"a" * (MAX_RECORD_BYTES + 1)
        self.set_contents(contents)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "download"
            result = self.runs.download(RUN_ID, target)
            self.assertEqual(
                (target / "artifacts" / FILE_ID / FILE_NAME).read_bytes(), contents
            )
            self.assertEqual(result["files"][0]["bytes"], len(contents))

    def test_rejects_path_and_size_changes_before_download(self):
        for fields in (
            {"key": f"runs/{FILE_ID}/artifacts/{FILE_ID}/{FILE_NAME}"},
            {"key": f"runs/{RUN_ID}/artifacts/../../spec.json"},
            {"name": "../outside"},
            {"name": "file\\outside"},
            {"bytes": MAX_ARTIFACT_BYTES + 1},
            {"bytes": True},
            {"sha256": "invalid"},
        ):
            with (
                self.subTest(fields=fields),
                tempfile.TemporaryDirectory() as temporary,
            ):
                self.set_contents(b"file", manifest={**artifact(), **fields})
                target = Path(temporary) / "download"
                with self.assertRaises(CloudboxError):
                    self.runs.download(RUN_ID, target)
                self.assertFalse(target.exists())
                self.runs.s3.get_object.assert_not_called()

    def test_digest_mismatch_fails(self):
        self.set_contents(b"evil", manifest=artifact(b"file"))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CloudboxError) as raised:
                self.runs.download(RUN_ID, Path(temporary) / "download")
        self.assertEqual(raised.exception.code, "artifact_integrity_error")

    def test_size_mismatch_fails(self):
        self.set_contents(b"too long", manifest=artifact(b"file"))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CloudboxError) as raised:
                self.runs.download(RUN_ID, Path(temporary) / "download")
        self.assertEqual(raised.exception.code, "artifact_size_mismatch")

    def test_duplicate_keys_fail(self):
        with self.assertRaises(CloudboxError):
            artifact_manifest(RUN_ID, {"artifacts": [artifact(), artifact()]})

    def test_links_use_new_credentials_and_saved_keys(self):
        fresh_session, signer_session = Mock(), Mock()
        fresh_session.client.return_value.get_object.return_value = {
            "Body": io.BytesIO(json.dumps({"artifacts": [artifact()]}).encode())
        }
        signer = signer_session.client.return_value
        signer.generate_presigned_url.return_value = "https://fresh.invalid/file"
        credentials = {"Expiration": datetime.now(UTC) + timedelta(minutes=40)}
        with (
            patch("cloudbox.cli.operator_session", return_value=fresh_session) as fresh,
            patch("cloudbox.cli.scoped_data_credentials", return_value=credentials),
            patch("cloudbox.cli.credential_session", return_value=signer_session),
        ):
            result = self.runs.links(RUN_ID)
        fresh.assert_called_once_with(self.runs.deployment)
        self.assertEqual(result["artifacts"][0]["url"], "https://fresh.invalid/file")
        request = signer.generate_presigned_url.call_args.kwargs
        self.assertEqual(request["Params"]["Key"], FILE_KEY)
        self.assertLess(request["ExpiresIn"], 40 * 60)
        self.runs.s3.get_object.assert_not_called()

    def test_terminal_commands_are_removed_but_layout_remains(self):
        self.assertEqual(terminal_text("\x1b[31mred\x1b[0m\n\tfile\r\b"), "red\n\tfile")


if __name__ == "__main__":
    unittest.main()
