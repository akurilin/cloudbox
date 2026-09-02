"""Delete one Cloudbox environment; keep local files and unrelated resources."""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import BotoCoreError, ClientError

from cloudbox.common import (
    MICROVM_SERVICE,
    SDK_CONFIG,
    CloudboxError,
    emit,
    error_record,
    operator_session,
)
from cloudbox.environments import add_environment_argument, get_environment
from cloudbox.resources import (
    IMAGE_OWNER,
    RESOURCE_MISSING,
    S3_MISSING,
    VM_STOPPED,
    check_identity,
    check_plan_coverage,
    check_tags,
    inventory,
    list_vms,
    optional_call,
    resource_context,
    resource_report,
    saved_resources,
    state_remains,
)
from scripts.build_image import (
    delete_image_once,
    wait_for_deletion,
    wait_until_deletable,
)

VM_STOPPING = "TERMINATING"
POLL_SECONDS = 5
STOP_WAIT_SECONDS = 600
SECRET_WAIT_SECONDS = 120
S3_DELETE_BATCH_SIZE = 1000


def destroy_plan(environment, directory, path, expected, config):
    resources = saved_resources(environment, directory, expected, config)
    if not state_remains(environment, directory):
        return None
    # Clear stale outputs without cloud reads when all tracked resources are gone.
    refresh = [] if resources else ["-refresh=false"]
    environment.terraform(
        directory,
        "plan",
        "-destroy",
        "-input=false",
        "-no-color",
        f"-out={path}",
        *refresh,
    )
    plan = json.loads(
        environment.terraform(directory, "show", "-json", str(path), capture=True)
    )
    check_plan_coverage(directory, plan, environment)
    for change in plan.get("resource_changes", []):
        if change["change"]["actions"] not in (["delete"], ["no-op"]):
            raise CloudboxError(
                "unexpected_plan", "Teardown accepts deletion plans only."
            )
        check_identity(change["address"], change["change"]["before"], expected, config)
    return path


def remove_compute(session, names, config):
    compute = session.client(MICROVM_SERVICE, config=SDK_CONFIG)
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    for vm in list_vms(compute, names):
        if vm["state"] not in {VM_STOPPED, VM_STOPPING}:
            compute.terminate_microvm(microvmIdentifier=vm["microvmId"])
    while any(vm["state"] != VM_STOPPED for vm in list_vms(compute, names)):
        if time.monotonic() >= deadline:
            raise CloudboxError(
                "vms_still_running",
                "VMs have not stopped. Run teardown again after they stop.",
            )
        time.sleep(POLL_SECONDS)
    image = wait_until_deletable(compute, names["image_arn"], wait=True)
    if image:
        check_tags(image.get("tags", {}), config, owner=IMAGE_OWNER)
        if image["state"] != "DELETING":
            delete_image_once(session, names["image_arn"])
        wait_for_deletion(compute, names["image_arn"])


def empty_bucket(session, names, config):
    # Compute is gone. Read the final object set, including late worker uploads.
    s3 = session.client("s3", config=SDK_CONFIG)
    bucket = {
        "Bucket": names["bucket_name"],
        "ExpectedBucketOwner": config["aws_account_id"],
    }
    if optional_call(s3.head_bucket, S3_MISSING, **bucket) is None:
        return
    check_tags(s3.get_bucket_tagging(**bucket)["TagSet"], config)
    if s3.get_bucket_versioning(**bucket).get("Status"):
        raise CloudboxError(
            "versioned_bucket", "Review versioned bucket cleanup separately."
        )
    for page in s3.get_paginator("list_multipart_uploads").paginate(**bucket):
        for upload in page.get("Uploads", []):
            s3.abort_multipart_upload(
                **bucket, Key=upload["Key"], UploadId=upload["UploadId"]
            )
    for page in s3.get_paginator("list_objects_v2").paginate(**bucket):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        for offset in range(0, len(objects), S3_DELETE_BATCH_SIZE):
            response = s3.delete_objects(
                **bucket,
                Delete={
                    "Objects": objects[offset : offset + S3_DELETE_BATCH_SIZE],
                    "Quiet": True,
                },
            )
            if response.get("Errors"):
                raise CloudboxError(
                    "objects_remain",
                    "AWS did not delete all objects. Run teardown again.",
                )


def force_delete_secret(session, secret):
    if secret is None:
        return
    secrets = session.client("secretsmanager", config=SDK_CONFIG)
    secrets.delete_secret(SecretId=secret["ARN"], ForceDeleteWithoutRecovery=True)
    deadline = time.monotonic() + SECRET_WAIT_SECONDS
    while (
        optional_call(secrets.describe_secret, RESOURCE_MISSING, SecretId=secret["ARN"])
        is not None
    ):
        if time.monotonic() >= deadline:
            raise CloudboxError(
                "secret_delete_pending",
                "AWS has not removed the secret yet. Run teardown again.",
            )
        time.sleep(POLL_SECONDS)


