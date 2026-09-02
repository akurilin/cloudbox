"""Check optional GitHub configuration and secret lifecycle without AWS writes."""

from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from cloudbox.common import CloudboxError
from cloudbox import resources
from scripts import set_github_secret, setup, teardown

CONFIG = {
    "aws_account_id": "123456789012", "aws_region": "us-east-1",
    "aws_profile": "test-profile", "project_name": "cloudbox",
}
GITHUB_CONFIG = {"github_app_id": 1, "github_installation_id": 2, "github_repository_ids": [3]}
NAMES = {
    "secret_name": "cloudbox/openrouter", "github_secret_name": "cloudbox/github-app-private-key",
    "bucket_name": "cloudbox-bucket", "log_group_name": "/aws/lambda-microvms/cloudbox-worker",
    "image_arn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:cloudbox-worker",
}
PENDING_DATE = datetime(2030, 1, 1, tzinfo=timezone.utc)
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:cloudbox/github-app-private-key-abcdef"


def pending_github():
    return {"Name": NAMES["github_secret_name"], "ARN": SECRET_ARN, "DeletedDate": PENDING_DATE}


def found_secrets(github=None):
    return {
        "present": set(), "image": None, "vms": [], "objects": [], "uploads": [], "orphans": [],
        "secret": None, "secrets": {resources.SECRET_ADDRESS: None, resources.GITHUB_SECRET_ADDRESS: github},
    }


class ConfigurationTests(unittest.TestCase):
    def read(self, config):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps({"deployment": config}))
            return setup.read_config(SimpleNamespace(input_path=path))[0]["deployment"]

    def test_github_is_optional(self):
        self.assertEqual(self.read(CONFIG), CONFIG)
        self.assertEqual(self.read({**CONFIG, **GITHUB_CONFIG}), {**CONFIG, **GITHUB_CONFIG})

    def test_partial_and_invalid_github_settings_fail(self):
        invalid = [
            {"github_app_id": 1},
            {**GITHUB_CONFIG, "github_repository_ids": []},
            {**GITHUB_CONFIG, "github_repository_ids": [3, 3]},
            {**GITHUB_CONFIG, "github_repository_ids": ["3"]},
            {**GITHUB_CONFIG, "github_repository_ids": list(range(1, setup.MAX_TOKEN_REPOSITORIES + 2))},
            {**GITHUB_CONFIG, "github_app_id": True},
            {**GITHUB_CONFIG, "github_installation_id": 0},
            {**GITHUB_CONFIG, "github_private_key_secret_arn": SECRET_ARN},
            {**GITHUB_CONFIG, "github_private_key": "not-an-input"},
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(CloudboxError):
                self.read({**CONFIG, **fields})

    def test_setup_requires_key_before_first_github_apply(self):
        output = io.StringIO()
        environment = SimpleNamespace(name="test", key_path=Path(".env.test"))
        with ExitStack() as stack:
            stack.enter_context(patch.object(setup, "get_environment", return_value=environment))
            stack.enter_context(patch.object(setup.shutil, "which", return_value="tool"))
            stack.enter_context(patch.object(setup, "read_config", return_value=({"deployment": {**CONFIG, **GITHUB_CONFIG}}, b"{}")))
            stack.enter_context(patch.object(setup, "check_local_state"))
            stack.enter_context(patch.object(setup, "key_from_file", return_value="test-key"))
            stack.enter_context(patch.object(setup, "validate_key"))
            stack.enter_context(patch.object(setup, "operator_session"))
            stack.enter_context(patch.object(resources, "check_resources", return_value={
                "untracked_resources": [], "orphan_resources": [],
                "secrets": {name: "absent" for name in resources.secret_targets(NAMES).values()},
            }))
            apply = stack.enter_context(patch.object(setup, "apply_stage"))
            stack.enter_context(redirect_stdout(output))
            self.assertEqual(setup.main(["--env", "test", "--yes"]), 1)
        apply.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "github_key_required")


