"""Check command guidance and reject invalid arguments before cloud access."""

import io
import unittest
from contextlib import redirect_stdout

from cloudbox import cli
from cloudbox.common import MAX_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, CloudboxError

RUN_ID = "11111111-1111-4111-8111-111111111111"
COMMANDS = (
    "submit",
    "exec",
    "list",
    "status",
    "logs",
    "download",
    "cancel",
    "wait",
    "links",
)


class ParserTests(unittest.TestCase):
    def help_text(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            cli.build_parser().parse_args([*arguments, "--help"])
        self.assertEqual(stopped.exception.code, 0)
        return output.getvalue()

    def test_root_help_lists_commands_and_examples(self):
        text = self.help_text()
        for command in COMMANDS:
            self.assertIn(command, text)
        self.assertIn("Examples:", text)
        self.assertIn('cloudbox --env test exec "What is 2 + 2?"', text)
        self.assertIn("cloudbox COMMAND --help", text)

    def test_command_help_needs_no_environment(self):
        for command in COMMANDS:
            with self.subTest(command=command):
                text = self.help_text(command)
                self.assertIn(f"usage: cloudbox --env ENV {command}", text)
                self.assertIn("Examples:", text)
                self.assertIn(f"cloudbox --env test {command}", text)

    def test_help_describes_prompt_sources_and_defaults(self):
        text = self.help_text("exec")
        for value in ("stdin", "JSON", "10m", "deployment", "--debug-agent"):
            self.assertIn(value, text)
        self.assertIn(f"{MIN_TIMEOUT_SECONDS} to {MAX_TIMEOUT_SECONDS}", text)
        self.assertNotIn("1h", text)

    def test_json_can_precede_or_follow_command(self):
        for arguments in (
            ["--json", "--env", "test", "list"],
            ["--env", "test", "list", "--json"],
            ["--env", "test", "--json", "wait", RUN_ID],
        ):
            with self.subTest(arguments=arguments):
                self.assertTrue(cli.build_parser().parse_args(arguments).json)
        self.assertFalse(cli.build_parser().parse_args(["--env", "test", "list"]).json)

    def test_list_limits_fail_during_argument_parsing(self):
        for value in ("0", str(cli.MAX_LIST_PAGE_SIZE + 1), "many"):
            with self.subTest(value=value):
                with self.assertRaises(CloudboxError) as failed:
                    cli.build_parser().parse_args(
                        ["--env", "test", "list", "--limit", value]
                    )
                self.assertEqual(failed.exception.code, "invalid_arguments")
                self.assertEqual(
                    failed.exception.parser.prog, "cloudbox --env ENV list"
                )
                self.assertIn(f"1 to {cli.MAX_LIST_PAGE_SIZE}", str(failed.exception))
        for value in (1, cli.MAX_LIST_PAGE_SIZE):
            arguments = cli.build_parser().parse_args(
                ["--env", "test", "list", "--limit", str(value)]
            )
            self.assertEqual(arguments.limit, value)

    def test_run_id_error_has_command_context(self):
        with self.assertRaises(CloudboxError) as failed:
            cli.build_parser().parse_args(["--env", "test", "status", "short-id"])
        self.assertEqual(failed.exception.code, "invalid_arguments")
        self.assertEqual(failed.exception.parser.prog, "cloudbox --env ENV status")
        self.assertIn("full run UUID", str(failed.exception))

    def test_option_abbreviations_are_rejected(self):
        for arguments in (
            ["--en", "test", "list"],
            ["--env", "test", "list", "--lim", "2"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(CloudboxError):
                cli.build_parser().parse_args(arguments)

    def test_selected_parser_is_available_for_input_errors(self):
        arguments = cli.build_parser().parse_args(["--env", "test", "exec"])
        self.assertEqual(arguments._parser.prog, "cloudbox --env ENV exec")


if __name__ == "__main__":
    unittest.main()
