"""Build the AWS image without selecting it for later runs."""

import argparse
import hashlib
import io
import sys
import time
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from cloudbox.common import (
    MICROVM_SERVICE,
    ROOT,
    S3_ENCRYPTION,
    SDK_CONFIG,
    CloudboxError,
    emit,
    error_record,
    load_deployment,
    operator_session,
)
from cloudbox.environments import add_environment_argument, get_environment

SOURCE_NAMES = (
    "Dockerfile",
    "listener.py",
    "supervisor.py",
    "finish.mjs",
    "github_access.py",
    "requirements.txt",
    "startup.sh",
    "teardown.sh",
)
SHARED_SOURCE_NAMES = ("github_api.py",)
HOOK_PORT = 8080
RUN_HOOK_TIMEOUT_SECONDS = 30
READY_HOOK_TIMEOUT_SECONDS = 120
POLL_SECONDS = 10
BUILD_WAIT_SECONDS = 1800
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ZIP_CONTENT_TYPE = "application/zip"
IMAGE_OWNER = "CloudboxImageScript"
VERSION_COMPLETE = {"SUCCESSFUL", "FAILED", "DELETED", "DELETE_FAILED"}
VERSION_PENDING = {"PENDING", "IN_PROGRESS"}
IMAGE_BUILD_PENDING = {"CREATING", "UPDATING"}
IMAGE_DELETABLE = {
    "CREATED",
    "CREATE_FAILED",
    "UPDATED",
    "UPDATE_FAILED",
    "DELETE_FAILED",
}
VERSION_DELETE_PENDING = VERSION_PENDING | {"DELETING"}
TRANSIENT_IMAGE_READ_ERRORS = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
DELETE_SDK_CONFIG = SDK_CONFIG.merge(
    Config(retries={"mode": "standard", "total_max_attempts": 1})
)
IMAGE_MISSING = "ResourceNotFoundException"
BUILD_SETTING_FIELDS = (
    "baseImageArn",
    "buildRoleArn",
    "codeArtifact",
    "cpuConfigurations",
    "resources",
    "hooks",
    "logging",
)


def source_archive():
    # Package named runtime sources; secrets and Terraform files cannot enter the image.
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        sources = [(name, ROOT / "worker" / name) for name in SOURCE_NAMES]
        sources.extend((name, ROOT / "cloudbox" / name) for name in SHARED_SOURCE_NAMES)
        for name, path in sources:
            if path.is_symlink() or not path.is_file():
                raise CloudboxError(
                    "source_invalid", "A required worker file is missing or is a link."
                )
            entry = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes())
    return content.getvalue()


def version_summary(response):
    return {
        key: response.get(key)
        for key in (
            "imageArn",
            "imageVersion",
            "state",
            "status",
            "latestActiveImageVersion",
            "latestFailedImageVersion",
        )
    }


def wait_for_version(client, image_arn, version):
    deadline = time.monotonic() + BUILD_WAIT_SECONDS
    while True:
        response = client.get_microvm_image_version(
            imageIdentifier=image_arn, imageVersion=version
        )
        if response.get("state") in VERSION_COMPLETE:
            return response
        if time.monotonic() >= deadline:
            raise CloudboxError(
                "build_pending",
                "The build wait ended; inspect the image before another build.",
                image_arn=image_arn,
                image_version=version,
            )
        time.sleep(POLL_SECONDS)


def image_request(deployment, artifact):
    digest = hashlib.sha256(artifact).hexdigest()
    key = f"{deployment['image_source_prefix']}{digest}/source.zip"
    request = {
        "baseImageArn": deployment["base_image_arn"],
        "buildRoleArn": deployment["build_role_arn"],
        "codeArtifact": {"uri": f"s3://{deployment['bucket_name']}/{key}"},
        "cpuConfigurations": [{"architecture": deployment["architecture"]}],
        "resources": [{"minimumMemoryInMiB": deployment["memory_mib"]}],
        "hooks": {
            "port": HOOK_PORT,
            "microvmHooks": {
                "run": "ENABLED",
                "runTimeoutInSeconds": RUN_HOOK_TIMEOUT_SECONDS,
            },
            "microvmImageHooks": {
                "ready": "ENABLED",
                "readyTimeoutInSeconds": READY_HOOK_TIMEOUT_SECONDS,
            },
        },
        "logging": {
            "cloudWatch": {
                "logGroup": deployment["log_group_name"],
                "logStream": f"build-{digest}",
            }
        },
        "description": f"Cloudbox source {digest}",
    }
    if deployment.get("base_image_version"):
        request["baseImageVersion"] = deployment["base_image_version"]
    return request, key


def owned_image(client, image_arn):
    try:
        image = client.get_microvm_image(imageIdentifier=image_arn)
    except ClientError as error:
        if error.response["Error"]["Code"] == IMAGE_MISSING:
            return None
        raise
    if image.get("state") == "DELETED":
        return None
    if image.get("tags", {}).get("ManagedBy") != IMAGE_OWNER:
        raise CloudboxError(
            "image_owner_mismatch", "The image is not owned by this script."
        )
    return image