class SecretInventoryTests(unittest.TestCase):
    def test_inventory_checks_each_secret_identity(self):
        compute, s3, secrets, logs = Mock(), Mock(), Mock(), Mock()
        compute.get_microvm_image.return_value = {"state": "DELETED"}
        compute.list_microvms.return_value = {"items": []}
        s3.head_bucket.side_effect = ClientError({"Error": {"Code": "NotFound"}}, "HeadBucket")
        logs.get_paginator.return_value.paginate.return_value = [{"logGroups": []}]
        tags = [{"Key": "Project", "Value": CONFIG["project_name"]}, {"Key": "ManagedBy", "Value": resources.TERRAFORM_OWNER}]
        github = {**pending_github(), "Tags": tags}
        openrouter = {"Name": NAMES["secret_name"], "ARN": SECRET_ARN.replace("github-app-private-key", "openrouter"), "Tags": tags}
        values = {NAMES["secret_name"]: openrouter, NAMES["github_secret_name"]: github}
        secrets.describe_secret.side_effect = lambda **kwargs: values[kwargs["SecretId"]]
        clients = {resources.MICROVM_SERVICE: compute, "s3": s3, "secretsmanager": secrets, "iam": Mock(), "logs": logs}
        session = SimpleNamespace(client=lambda name, **kwargs: clients[name])
        expected = {"main": {address: {"kind": "secret", "identity": {"name": name}}
                             for address, name in resources.secret_targets(NAMES).items()}}
        with patch.object(resources, "orphan_resources", return_value=[]):
            found = resources.inventory(session, NAMES, CONFIG, expected)
            self.assertEqual(found["present"], {resources.SECRET_ADDRESS})
            self.assertEqual(found["secret"], openrouter)
            self.assertEqual(found["secrets"][resources.GITHUB_SECRET_ADDRESS], github)
            github["ARN"] = github["ARN"].replace(CONFIG["aws_account_id"], "999999999999")
            with self.assertRaises(CloudboxError):
                resources.inventory(session, NAMES, CONFIG, expected)

    def test_optional_declaration_has_no_inactive_instance(self):
        disabled = {**NAMES, "github_secret_name": None}
        targets = resources.secret_targets(disabled)
        self.assertEqual(targets, {resources.SECRET_ADDRESS: NAMES["secret_name"]})
        self.assertIn(resources.GITHUB_SECRET_DECLARATION,
                      resources.covered_declarations(targets, disabled, main=True))
        self.assertNotIn(resources.GITHUB_SECRET_ADDRESS, targets)

    def test_pending_github_prevents_clean_report_when_openrouter_is_absent(self):
        environment = SimpleNamespace(name="test", roots=("bootstrap", "main"))
        with patch.object(resources, "state_remains", return_value=False):
            report = resources.resource_report(
                environment, CONFIG, NAMES, {"bootstrap": {}, "main": {}}, found_secrets(pending_github()),
            )
        self.assertFalse(report["clean"])
        self.assertEqual(report["secret"], "absent")
        self.assertEqual(report["secrets"][NAMES["github_secret_name"]], "pending_deletion")

    def test_disabled_github_secret_is_an_orphan(self):
        # Discovery keeps disabled secrets outside automatic deletion targets.
        pages = {
            "list_roles": [{"Roles": []}], "list_policies": [{"Policies": []}],
            "list_buckets": [{"Buckets": []}], "describe_log_groups": [{"logGroups": []}],
            "list_secrets": [{"SecretList": [pending_github()]}],
        }
        client = Mock()
        client.get_paginator.side_effect = lambda name: SimpleNamespace(paginate=lambda **kwargs: pages[name])
        client.list_microvm_images.return_value = {"items": []}
        session = SimpleNamespace(client=lambda *args, **kwargs: client)
        disabled = {**NAMES, "github_secret_name": None}
        self.assertEqual(resources.orphan_resources(session, disabled, CONFIG, {}, {}), [SECRET_ARN])
        self.assertEqual(resources.orphan_resources(session, NAMES, CONFIG, {}, {}), [])


class SecretTeardownTests(unittest.TestCase):
    def run_teardown(self, *, force):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "input.json"
            path.write_bytes(b"{}")
            environment = SimpleNamespace(name="test", input_path=path,
                                          main_root=Path(directory) / "main", bootstrap_root=Path(directory) / "bootstrap")
            environment.roots = (environment.bootstrap_root, environment.main_root)
            empty = {root: {} for root in environment.roots}
            before = found_secrets(pending_github())
            after = found_secrets() if force else before
            stack.enter_context(patch.object(teardown, "get_environment", return_value=environment))
            stack.enter_context(patch.object(teardown, "resource_context",
                                      return_value=(CONFIG, b"{}", Mock(), NAMES, empty, empty, before)))
            stack.enter_context(patch.object(teardown, "destroy_plan", return_value=None))
            stack.enter_context(patch.object(teardown, "operator_session"))
            stack.enter_context(patch.object(teardown, "inventory", return_value=after))
            stack.enter_context(patch.object(teardown, "saved_resources", return_value={}))
            stack.enter_context(patch.object(resources, "state_remains", return_value=False))
            delete = stack.enter_context(patch.object(teardown, "force_delete_secret"))
            stack.enter_context(redirect_stdout(output))
            arguments = ["--env", "test", "--yes"] + (["--force-delete-secret"] if force else [])
            self.assertEqual(teardown.main(arguments), 0)
        return [json.loads(line) for line in output.getvalue().splitlines()][-1], delete

    def test_pending_github_remains_with_recovery(self):
        report, delete = self.run_teardown(force=False)
        delete.assert_not_called()
        self.assertTrue(report["deleted"])
        self.assertFalse(report["all_resources_absent"])
        self.assertIsNone(report["secret_deletion_date"])
        self.assertEqual(report["secret_deletion_dates"], {NAMES["github_secret_name"]: PENDING_DATE.isoformat()})

    def test_forced_teardown_removes_pending_github_when_openrouter_absent(self):
        report, delete = self.run_teardown(force=True)
        self.assertTrue(report["all_resources_absent"])
        self.assertIn(pending_github(), [call.args[1] for call in delete.call_args_list])


class KeyStorageTests(unittest.TestCase):
    def test_key_file_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.pem"
            path.write_bytes(b"x" * (set_github_secret.MAX_PRIVATE_KEY_BYTES + 1))
            with patch.object(set_github_secret, "validate_private_key") as validate:
                with self.assertRaises(CloudboxError):
                    set_github_secret.private_key_from_file(path)
                validate.assert_not_called()

    def test_existing_key_check_reads_metadata_only(self):
        client = Mock()
        client.describe_secret.return_value = {"VersionIdsToStages": {"version": [set_github_secret.CURRENT_SECRET_STAGE]}}
        session = SimpleNamespace(client=lambda *args, **kwargs: client)
        self.assertTrue(set_github_secret.secret_has_value(session, SECRET_ARN))
        client.get_secret_value.assert_not_called()

    def test_upload_uses_only_separate_key_secret(self):
        session = Mock()
        deployment = {"github_private_key_secret_arn": SECRET_ARN, "openrouter_secret_arn": "other-secret"}
        set_github_secret.store_private_key(session, deployment, "validated-test-value")
        session.client.return_value.put_secret_value.assert_called_once_with(
            SecretId=SECRET_ARN, SecretString="validated-test-value",
        )
        with self.assertRaises(CloudboxError):
            set_github_secret.store_private_key(session, {}, "validated-test-value")


if __name__ == "__main__":
    unittest.main()
