"""Check generic GitHub capability delivery and launch failures."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError, ReadTimeoutError

from cloudbox.cli import MAX_HOOK_PAYLOAD_BYTES, Runs
from cloudbox.common import CloudboxError
from cloudbox.github import GitHubAccess, RUN_PERMISSIONS

TOKEN = "test-token-do-not-save"
PROMPT = "Read https://github.com/example/project/issues/1 and explain it."


class GitHubCLITests(unittest.TestCase):
    def setUp(self):
        self.runs = Runs.__new__(Runs)
        self.runs.deployment = {"default_model": "test/model", "image_version": "1.0", "image_arn": "image",
            "openrouter_secret_arn": "openrouter-secret", "log_group_name": "logs", "aws_region": "us-east-1",
            "memory_mib": 2048, "architecture": "arm64", "runtime_role_arn": "runtime",
            "ingress_connector_arn": "ingress"}
        self.runs.session = Mock()
        self.runs.s3 = Mock()
        self.runs.compute = Mock()
        self.runs.compute.get_microvm_image_version.return_value = {"state": "SUCCESSFUL", "status": "ACTIVE"}
        self.runs.compute.run_microvm.return_value = {"microvmId": "vm-1", "state": "RUNNING"}
        self.runs.bucket = "bucket"
        self.runs.environment = SimpleNamespace(name="test")
        self.access = GitHubAccess({"repositories": [{"id": 42, "full_name": "example/project"}],
                                   "permissions": RUN_PERMISSIONS,
                                   "git_identity": {"name": "test[bot]", "email": "123+test[bot]@users.noreply.github.com"}}, TOKEN,
                                  (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        self.credentials = {"AccessKeyId": "access", "SecretAccessKey": "secret", "SessionToken": "session",
                            "Expiration": datetime.now(timezone.utc) + timedelta(hours=1)}
        self.credentials_patch = patch("cloudbox.cli.scoped_data_credentials", return_value=self.credentials)
        self.access_patch = patch("cloudbox.cli.prepare_github_access", return_value=self.access)
        self.revoke_patch = patch("cloudbox.cli.revoke_quietly")
        self.credentials_patch.start()
        self.prepare_access = self.access_patch.start()
        self.revoke = self.revoke_patch.start()
        self.addCleanup(self.credentials_patch.stop)
        self.addCleanup(self.access_patch.stop)
        self.addCleanup(self.revoke_patch.stop)

    def submit(self):
        return self.runs.submit({"prompt": PROMPT})

    def test_github_token_only_enters_transient_hook(self):
        self.submit()
        payload = json.loads(self.runs.compute.run_microvm.call_args.kwargs["runHookPayload"])
        records = [json.loads(call.kwargs["Body"]) for call in self.runs.s3.put_object.call_args_list]
        self.assertEqual(payload["github_token"], TOKEN)
        self.assertEqual(payload["github_token_expires_at"], self.access.expires_at)
        self.assertNotIn(TOKEN, json.dumps(records))
        self.revoke.assert_not_called()

    def test_large_payload_revokes_before_launch(self):
        self.prepare_access.return_value = GitHubAccess(self.access.github, "x" * MAX_HOOK_PAYLOAD_BYTES, self.access.expires_at)
        with self.assertRaises(CloudboxError) as raised:
            self.submit()
        self.assertEqual(raised.exception.code, "payload_too_large")
        self.runs.compute.run_microvm.assert_not_called()
        self.runs.s3.put_object.assert_not_called()
        self.revoke.assert_called_once()

    def test_short_token_lifetime_revokes_before_launch(self):
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        self.prepare_access.return_value = GitHubAccess(self.access.github, TOKEN, expiry)
        with self.assertRaises(CloudboxError) as raised:
            self.submit()
        self.assertEqual(raised.exception.code, "github_token_expires_early")
        self.runs.compute.run_microvm.assert_not_called()
        self.revoke.assert_called_once_with(TOKEN)

    def test_failed_spec_save_revokes_before_launch(self):
        self.runs.s3.put_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        with self.assertRaises(ClientError):
            self.submit()
        self.runs.compute.run_microvm.assert_not_called()
        self.revoke.assert_called_once_with(TOKEN)

    def test_known_launch_rejection_revokes(self):
        self.runs.compute.run_microvm.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}, "ResponseMetadata": {"RetryAttempts": 0}}, "RunMicrovm")
        with self.assertRaises(CloudboxError) as raised:
            self.submit()
        self.assertEqual(raised.exception.code, "launch_rejected")
        self.revoke.assert_called_once_with(TOKEN)

    def test_missing_launch_response_preserves_token(self):
        self.runs.compute.run_microvm.side_effect = ReadTimeoutError(endpoint_url="https://test.invalid")
        with self.assertRaises(CloudboxError) as raised:
            self.submit()
        self.assertEqual(raised.exception.code, "launch_unknown")
        self.revoke.assert_not_called()

    def test_rejection_after_sdk_retry_preserves_token(self):
        self.runs.compute.run_microvm.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}, "ResponseMetadata": {"RetryAttempts": 1}}, "RunMicrovm")
        with self.assertRaises(CloudboxError) as raised:
            self.submit()
        self.assertEqual(raised.exception.code, "launch_unknown")
        self.revoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
