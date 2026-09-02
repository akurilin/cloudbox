"""Delete one Cloudbox deployment; keep local files and unrelated AWS resources."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import BotoCoreError, ClientError
from cloudbox.common import (
    MICROVM_SERVICE, SDK_CONFIG, CloudboxError, emit, error_record, operator_session,
)
from scripts.build_image import owned_image, wait_for_deletion
from scripts.setup import (
    BOOTSTRAP_ROOT, MAIN_ROOT, INPUT_PATH, ARN_PATTERN, REGION_PATTERN,
    command_environment, read_config, state_strings, terraform,
)

TERRAFORM_OWNER = "Terraform"
VM_STOPPED = "TERMINATED"
VM_STOPPING = "TERMINATING"
POLL_SECONDS = 5
STOP_WAIT_SECONDS = 600
SECRET_WAIT_SECONDS = 120
CONSOLE_TIMEOUT_SECONDS = 60
S3_DELETE_BATCH_SIZE = 1000
S3_MISSING = {"404", "NoSuchBucket", "NotFound"}
RESOURCE_MISSING = {"ResourceNotFoundException"}
IAM_MISSING = {"NoSuchEntity"}
BUCKET_RESOURCES = (
    "aws_s3_bucket.data", "aws_s3_bucket_public_access_block.data",
    "aws_s3_bucket_ownership_controls.data",
    "aws_s3_bucket_server_side_encryption_configuration.data",
    "aws_s3_bucket_policy.tls", "aws_s3_bucket_lifecycle_configuration.runs",
)


def optional_call(function, missing, **arguments):
    # Only documented absence codes count as missing; access errors must stop work.
    try:
        return function(**arguments)
    except ClientError as error:
        if error.response["Error"]["Code"] in missing:
            return None
        raise


def terraform_names():
    # Evaluate the existing naming rules even after destroy removes root outputs.
    result = subprocess.run(
        ["terraform", f"-chdir={BOOTSTRAP_ROOT}", "console", "-no-color", f"-var-file={INPUT_PATH}"],
        input="jsonencode(module.policy.names)\n", capture_output=True, text=True,
        env=command_environment(), timeout=CONSOLE_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise CloudboxError("names_unavailable", "Terraform could not resolve the deployment names.")
    return json.loads(json.loads(result.stdout))


def expected_resources(names):
    # Check exact identities, not just a project prefix, before Terraform can delete.
    main = {address: {"bucket": names["bucket_name"]} for address in BUCKET_RESOURCES}
    main["aws_cloudwatch_log_group.worker"] = {"name": names["log_group_name"]}
    main["aws_secretsmanager_secret.openrouter"] = {"name": names["secret_name"]}
    provisioner_name = names["provisioner_role_arn"].rsplit("/", 1)[1]
    provisioner_policy = names["provisioner_role_arn"].replace(":role/", ":policy/", 1)
    bootstrap = {
        "aws_iam_role.provisioner": {"arn": names["provisioner_role_arn"]},
        "aws_iam_policy.provisioner": {"arn": provisioner_policy},
        "aws_iam_role_policy_attachment.provisioner": {
            "role": provisioner_name, "policy_arn": provisioner_policy,
        },
    }
    for key, name in names["role_names"].items():
        suffix = f"[{json.dumps(key)}]"
        main[f"aws_iam_role.worker{suffix}"] = {"name": name}
        main[f"aws_iam_role_policy.worker{suffix}"] = {"role": name, "name": "cloudbox-access"}
        bootstrap[f"aws_iam_policy.worker_boundary{suffix}"] = {"arn": names["boundary_arns"][key]}
    return {MAIN_ROOT: main, BOOTSTRAP_ROOT: bootstrap}


def check_identity(address, values, expected, config):
    if address not in expected or any(values.get(key) != value for key, value in expected[address].items()):
        raise CloudboxError("target_mismatch", "A Terraform resource is outside this deployment.", resource=address)
    for value in state_strings(values):
        for _, region, account in ARN_PATTERN.findall(value):
            if (account.isdigit() and account != config["aws_account_id"]) or (
                REGION_PATTERN.fullmatch(region) and region != config["aws_region"]
            ):
                raise CloudboxError("state_mismatch", "A saved resource belongs to another account or region.")


def saved_resources(directory, expected, config):
    if os.environ.get("TF_WORKSPACE", "default") != "default" or os.environ.get("TF_DATA_DIR"):
        raise CloudboxError("unsupported_state", "Teardown requires default workspaces and local state.")
    workspace = directory / ".terraform" / "environment"
    if workspace.exists() and workspace.read_text().strip() != "default":
        raise CloudboxError("unsupported_state", "Select the default Terraform workspace first.")
    path = directory / "terraform.tfstate"
    state = json.loads(path.read_bytes()) if path.exists() else {}
    outputs = state.get("outputs", {})
    deployment = outputs.get("cloudbox", {}).get("value", {})
    for field in ("aws_account_id", "aws_region", "project_name"):
        if field in deployment and deployment[field] != config[field]:
            raise CloudboxError("state_mismatch", "Saved outputs belong to another deployment.")
    role = outputs.get("provisioner_role_arn", {}).get("value")
    if role and role != expected.get("aws_iam_role.provisioner", {}).get("arn"):
        raise CloudboxError("state_mismatch", "The saved provisioner output belongs to another deployment.")
    resources = {}
    for resource in state.get("resources", []):
        address = f"{resource['type']}.{resource['name']}"
        if resource.get("module") or resource.get("mode") != "managed":
            raise CloudboxError("unexpected_state", "Review the unexpected Terraform state before teardown.")
        for instance in resource["instances"]:
            indexed = address + (f"[{json.dumps(instance['index_key'])}]" if "index_key" in instance else "")
            values = instance["attributes"]
            check_identity(indexed, values, expected, config)
            resources[indexed] = values
    return resources


def state_remains(directory):
    path = directory / "terraform.tfstate"
    state = json.loads(path.read_bytes()) if path.exists() else {}
    return bool(state.get("resources") or state.get("outputs"))


def destroy_plan(directory, path, expected, config):
    resources = saved_resources(directory, expected, config)
    if not state_remains(directory):
        return None
    # Clear stale outputs without cloud reads when all tracked resources are gone.
    refresh = [] if resources else ["-refresh=false"]
    terraform(directory, "plan", "-destroy", "-input=false", "-no-color",
              f"-var-file={INPUT_PATH}", f"-out={path}", *refresh)
    plan = json.loads(terraform(directory, "show", "-json", str(path), capture=True))
    for change in plan.get("resource_changes", []):
        if change["change"]["actions"] not in (["delete"], ["no-op"]):
            raise CloudboxError("unexpected_plan", "Teardown accepts deletion plans only.")
        check_identity(change["address"], change["change"]["before"], expected, config)
    return path


def check_tags(tags, config):
    values = {tag["Key"]: tag["Value"] for tag in tags}
    if values.get("Project") != config["project_name"] or values.get("ManagedBy") != TERRAFORM_OWNER:
        raise CloudboxError("owner_mismatch", "An AWS resource lacks this deployment's ownership tags.")


def list_vms(compute, names):
    items, request = [], {"imageIdentifier": names["image_arn"]}
    while True:
        page = optional_call(compute.list_microvms, RESOURCE_MISSING, **request)
        if page is None:
            return items
        items.extend(page.get("items", []))
        if not page.get("nextToken"):
            return items
        request["nextToken"] = page["nextToken"]


def inventory(session, names, config):
    compute = session.client(MICROVM_SERVICE, config=SDK_CONFIG)
    image = owned_image(compute, names["image_arn"])
    if image and image.get("tags", {}).get("Project") != config["project_name"]:
        raise CloudboxError("image_owner_mismatch", "The image belongs to another project.")
    present = set()
    s3 = session.client("s3", config=SDK_CONFIG)
    bucket = {"Bucket": names["bucket_name"], "ExpectedBucketOwner": config["aws_account_id"]}
    objects, uploads = [], []
    if optional_call(s3.head_bucket, S3_MISSING, **bucket) is not None:
        present.add("aws_s3_bucket.data")
        check_tags(s3.get_bucket_tagging(**bucket)["TagSet"], config)
        if s3.get_bucket_versioning(**bucket).get("Status"):
            raise CloudboxError("versioned_bucket", "Review versioned bucket cleanup separately.")
        for page in s3.get_paginator("list_objects_v2").paginate(**bucket):
            objects.extend({"Key": item["Key"]} for item in page.get("Contents", []))
        for page in s3.get_paginator("list_multipart_uploads").paginate(**bucket):
            uploads.extend(page.get("Uploads", []))
    secret = optional_call(session.client("secretsmanager", config=SDK_CONFIG).describe_secret,
                           RESOURCE_MISSING, SecretId=names["secret_name"])
    if secret:
        check_tags(secret.get("Tags", []), config)
        prefix = f"arn:aws:secretsmanager:{config['aws_region']}:{config['aws_account_id']}:secret:{names['secret_name']}-"
        if secret["Name"] != names["secret_name"] or not secret["ARN"].startswith(prefix):
            raise CloudboxError("secret_mismatch", "The secret identity does not match this deployment.")
        if not secret.get("DeletedDate"):
            present.add("aws_secretsmanager_secret.openrouter")
    iam = session.client("iam", config=SDK_CONFIG)
    expected = expected_resources(names)
    for targets in expected.values():
        for address, fields in targets.items():
            if address.startswith("aws_iam_role."):
                name = fields.get("name") or fields["arn"].rsplit("/", 1)[1]
                if optional_call(iam.get_role, IAM_MISSING, RoleName=name) is not None:
                    present.add(address)
            elif address.startswith("aws_iam_policy."):
                if optional_call(iam.get_policy, IAM_MISSING, PolicyArn=fields["arn"]) is not None:
                    present.add(address)
    logs = session.client("logs", config=SDK_CONFIG)
    for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=names["log_group_name"]):
        if any(group["logGroupName"] == names["log_group_name"] for group in page.get("logGroups", [])):
            present.add("aws_cloudwatch_log_group.worker")
    return {"present": present, "image": image, "vms": list_vms(compute, names),
            "objects": objects, "uploads": uploads, "secret": secret}


def remove_compute(session, names):
    compute = session.client(MICROVM_SERVICE, config=SDK_CONFIG)
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    for vm in list_vms(compute, names):
        if vm["state"] not in {VM_STOPPED, VM_STOPPING}:
            compute.terminate_microvm(microvmIdentifier=vm["microvmId"])
    while any(vm["state"] != VM_STOPPED for vm in list_vms(compute, names)):
        if time.monotonic() >= deadline:
            raise CloudboxError("vms_still_running", "VMs have not stopped. Run teardown again after they stop.")
        time.sleep(POLL_SECONDS)
    image = owned_image(compute, names["image_arn"])
    if image:
        if image["state"] != "DELETING":
            compute.delete_microvm_image(imageIdentifier=names["image_arn"])
        wait_for_deletion(compute, names["image_arn"])


def empty_bucket(session, names, config):
    # Compute is gone. Read the final object set, including late worker uploads.
    s3 = session.client("s3", config=SDK_CONFIG)
    bucket = {"Bucket": names["bucket_name"], "ExpectedBucketOwner": config["aws_account_id"]}
    if optional_call(s3.head_bucket, S3_MISSING, **bucket) is None:
        return
    check_tags(s3.get_bucket_tagging(**bucket)["TagSet"], config)
    if s3.get_bucket_versioning(**bucket).get("Status"):
        raise CloudboxError("versioned_bucket", "Review versioned bucket cleanup separately.")
    for page in s3.get_paginator("list_multipart_uploads").paginate(**bucket):
        for upload in page.get("Uploads", []):
            s3.abort_multipart_upload(**bucket, Key=upload["Key"], UploadId=upload["UploadId"])
    for page in s3.get_paginator("list_objects_v2").paginate(**bucket):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        for offset in range(0, len(objects), S3_DELETE_BATCH_SIZE):
            response = s3.delete_objects(**bucket, Delete={"Objects": objects[offset:offset + S3_DELETE_BATCH_SIZE], "Quiet": True})
            if response.get("Errors"):
                raise CloudboxError("objects_remain", "AWS did not delete all objects. Run teardown again.")


def force_delete_secret(session, secret):
    if secret is None:
        return
    secrets = session.client("secretsmanager", config=SDK_CONFIG)
    secrets.delete_secret(SecretId=secret["ARN"], ForceDeleteWithoutRecovery=True)
    deadline = time.monotonic() + SECRET_WAIT_SECONDS
    while optional_call(secrets.describe_secret, RESOURCE_MISSING, SecretId=secret["ARN"]) is not None:
        if time.monotonic() >= deadline:
            raise CloudboxError("secret_delete_pending", "AWS has not removed the secret yet. Run teardown again.")
        time.sleep(POLL_SECONDS)


def main():
    stage = "preflight"
    try:
        parser = argparse.ArgumentParser(description="Delete this Cloudbox deployment, not local files.")
        parser.add_argument("--plan", action="store_true", help="Show targets and Terraform plans; make no AWS changes.")
        parser.add_argument("--yes", action="store_true", help="Approve deletion of run data, logs, image, and infrastructure.")
        parser.add_argument("--force-delete-secret", action="store_true", help="Also delete the secret without recovery, for a clean rebuild.")
        arguments = parser.parse_args()
        if shutil.which("terraform") is None:
            raise CloudboxError("missing_tool", "Install Terraform before teardown.")
        document, original = read_config()
        config = document["deployment"]
        admin = operator_session(config, provisioner=False)
        for directory in (BOOTSTRAP_ROOT, MAIN_ROOT):
            terraform(directory, "init", "-input=false", "-no-color")
        names = terraform_names()
        expected = expected_resources(names)
        saved = {directory: saved_resources(directory, targets, config) for directory, targets in expected.items()}
        found = inventory(admin, names, config)
        tracked = set().union(*saved.values())
        if found["present"] - tracked:
            raise CloudboxError("untracked_resources", "Restore the matching state before teardown.", resources=sorted(found["present"] - tracked))
        state_secret = saved[MAIN_ROOT].get("aws_secretsmanager_secret.openrouter")
        if state_secret and found["secret"] and state_secret["arn"] != found["secret"]["ARN"]:
            raise CloudboxError("secret_mismatch", "The saved and live secret ARNs differ.")
        with tempfile.TemporaryDirectory(prefix="cloudbox-teardown-") as temporary:
            plans = {directory: destroy_plan(directory, Path(temporary) / f"{directory.name}.tfplan", targets, config)
                     for directory, targets in expected.items()}
            summary = {"account_id": config["aws_account_id"], "region": config["aws_region"],
                       "project": config["project_name"], "tracked_resources": len(tracked),
                       "image": names["image_arn"] if found["image"] else None,
                       "active_vms": sum(vm["state"] != VM_STOPPED for vm in found["vms"]),
                       "bucket": names["bucket_name"], "objects": len(found["objects"]),
                       "multipart_uploads": len(found["uploads"]),
                       "secret_deletion": "without_recovery" if arguments.force_delete_secret else "seven_day_recovery"}
            emit({"ok": True, "plan_only": True, **summary})
            if arguments.plan:
                return 0
            print("Deletes cloud run data, logs, image, and infrastructure. Local files stay.\n"
                  "Do not run setup or submit jobs during teardown.", file=sys.stderr)
            if not arguments.yes and input(f"Type {config['project_name']} to approve: ").strip() != config["project_name"]:
                raise CloudboxError("not_approved", "Teardown was not approved.")

            def guard():
                if INPUT_PATH.read_bytes() != original:
                    raise CloudboxError("config_changed", "The input file changed. Run teardown again.")
                operator_session(config, provisioner=False)

            deployment = {**config, **names}
            stage = "compute_and_data"
            guard()
            if found["image"] or found["objects"] or found["uploads"] or summary["active_vms"]:
                session = operator_session(deployment)
                print("Teardown: stop VMs, delete image, empty bucket", file=sys.stderr, flush=True)
                remove_compute(session, names)
                empty_bucket(session, names, config)
            stage = "infrastructure"
            guard()
            if plans[MAIN_ROOT]:
                terraform(MAIN_ROOT, "apply", "-input=false", "-no-color", str(plans[MAIN_ROOT]))
            stage = "secret"
            guard()
            if arguments.force_delete_secret and found["secret"]:
                # With no main resources in state, only a checked scheduled secret can remain.
                session = operator_session(deployment) if saved[MAIN_ROOT] else admin
                force_delete_secret(session, found["secret"])
            stage = "bootstrap"
            guard()
            if plans[BOOTSTRAP_ROOT]:
                terraform(BOOTSTRAP_ROOT, "apply", "-input=false", "-no-color", str(plans[BOOTSTRAP_ROOT]))
        stage = "verify"
        remaining = inventory(admin, names, config)
        if any(state_remains(directory) for directory in expected) or (
            remaining["present"] or remaining["image"] or any(vm["state"] != VM_STOPPED for vm in remaining["vms"])
        ):
            raise CloudboxError("resources_remain", "Some Cloudbox resources remain. Inspect and run teardown again.")
        pending = remaining["secret"]
        if pending and (arguments.force_delete_secret or not pending.get("DeletedDate")):
            raise CloudboxError("secret_remains", "The secret is not removed or scheduled for deletion.")
        emit({"ok": True, "deleted": True, "all_resources_absent": pending is None,
              "secret": "pending_deletion" if pending else "absent",
              "secret_deletion_date": pending["DeletedDate"].isoformat() if pending else None,
              "local_files_retained": True})
        return 0
    except (CloudboxError, BotoCoreError, ClientError, OSError, ValueError, KeyError, TypeError,
            EOFError, subprocess.SubprocessError) as error:
        emit({**error_record(error), "stage": stage, "deleted": False})
        return 1
    except KeyboardInterrupt:
        emit({"ok": False, "deleted": False, "stage": stage, "error": {"code": "interrupted"}})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
