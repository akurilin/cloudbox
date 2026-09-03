"""Check CLI guidance without deployment access."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

from cloudbox.cli import main
from cloudbox.common import CloudboxError

RUN_ID = "11111111-1111-4111-8111-111111111111"
USAGE_EXIT = 2


class HelpTests(unittest.TestCase):
    def setUp(self):
        # Block deployment reads so help and invalid input stay local.
        patcher = patch("cloudbox.cli.load_deployment")
        self.deployment = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("cloudbox.cli.Runs")
        self.runs = patcher.start().return_value
        self.addCleanup(patcher.stop)

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = main(list(arguments))
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_no_arguments_shows_help(self):
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("usage: cloudbox", stdout)
        self.assertIn("Examples:", stdout)
        self.assertIn("--env test exec", stdout)
        self.assertEqual(stderr, "")
        self.deployment.assert_not_called()

    def test_help_needs_no_environment(self):
        for arguments in (("--help",), ("exec", "--help"), ("status", "-h")):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.invoke(*arguments)
                self.assertEqual(code, 0)
                self.assertIn("Examples:", stdout)
                self.assertEqual(stderr, "")
        self.deployment.assert_not_called()

    def test_argument_errors_show_usage_and_help_on_stderr(self):
        cases = (
            (("list",), "--env"),
            (("--env", "test"), "COMMAND"),
            (("--env", "test", "exce"), "invalid choice"),
            (("--env", "other", "list"), "invalid choice"),
            (("--env", "test", "status"), "RUN_ID"),
            (("--env", "test", "status", "bad-id"), "UUID"),
            (("--env", "test", "exec", "Hello", "--unknown"), "--unknown"),
            (("--env", "test", "exec", "Hello", "--timeout"), "--timeout"),
            (("--env", "test", "list", "--limit", "0"), "--limit"),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.invoke(*arguments)
                self.assertEqual(code, USAGE_EXIT)
                self.assertEqual(stdout, "")
                self.assertIn("usage:", stderr)
                self.assertIn("error:", stderr)
                self.assertIn(message, stderr)
                self.assertIn("--help", stderr)
        self.deployment.assert_not_called()

    def test_unknown_option_points_to_command_help(self):
        code, _, stderr = self.invoke("--env", "test", "list", "--unknown")
        self.assertEqual(code, USAGE_EXIT)
        self.assertIn("cloudbox list --help", stderr)

    def test_prompt_errors_include_command_guidance(self):
        for arguments in (
            (),
            ("",),
            ("Hello", "--spec", "job.json"),
            ("Hello", "--timeout", "soon"),
            ("Hello", "--timeout", "1"),
            ("Hello", "--model", "invalid model"),
        ):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.invoke("--env", "test", "exec", *arguments)
                self.assertEqual(code, USAGE_EXIT)
                self.assertEqual(stdout, "")
                self.assertIn("cloudbox exec --help", stderr)
        self.deployment.assert_not_called()

    def test_spec_read_errors_are_actionable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            code, stdout, stderr = self.invoke(
                "--env", "test", "submit", "--spec", str(path)
            )
        self.assertEqual(code, USAGE_EXIT)
        self.assertEqual(stdout, "")
        self.assertIn(str(path), stderr)
        self.assertIn("read", stderr)
        self.assertIn("submit --help", stderr)
        self.deployment.assert_not_called()

    def test_invalid_spec_keeps_its_json_error_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            path.write_text("{")
            code, stdout, stderr = self.invoke(
                "--env", "test", "submit", "--spec", str(path), "--json"
            )
        self.assertEqual(code, USAGE_EXIT)
        self.assertEqual(json.loads(stdout)["error"]["code"], "invalid_spec")
        self.assertEqual(stderr, "")
        self.deployment.assert_not_called()

    def test_json_argument_errors_work_before_and_after_command(self):
        for arguments in (
            ("--json",),
            ("--json", "--env", "test", "exce"),
            ("--env", "test", "exce", "--json"),
            ("--json", "--env", "test", "list", "--unknown"),
            ("--env", "test", "list", "--unknown", "--json"),
            ("--env", "test", "list", "--limit", "0", "--json"),
        ):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.invoke(*arguments)
                self.assertEqual(code, USAGE_EXIT)
                self.assertFalse(json.loads(stdout)["ok"])
                self.assertEqual(stderr, "")
        self.deployment.assert_not_called()

    def test_json_after_separator_is_a_prompt(self):
        self.deployment.side_effect = CloudboxError("test_error", "Test failure.")
        code, stdout, stderr = self.invoke("--env", "test", "exec", "--", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Test failure.", stderr)

    def test_runtime_errors_use_text_unless_json_is_requested(self):
        self.deployment.side_effect = CloudboxError("test_error", "Test failure.")
        for arguments in (("--env", "test", "list"), ("--env", "test", "submit", "Hi")):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.invoke(*arguments)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("cloudbox: error: Test failure.", stderr)
                self.assertNotIn("usage:", stderr)
                for json_arguments in (("--json", *arguments), (*arguments, "--json")):
                    code, stdout, stderr = self.invoke(*json_arguments)
                    self.assertEqual(code, 1)
                    self.assertEqual(json.loads(stdout)["error"]["code"], "test_error")
                    self.assertEqual(stderr, "")

    def test_aws_errors_identify_operation_without_request_contents(self):
        self.deployment.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "private request contents"}},
            "GetObject",
        )
        code, stdout, stderr = self.invoke("--env", "test", "list")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("GetObject", stderr)
        self.assertIn("AccessDenied", stderr)
        self.assertNotIn("private request contents", stderr)

    def test_interrupted_nonblocking_command_uses_stderr(self):
        self.deployment.side_effect = KeyboardInterrupt
        code, stdout, stderr = self.invoke("--env", "test", "list")
        self.assertEqual(code, 130)
        self.assertEqual(stdout, "")
        self.assertIn("stopped", stderr)

    def test_submit_failure_keeps_known_run_id_in_text(self):
        self.runs.submit.side_effect = CloudboxError(
            "launch_unknown", "Inspect this run before resubmitting.", run_id=RUN_ID
        )
        code, stdout, stderr = self.invoke("--env", "test", "submit", "Hi")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(RUN_ID, stderr)


if __name__ == "__main__":
    unittest.main()
