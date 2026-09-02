"""Check GitHub capability setup without GitHub or AWS writes."""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "cloudbox"))

from github_access import child_environment, github_environment, revoke_token
from scripts.build_image import source_archive
import supervisor

TEST_TOKEN = "test-runtime-token"
TEST_RUN_ID = "45e3a9d8-f176-4f28-bd66-622bcd744272"
TEST_IDENTITY = {"name": "test-agent[bot]", "email": "42+test-agent[bot]@users.noreply.github.com"}


def access_spec():
    return {
        "schema_version": 3, "prompt": "Read https://github.com/owner/project/issues/1",
        "model": "test-model", "timeout_seconds": 600, "image_arn": "test-image", "image_version": "1",
        "github": {"repositories": [{"id": 1, "full_name": "owner/project"}],
                   "permissions": {"contents": "write", "issues": "write",
                                   "pull_requests": "write", "metadata": "read"},
                   "git_identity": TEST_IDENTITY.copy()},
    }


def access_payload():
    return {"github_token": TEST_TOKEN,
            "github_token_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}


class GitHubAccessTests(unittest.TestCase):
    def test_inherited_auth_and_git_trace_are_removed(self):
        with patch.dict(os.environ, {"GIT_TRACE": "1", "GH_TOKEN": "inherited", "GITHUB_TOKEN": "inherited"}):
            environment = child_environment()
        self.assertFalse(any(key.startswith(("GIT_", "GH_", "GITHUB_")) for key in environment))

    def test_git_helper_uses_runtime_token_without_saving_it(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            # Fake gh supplies the token to Git, with no network or key store.
            helper = workspace / "gh"
            helper.write_text('#!/bin/sh\ncat >/dev/null\nprintf "username=x-access-token\\npassword=%s\\n" "$GH_TOKEN"\n')
            helper.chmod(0o700)
            environment = child_environment()
            environment.update(github_environment(access_spec(), access_payload(), workspace, time.monotonic() + 60))
            environment["PATH"] = f"{workspace}{os.pathsep}{environment['PATH']}"
            environment["GIT_CONFIG_NOSYSTEM"] = "1"
            environment["GIT_CONFIG_GLOBAL"] = "/dev/null"
            subprocess.run(["git", "init", "--quiet", directory], check=True, capture_output=True, env=environment)
            result = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                                    cwd=workspace, env=environment, capture_output=True, text=True, check=True)
            self.assertIn(f"password={TEST_TOKEN}", result.stdout)
            self.assertNotIn(TEST_TOKEN, (workspace / ".git" / "config").read_text())
            self.assertFalse((workspace / ".gh").exists())

    def test_git_commit_uses_configured_bot_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            environment = child_environment()
            environment.update(github_environment(access_spec(), access_payload(), workspace, time.monotonic() + 60))
            environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
            (workspace / "sample.txt").write_text("sample\n")
            # Commit locally to check the real Git identity contract without a remote.
            for arguments in (("init", "--quiet"), ("add", "sample.txt"), ("commit", "--quiet", "-m", "Add sample")):
                subprocess.run(["git", *arguments], cwd=workspace, env=environment, check=True, capture_output=True)
            identity = subprocess.run(["git", "log", "-1", "--format=%an%n%ae%n%cn%n%ce"], cwd=workspace,
                                      env=environment, check=True, capture_output=True, text=True).stdout.splitlines()
            self.assertEqual([TEST_IDENTITY["name"], TEST_IDENTITY["email"]] * 2, identity)
            self.assertNotIn(TEST_TOKEN, (workspace / ".git" / "config").read_text())

    def test_revocation_is_bounded_and_errors_do_not_escape(self):
        factory = Mock()
        self.assertTrue(revoke_token(TEST_TOKEN, time.monotonic() + 60, factory))
        factory.return_value.request.assert_called_once_with("DELETE", "/installation/token")
        self.assertLessEqual(factory.call_args.kwargs["timeout"], 10)
        factory.return_value.request.side_effect = RuntimeError(TEST_TOKEN)
        self.assertFalse(revoke_token(TEST_TOKEN, time.monotonic() + 60, factory))
        factory.reset_mock()
        self.assertFalse(revoke_token(TEST_TOKEN, time.monotonic() - 1, factory))
        factory.assert_not_called()

    def test_image_archive_contains_shared_api_without_private_key(self):
        with zipfile.ZipFile(io.BytesIO(source_archive())) as archive:
            self.assertIn("github_api.py", archive.namelist())
            self.assertIn("github_access.py", archive.namelist())
            self.assertIn("finish.mjs", archive.namelist())
            self.assertFalse(any(name.endswith(".pem") or "/" in name for name in archive.namelist()))
            self.assertEqual((ROOT / "cloudbox" / "github_api.py").read_bytes(), archive.read("github_api.py"))


class SupervisorAccessTests(unittest.TestCase):
    def supervise_with_agent(self, agent):
        spec = access_spec()
        payload = {**access_payload(), "run_id": TEST_RUN_ID, "bucket_name": "test-bucket",
                   "aws_region": "test-region", "log_group_name": "test-log",
                   "openrouter_secret_arn": "test-secret", "data_credentials": {
                       "AccessKeyId": "test", "SecretAccessKey": "test", "SessionToken": "test",
                   }}
        session = Mock()
        s3 = Mock()
        s3.get_object.return_value = {"Body": io.BytesIO(json.dumps(spec).encode())}
        secret = Mock()
        secret.get_secret_value.return_value = {"SecretString": "test-model-key"}
        vm = Mock()
        session.client.side_effect = lambda name, **_kwargs: {"s3": s3, "secretsmanager": secret,
                                                            "lambda-microvms": vm}[name]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(supervisor, "WORKSPACE_ROOT", Path(directory)), \
                patch.object(supervisor.boto3, "Session", return_value=session), \
                patch.object(supervisor, "run_script"), \
                patch.object(supervisor, "run_pi", side_effect=agent), \
                patch.object(supervisor, "revoke_token", return_value=True) as revoke, \
                patch.object(supervisor, "emit"):
            supervisor.supervise("test-vm", payload)
        revoke.assert_called_once()
        self.assertEqual(TEST_TOKEN, revoke.call_args.args[0])
        saved = [json.loads(call.kwargs["Body"]) for call in s3.put_object.call_args_list
                 if call.kwargs["Key"].endswith("/result.json")]
        self.assertEqual("failed", saved[0]["status"])
        self.assertTrue(saved[0]["github_token_revoked"])
        self.assertNotIn(TEST_TOKEN, json.dumps(saved))
        vm.terminate_microvm.assert_called_once_with(microvmIdentifier="test-vm")
        return saved[0]

    def test_supervisor_revokes_token_after_agent_failure(self):
        result = self.supervise_with_agent(RuntimeError("agent failed"))
        self.assertEqual("worker_error", result["reason"])


if __name__ == "__main__":
    unittest.main()