def main(argv=None):
    stage = "preflight"
    try:
        parser = argparse.ArgumentParser(
            description="Delete one Cloudbox environment, not local files."
        )
        add_environment_argument(parser)
        parser.add_argument(
            "--plan",
            action="store_true",
            help="Show targets and Terraform plans; make no AWS changes.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Approve deletion of run data, logs, image, and infrastructure.",
        )
        parser.add_argument(
            "--force-delete-secret",
            action="store_true",
            help="Delete configured secrets without recovery, for a clean rebuild.",
        )
        arguments = parser.parse_args(argv)
        environment = get_environment(arguments.env)
        config, original, admin, names, expected, saved, found = resource_context(
            environment
        )
        report = resource_report(environment, config, names, saved, found)
        if report["untracked_resources"] or report["orphan_resources"]:
            raise CloudboxError(
                "untracked_resources",
                "Restore matching state or review extra resources before teardown.",
                inventory=report,
            )
        main_root, bootstrap_root = environment.main_root, environment.bootstrap_root
        with tempfile.TemporaryDirectory(prefix="cloudbox-teardown-") as temporary:
            plans = {
                directory: destroy_plan(
                    environment,
                    directory,
                    Path(temporary) / f"{directory.name}.tfplan",
                    targets,
                    config,
                )
                for directory, targets in expected.items()
            }
            emit(
                {
                    **report,
                    "plan_only": True,
                    "secret_deletion": "without_recovery"
                    if arguments.force_delete_secret
                    else "seven_day_recovery",
                }
            )
            if arguments.plan:
                return 0
            print(
                "Deletes cloud run data, logs, image, and infrastructure. Local files stay.\n"
                "Do not run setup or submit jobs during teardown.",
                file=sys.stderr,
            )
            confirmation = f"{environment.name}/{config['project_name']}"
            if not arguments.yes:
                # Keep approval text out of the JSON result stream.
                print(
                    f"Type {confirmation} to approve: ",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
                if input().strip() != confirmation:
                    raise CloudboxError("not_approved", "Teardown was not approved.")

            def guard():
                if environment.input_path.read_bytes() != original:
                    raise CloudboxError(
                        "config_changed", "The input file changed. Run teardown again."
                    )
                operator_session(config, provisioner=False)

            deployment = {**config, **names}
            stage = "compute_and_data"
            guard()
            if (
                found["image"]
                or found["objects"]
                or found["uploads"]
                or report["active_vms"]
            ):
                session = operator_session(deployment)
                print(
                    "Teardown: stop VMs, delete image, empty bucket",
                    file=sys.stderr,
                    flush=True,
                )
                remove_compute(session, names, config)
                empty_bucket(session, names, config)
            stage = "infrastructure"
            guard()
            if plans[main_root]:
                environment.terraform(
                    main_root,
                    "apply",
                    "-input=false",
                    "-no-color",
                    str(plans[main_root]),
                )
            stage = "secret"
            guard()
            if arguments.force_delete_secret and any(found["secrets"].values()):
                # Empty state permits only ownership-checked scheduled secrets.
                session = operator_session(deployment) if saved[main_root] else admin
                for secret in found["secrets"].values():
                    force_delete_secret(session, secret)
            stage = "bootstrap"
            guard()
            if plans[bootstrap_root]:
                environment.terraform(
                    bootstrap_root,
                    "apply",
                    "-input=false",
                    "-no-color",
                    str(plans[bootstrap_root]),
                )
        stage = "verify"
        remaining = inventory(admin, names, config, expected)
        remaining_saved = {
            directory: saved_resources(environment, directory, targets, config)
            for directory, targets in expected.items()
        }
        report = resource_report(environment, config, names, remaining_saved, remaining)
        if (
            report["present_resources"]
            or report["image"]
            or report["active_vms"]
            or report["orphan_resources"]
            or not report["local_state_empty"]
        ):
            raise CloudboxError(
                "resources_remain",
                "Some Cloudbox resources remain. Inspect and run teardown again.",
                inventory=report,
            )
        pending = {
            address: secret
            for address, secret in remaining["secrets"].items()
            if secret
        }
        if any(
            arguments.force_delete_secret or not secret.get("DeletedDate")
            for secret in pending.values()
        ):
            raise CloudboxError(
                "secret_remains",
                "A secret is not removed or scheduled for deletion.",
                inventory=report,
            )
        openrouter = remaining["secret"]
        emit(
            {
                **report,
                "deleted": True,
                "all_resources_absent": report["clean"],
                "secret_deletion_date": openrouter["DeletedDate"].isoformat()
                if openrouter
                else None,
                "secret_deletion_dates": {
                    secret["Name"]: secret["DeletedDate"].isoformat()
                    for secret in pending.values()
                },
                "local_files_retained": True,
            }
        )
        return 0
    except (
        CloudboxError,
        BotoCoreError,
        ClientError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        EOFError,
        subprocess.SubprocessError,
    ) as error:
        emit({**error_record(error), "stage": stage, "deleted": False})
        return 1
    except KeyboardInterrupt:
        emit(
            {
                "ok": False,
                "deleted": False,
                "stage": stage,
                "error": {"code": "interrupted"},
            }
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