def start_build(client, session, deployment, artifact, request, key, *, create):
    # Identical archives use one key; no runtime secret enters the build request.
    session.client("s3", config=SDK_CONFIG).put_object(
        Bucket=deployment["bucket_name"],
        Key=key,
        Body=artifact,
        ServerSideEncryption=S3_ENCRYPTION,
        ContentType=ZIP_CONTENT_TYPE,
    )
    request = {**request, "clientToken": str(uuid.uuid4())}
    if create:
        return client.create_microvm_image(
            **request,
            name=deployment["image_name"],
            tags={"Project": deployment["project_name"], "ManagedBy": IMAGE_OWNER},
        )
    return client.update_microvm_image(
        **request, imageIdentifier=deployment["image_arn"]
    )


def ready_version(client, image_arn, version, *, wait):
    response = client.get_microvm_image_version(
        imageIdentifier=image_arn, imageVersion=version
    )
    if response.get("state") in VERSION_PENDING:
        if not wait:
            raise CloudboxError(
                "build_pending",
                "The image build is still running.",
                image_arn=image_arn,
                image_version=version,
            )
        response = wait_for_version(client, image_arn, version)
    if response.get("state") != "SUCCESSFUL" or response.get("status") != "ACTIVE":
        raise CloudboxError(
            "image_not_ready",
            "The matching image build is not successful and active.",
            image_arn=image_arn,
            image_version=version,
            state=response.get("state"),
            status=response.get("status"),
        )
    return response


def ensure_image(deployment, session=None, *, wait=True):
    """Reuse or build the current worker; return its successful, active version."""
    session = session or operator_session(deployment)
    client = session.client(MICROVM_SERVICE, config=SDK_CONFIG)
    artifact = source_archive()
    request, key = image_request(deployment, artifact)
    image_arn = deployment["image_arn"]
    image = owned_image(client, image_arn)
    if image is not None:
        fields = (
            (*BUILD_SETTING_FIELDS, "baseImageVersion")
            if "baseImageVersion" in request
            else BUILD_SETTING_FIELDS
        )
        matching = []
        page_request = {"imageIdentifier": image_arn}
        while True:
            page = client.list_microvm_image_versions(**page_request)
            matching.extend(
                version
                for version in page.get("items", [])
                if all(version.get(field) == request[field] for field in fields)
                and version.get("state") not in {"DELETED", "DELETING"}
            )
            if not page.get("nextToken"):
                break
            page_request["nextToken"] = page["nextToken"]
        # A prior response can be lost. Inspect matching versions before another build.
        for version in matching:
            if (
                version.get("state") == "SUCCESSFUL"
                and version.get("status") == "ACTIVE"
            ):
                return ready_version(
                    client, image_arn, version["imageVersion"], wait=wait
                )
        for version in matching:
            if version.get("state") in VERSION_PENDING:
                return ready_version(
                    client, image_arn, version["imageVersion"], wait=wait
                )
        if matching:
            # Do not repeat a failed build on each setup run. An explicit update can retry it.
            return ready_version(
                client, image_arn, matching[0]["imageVersion"], wait=wait
            )
        if image.get("state") in {"CREATING", "UPDATING", "DELETING"}:
            raise CloudboxError(
                "image_busy",
                "An unmatched image operation is still running.",
                image_arn=image_arn,
            )
    response = start_build(
        client, session, deployment, artifact, request, key, create=image is None
    )
    return ready_version(client, image_arn, response["imageVersion"], wait=wait)


def delete_image_once(session, image_arn):
    # Delete has no idempotency token; an unknown response must not repeat it.
    session.client(MICROVM_SERVICE, config=DELETE_SDK_CONFIG).delete_microvm_image(
        imageIdentifier=image_arn
    )


def wait_until_deletable(client, image_arn, *, wait):
    # The parent can be ready while an inactive version still builds.
    deadline = time.monotonic() + BUILD_WAIT_SECONDS
    while True:
        unavailable = False
        try:
            image = owned_image(client, image_arn)
            if image is None or image.get("state") == "DELETING":
                return image
            busy = image.get("state") in IMAGE_BUILD_PENDING
            if not busy:
                if image.get("state") not in IMAGE_DELETABLE:
                    raise CloudboxError(
                        "image_state_unknown",
                        "Inspect the image state before deletion.",
                        image_arn=image_arn,
                    )
                request = {"imageIdentifier": image_arn}
                while True:
                    page = client.list_microvm_image_versions(**request)
                    busy = any(
                        item.get("state") in VERSION_DELETE_PENDING
                        for item in page.get("items", [])
                    )
                    if busy or not page.get("nextToken"):
                        break
                    if time.monotonic() >= deadline:
                        raise CloudboxError(
                            "image_check_unavailable",
                            "The image check timed out; inspect it before retrying.",
                            image_arn=image_arn,
                        )
                    request["nextToken"] = page["nextToken"]
                if not busy:
                    return image
        except TRANSIENT_IMAGE_READ_ERRORS:
            # Retry read failures only; a failed read does not prove build completion.
            unavailable = True
        remaining = deadline - time.monotonic()
        if not wait or remaining <= 0:
            code = "image_check_unavailable" if unavailable else "image_busy"
            raise CloudboxError(
                code,
                "The image is not ready for deletion; inspect it before retrying.",
                image_arn=image_arn,
            )
        time.sleep(min(POLL_SECONDS, remaining))


