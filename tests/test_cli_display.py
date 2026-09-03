"""Check terminal output selection without cloud access."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cloudbox import cli

RUN_ID = "11111111-1111-4111-8111-111111111111"
RUN_PREFIX = RUN_ID[:8]
STATUS = {
    "ok": True,
    "run_id": RUN_ID,
    "task_status": "succeeded",
    "compute_state": "TERMINATED",
    "result": {"report": {"summary": "Done", "response": "Answer"}},
}


class DisplayTests(unittest.TestCase):
    def setUp(self):
        for target, value in (
            ("cloudbox.cli.get_environment", SimpleNamespace(name="test")),
            ("cloudbox.cli.load_deployment", {}),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("cloudbox.cli.Runs")
        self.runs = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.runs.list.return_value = {
            "ok": True,
            "runs": [STATUS],
            "next_cursor": None,
        }
        self.runs.status.return_value = STATUS
        self.runs.wait.return_value = STATUS

    def invoke(self, *arguments, terminal=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        stdout.isatty = lambda: terminal
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["--env", "test", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_terminal_list_is_human_and_pipe_preserves_json(self):
        code, output, error = self.invoke("list", terminal=True)
        self.assertEqual(code, 0)
        self.assertIn("Environment: test", output)
        self.assertIn("Done", output)
        self.assertNotIn('"result"', output)
        self.assertEqual(error, "")
        self.assertTrue(self.runs.list.call_args.kwargs["human"])
        code, output, _ = self.invoke("list")
        self.assertEqual(code, 0)
        self.assertFalse(self.runs.list.call_args.kwargs["human"])
        self.assertEqual(
            json.loads(output), {"environment": "test", **self.runs.list.return_value}
        )

    def test_explicit_modes_override_terminal_detection(self):
        for arguments in (("--human", "list"), ("list", "--human")):
            with self.subTest(arguments=arguments):
                code, output, _ = self.invoke(*arguments)
                self.assertEqual(code, 0)
                self.assertIn("Environment: test", output)
        for arguments in (("--json", "list"), ("list", "--json")):
            with self.subTest(arguments=arguments):
                code, output, _ = self.invoke(*arguments, terminal=True)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["runs"], [STATUS])

    def test_conflicting_formats_fail_before_cloud_access(self):
        for arguments in (
            ("list", "--human", "--json"),
            ("--human", "list", "--json"),
            ("--json", "list", "--human"),
        ):
            with self.subTest(arguments=arguments):
                code, output, _ = self.invoke(*arguments)
                self.assertEqual(code, cli.USAGE_EXIT)
                self.assertFalse(json.loads(output)["ok"])
        self.runs.list.assert_not_called()

    def test_invalid_cursor_is_an_input_error_before_deployment_reads(self):
        with patch("cloudbox.cli.load_deployment") as deployment:
            code, output, error = self.invoke("list", "--cursor", "old-aws-token")
        self.assertEqual(code, cli.USAGE_EXIT)
        self.assertEqual(output, "")
        self.assertIn("cloudbox list --help", error)
        deployment.assert_not_called()

    def test_wait_keeps_response_text_when_piped(self):
        self.assertEqual(self.invoke("wait", RUN_ID)[1], "Answer\n")
        self.assertEqual(self.invoke("wait", RUN_ID, "--human")[1], "Answer\n")

    def test_full_uuid_does_not_need_discovery(self):
        self.assertEqual(self.invoke("status", RUN_ID)[0], 0)
        self.runs.s3.list_objects_v2.assert_not_called()
        self.runs.status.assert_called_once_with(RUN_ID)

    def test_prefix_resolves_before_status_or_cancel(self):
        for command in ("status", "cancel"):
            with (
                self.subTest(command=command),
                patch("cloudbox.cli.resolve_run_id", return_value=RUN_ID) as resolve,
            ):
                self.runs.cancel.return_value = STATUS
                self.assertEqual(self.invoke(command, RUN_PREFIX)[0], 0)
                resolve.assert_called_once_with(
                    self.runs.s3, self.runs.bucket, RUN_PREFIX
                )
                getattr(self.runs, command).assert_called_with(RUN_ID)

    def test_ambiguous_prefix_does_not_cancel(self):
        with patch(
            "cloudbox.cli.resolve_run_id",
            side_effect=cli.CloudboxError("ambiguous_run_id", "Use a longer prefix."),
        ):
            code, _, error = self.invoke("cancel", RUN_PREFIX)
        self.assertEqual(code, 1)
        self.assertIn("longer prefix", error)
        self.runs.cancel.assert_not_called()

    def test_wait_prefix_announces_and_uses_full_id_once(self):
        with patch("cloudbox.cli.resolve_run_id", return_value=RUN_ID):
            code, output, error = self.invoke("wait", RUN_PREFIX)
        self.assertEqual(code, 0)
        self.assertEqual(output, "Answer\n")
        self.assertEqual(error, f"Run ID: {RUN_ID}\n")
        self.runs.wait.assert_called_once_with(
            RUN_ID, debug_agent=False, debug_supervisor=False, launch=None
        )

    def test_human_cursor_command_keeps_filter_and_limit(self):
        self.runs.list.return_value["next_cursor"] = "cursor-token"
        code, output, _ = self.invoke(
            "list", "--human", "--status", "failed", "--limit", "2"
        )
        self.assertEqual(code, 0)
        self.assertIn("--status failed", output)
        self.assertIn("--limit 2", output)
        self.assertIn("--cursor cursor-token", output)

    def test_logs_select_human_output(self):
        self.assertEqual(self.invoke("logs", RUN_ID, terminal=True)[0], 0)
        self.runs.logs.assert_called_with(RUN_ID, False, human=True)
        self.assertEqual(self.invoke("logs", RUN_ID)[0], 0)
        self.runs.logs.assert_called_with(RUN_ID, False, human=False)


class LogOutputTests(unittest.TestCase):
    def test_human_logs_keep_messages_and_json_keeps_envelopes(self):
        for human in (True, False):
            with self.subTest(human=human):
                runs = cli.Runs.__new__(cli.Runs)
                runs.environment = SimpleNamespace(name="test")
                runs.deployment = {"log_group_name": "logs"}
                runs.session = Mock()
                runs.session.client.return_value.get_log_events.side_effect = [
                    {
                        "events": [{"timestamp": 0, "message": "Task started"}],
                        "nextForwardToken": "end",
                    },
                    {"events": [], "nextForwardToken": "end"},
                ]
                output = io.StringIO()
                with redirect_stdout(output):
                    runs.logs(RUN_ID, False, human=human)
                if human:
                    self.assertIn("Task started", output.getvalue())
                    self.assertNotIn("end_of_stream", output.getvalue())
                else:
                    rows = [json.loads(line) for line in output.getvalue().splitlines()]
                    self.assertEqual(rows[0]["event"]["message"], "Task started")
                    self.assertTrue(rows[-1]["end_of_stream"])


if __name__ == "__main__":
    unittest.main()
