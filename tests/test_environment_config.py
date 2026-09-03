"""Check local environment selection and lifecycle account guards."""

import argparse
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cloudbox import cli, environments
from cloudbox.common import CloudboxError
from scripts import e2e_cloud, smoke_cloud

TEST_ACCOUNT = "111111111111"
PROD_ACCOUNT = "222222222222"
REPO_ROOT = Path(__file__).resolve().parents[1]


class EnvironmentConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.enterContext(patch.object(environments, "ROOT", self.root))
        self.enterContext(patch.object(e2e_cloud, "ROOT", self.root))
        # Stop unexpected AWS access, Terraform commands, or lifecycle stages.
        self.aws = self.enterContext(
            patch(
                "cloudbox.common.boto3.Session",
                side_effect=AssertionError("Unexpected AWS access"),
            )
        )
        self.process = self.enterContext(
            patch("subprocess.run", side_effect=AssertionError("Unexpected command"))
        )
        self.stages = self.enterContext(
            patch.object(
                e2e_cloud,
                "run_stage",
                side_effect=AssertionError("Unexpected cloud stage"),
            )
        )

    def tearDown(self):
        self.aws.assert_not_called()
        self.stages.assert_not_called()

    def write_config(self, name, account=TEST_ACCOUNT):
        config = {
            "aws_account_id": account,
            "aws_region": "us-east-1",
            "aws_profile": "local-profile",
            "project_name": "cloudbox",
        }
        path = self.root / "infra" / "environments" / f"{name}.tfvars.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"deployment": config}), encoding="utf-8")
        return path

    def write_prod_state(self, stage, state):
        path = (
            self.root
            / ".cloudbox"
            / "environments"
            / "prod"
            / stage
            / "terraform.tfstate"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def test_selection_accepts_test_and_prod_without_input_files(self):
        for name in ("test", "prod"):
            with self.subTest(name=name):
                environment = environments.get_environment(name)
                self.assertEqual(
                    environment.input_path,
                    self.root / "infra" / "environments" / f"{name}.tfvars.json",
                )
                self.assertEqual(environment.key_path, self.root / f".env.{name}")
                self.assertEqual(
                    environment.state_path(environment.main_root),
                    self.root
                    / ".cloudbox"
                    / "environments"
                    / name
                    / "main"
                    / "terraform.tfstate",
                )

    def test_selection_rejects_other_names_even_with_input_files(self):
        for name in ("legacy", "example", "sandbox"):
            with self.subTest(name=name):
                self.write_config(name)
                with self.assertRaises(CloudboxError) as failure:
                    environments.get_environment(name)
                self.assertEqual(failure.exception.code, "invalid_environment")

    def test_parser_accepts_only_test_and_prod(self):
        parser = argparse.ArgumentParser()
        environments.add_environment_argument(parser)
        for name in ("test", "prod"):
            self.assertEqual(parser.parse_args(["--env", name]).env, name)
        for name in ("legacy", "example", "sandbox"):
            with (
                self.subTest(name=name),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(["--env", name])

    def test_lifecycle_accepts_missing_prod_without_state(self):
        self.write_config("test")
        self.assertEqual(
            e2e_cloud.test_configuration(environments.get_environment("test"))[
                "aws_account_id"
            ],
            TEST_ACCOUNT,
        )

    def test_lifecycle_accepts_missing_prod_with_empty_state(self):
        self.write_config("test")
        for stage in ("bootstrap", "main"):
            self.write_prod_state(stage, {"resources": [], "outputs": {}})
        self.assertEqual(
            e2e_cloud.test_configuration(environments.get_environment("test"))[
                "aws_account_id"
            ],
            TEST_ACCOUNT,
        )

    def test_lifecycle_rejects_missing_prod_with_saved_resources_or_outputs(self):
        self.write_config("test")
        for stage in ("bootstrap", "main"):
            for state in ({"resources": [{}]}, {"outputs": {"cloudbox": {}}}):
                with self.subTest(stage=stage, state=state):
                    path = self.write_prod_state(stage, state)
                    with self.assertRaises(CloudboxError) as failure:
                        e2e_cloud.test_configuration(
                            environments.get_environment("test")
                        )
                    self.assertEqual(failure.exception.code, "prod_config_missing")
                    path.unlink()

    def test_lifecycle_rejects_missing_prod_with_invalid_state(self):
        self.write_config("test")
        self.write_prod_state("main", [])
        with self.assertRaises(CloudboxError) as failure:
            e2e_cloud.test_configuration(environments.get_environment("test"))
        self.assertEqual(failure.exception.code, "invalid_state")

    def test_lifecycle_validates_prod_input(self):
        self.write_config("test")
        for account in (int(TEST_ACCOUNT), "123", "REPLACE-WITH-YOUR-ACCOUNT-ID"):
            with self.subTest(account=account):
                self.write_config("prod", account)
                with self.assertRaises(CloudboxError) as failure:
                    e2e_cloud.test_configuration(environments.get_environment("test"))
                self.assertEqual(failure.exception.code, "invalid_config")
        for raw in ("{", '{"deployment": {}}'):
            with self.subTest(raw=raw):
                self.write_config("prod").write_text(raw, encoding="utf-8")
                with self.assertRaises((CloudboxError, ValueError)):
                    e2e_cloud.test_configuration(environments.get_environment("test"))

    def test_lifecycle_rejects_shared_accounts(self):
        self.write_config("test")
        self.write_config("prod")
        with self.assertRaises(CloudboxError) as failure:
            e2e_cloud.test_configuration(environments.get_environment("test"))
        self.assertEqual(failure.exception.code, "test_account_shared")

    def test_lifecycle_accepts_different_accounts(self):
        self.write_config("test")
        self.write_config("prod", PROD_ACCOUNT)
        self.assertEqual(
            e2e_cloud.test_configuration(environments.get_environment("test"))[
                "aws_account_id"
            ],
            TEST_ACCOUNT,
        )

    def test_missing_test_input_returns_json_from_lifecycle(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(e2e_cloud.main([]), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "preflight")
        self.assertEqual(result["error"]["code"], "FileNotFoundError")
        self.process.assert_not_called()

    def test_missing_test_input_returns_json_from_smoke(self):
        # Run the CLI in-process; empty local state must stop before AWS access.
        def local_cli(command, **kwargs):
            self.assertEqual(command[1:3], ["-m", "cloudbox"])
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(command[3:])
            return subprocess.CompletedProcess(
                command, code, stdout=output.getvalue(), stderr=""
            )

        output = io.StringIO()
        self.process.side_effect = local_cli
        with redirect_stdout(output):
            self.assertEqual(smoke_cloud.main(["--env", "test"]), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "cli_failed")
        self.assertEqual(result["cli_error"], "terraform_not_initialized")
        self.process.assert_called_once()


class LocalInputIgnoreTests(unittest.TestCase):
    def test_old_input_and_default_key_files_stay_ignored(self):
        paths = ("infra/cloudbox.auto.tfvars.json", ".env.test", ".env.prod")
        for path in paths:
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", path],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