def wait_for_deletion(client, image_arn):
    deadline = time.monotonic() + BUILD_WAIT_SECONDS
    while True:
        try:
            image = owned_image(client, image_arn)
            if image is None:
                return
            if image.get("state") == "DELETE_FAILED":
                raise CloudboxError(
                    "image_delete_failed",
                    "AWS could not delete the image.",
                    image_arn=image_arn,
                )
        except TRANSIENT_IMAGE_READ_ERRORS:
            # Keep checking a submitted deletion through short network failures.
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CloudboxError(
                "image_delete_pending",
                "The image deletion is still running.",
                image_arn=image_arn,
            )
        time.sleep(min(POLL_SECONDS, remaining))


def main(argv=None):
    try:
        parser = argparse.ArgumentParser(
            description="Manage the script-owned MicroVM image."
        )
        add_environment_argument(parser)
        parser.add_argument(
            "action", choices=("create", "update", "ensure", "status", "delete")
        )
        parser.add_argument("--wait", action="store_true")
        parser.add_argument("--version")
        parser.add_argument("--confirm-name")
        arguments = parser.parse_args(argv)
        if arguments.action == "status" and arguments.wait and not arguments.version:
            raise CloudboxError(
                "version_required", "Use --version when waiting for an image build."
            )
        deployment = load_deployment(get_environment(arguments.env))
        session = operator_session(deployment)
        client = session.client(MICROVM_SERVICE, config=SDK_CONFIG)
        image_arn = deployment["image_arn"]
        if arguments.action == "ensure":
            response = ensure_image(deployment, session, wait=arguments.wait)
            emit({"ok": True, **version_summary(response), "selected_for_runs": False})
            return 0
        if arguments.action == "status":
            response = client.get_microvm_image(imageIdentifier=image_arn)
            version = arguments.version
            if version:
                response = client.get_microvm_image_version(
                    imageIdentifier=image_arn, imageVersion=version
                )
                if arguments.wait:
                    response = wait_for_version(client, image_arn, version)
            emit({"ok": True, **version_summary(response)})
            return 0
        if arguments.action == "delete":
            if arguments.confirm_name != deployment["image_name"]:
                raise CloudboxError(
                    "confirmation_required",
                    "Pass --confirm-name with the configured image name.",
                )
            image = wait_until_deletable(client, image_arn, wait=arguments.wait)
            if image is None:
                emit(
                    {
                        "ok": True,
                        "image_arn": image_arn,
                        "state": "DELETED",
                        "source_archives_retained": True,
                    }
                )
                return 0
            request = {"imageIdentifier": image_arn}
            while True:
                page = client.list_microvms(**request)
                if any(
                    item.get("state") != "TERMINATED" for item in page.get("items", [])
                ):
                    raise CloudboxError(
                        "image_in_use", "Stop the image's active VMs before deletion."
                    )
                if not page.get("nextToken"):
                    break
                request["nextToken"] = page["nextToken"]
            if image and image.get("state") != "DELETING":
                delete_image_once(session, image_arn)
            if arguments.wait:
                wait_for_deletion(client, image_arn)
            emit(
                {
                    "ok": True,
                    "image_arn": image_arn,
                    "delete_requested": True,
                    "deletion_complete": arguments.wait,
                    "source_archives_retained": True,
                }
            )
            return 0
        artifact = source_archive()
        request, key = image_request(deployment, artifact)
        if arguments.action == "update" and owned_image(client, image_arn) is None:
            raise CloudboxError(
                "image_missing", "Create the image before an explicit update."
            )
        response = start_build(
            client,
            session,
            deployment,
            artifact,
            request,
            key,
            create=arguments.action == "create",
        )
        version = response["imageVersion"]
        if arguments.wait:
            response = wait_for_version(client, image_arn, version)
        result = {
            "ok": response.get("state")
            not in {"FAILED", "CREATION_FAILED", "UPDATE_FAILED"},
            **version_summary(response),
            "source_uri": request["codeArtifact"]["uri"],
            "selected_for_runs": False,
        }
        emit(result)
        return 0 if result["ok"] else 1
    except (
        CloudboxError,
        BotoCoreError,
        ClientError,
        OSError,
        ValueError,
        KeyError,
    ) as error:
        record = error_record(error)
        # Image requests contain no runtime secrets; keep AWS build denials actionable.
        if isinstance(error, ClientError) and error.operation_name in {
            "CreateMicrovmImage",
            "UpdateMicrovmImage",
        }:
            record["error"]["message"] = error.response.get("Error", {}).get("Message")
        emit(record)
        return 1
    except KeyboardInterrupt:
        emit(
            {
                "ok": False,
                "error": {
                    "code": "interrupted",
                    "message": "The AWS build can still be running.",
                },
            }
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
