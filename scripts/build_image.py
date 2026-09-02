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

from botocore.exceptions import BotoCoreError, ClientError
from cloudbox.common import (
    ROOT, MICROVM_SERVICE, SDK_CONFIG, S3_ENCRYPTION, CloudboxError,
    emit, error_record, load_deployment, operator_session,
)

SOURCE_NAMES = ("Dockerfile", "listener.py", "supervisor.py", "requirements.txt", "startup.sh", "teardown.sh")
HOOK_PORT = 8080
RUN_HOOK_TIMEOUT_SECONDS = 30
READY_HOOK_TIMEOUT_SECONDS = 120
POLL_SECONDS = 10
BUILD_WAIT_SECONDS = 1800
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ZIP_CONTENT_TYPE = "application/zip"
IMAGE_OWNER = "CloudboxImageScript"
VERSION_COMPLETE = {"SUCCESSFUL", "FAILED", "DELETED", "DELETE_FAILED"}


def source_archive():
    # Package only worker sources; root secrets and Terraform files cannot enter the image.
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in SOURCE_NAMES:
            path = ROOT / "worker" / name
            if path.is_symlink() or not path.is_file():
                raise CloudboxError("source_invalid", "A required worker file is missing or is a link.")
            entry = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes())
    return content.getvalue()


def version_summary(response):
    return {key: response.get(key) for key in ("imageArn", "imageVersion", "state", "status")}


def wait_for_version(client, image_arn, version):
    deadline = time.monotonic() + BUILD_WAIT_SECONDS
    while True:
        response = client.get_microvm_image_version(imageIdentifier=image_arn, imageVersion=version)
        if response.get("state") in VERSION_COMPLETE:
            return response
        if time.monotonic() >= deadline:
            raise CloudboxError("build_pending", "The build wait ended; inspect the image before another build.",
                                image_arn=image_arn, image_version=version)
        time.sleep(POLL_SECONDS)


def main():
    try:
        parser = argparse.ArgumentParser(description="Manage the script-owned MicroVM image.")
        parser.add_argument("action", choices=("create", "update", "status", "delete"))
        parser.add_argument("--wait", action="store_true")
        parser.add_argument("--version")
        parser.add_argument("--confirm-name")
        arguments = parser.parse_args()
        deployment = load_deployment()
        session = operator_session(deployment)
        client = session.client(MICROVM_SERVICE, config=SDK_CONFIG)
        image_arn = deployment["image_arn"]
        if arguments.action == "status":
            response = client.get_microvm_image(imageIdentifier=image_arn)
            version = arguments.version or response.get("imageVersion")
            if version:
                response = client.get_microvm_image_version(imageIdentifier=image_arn, imageVersion=version)
                if arguments.wait:
                    response = wait_for_version(client, image_arn, version)
            emit({"ok": True, **version_summary(response)})
            return 0
        if arguments.action == "delete":
            if arguments.confirm_name != deployment["image_name"]:
                raise CloudboxError("confirmation_required", "Pass --confirm-name with the configured image name.")
            image = client.get_microvm_image(imageIdentifier=image_arn)
            if image.get("tags", {}).get("ManagedBy") != IMAGE_OWNER:
                raise CloudboxError("image_owner_mismatch", "The image is not owned by this script.")
            request = {"imageIdentifier": image_arn}
            while True:
                page = client.list_microvms(**request)
                if any(item.get("state") != "TERMINATED" for item in page.get("microvms", [])):
                    raise CloudboxError("image_in_use", "Stop the image's active VMs before deletion.")
                if not page.get("nextToken"):
                    break
                request["nextToken"] = page["nextToken"]
            client.delete_microvm_image(imageIdentifier=image_arn)
            emit({"ok": True, "image_arn": image_arn, "delete_requested": True, "source_archives_retained": True})
            return 0
        artifact = source_archive()
        digest = hashlib.sha256(artifact).hexdigest()
        key = f"{deployment['image_source_prefix']}{digest}/source.zip"
        session.client("s3", config=SDK_CONFIG).put_object(
            Bucket=deployment["bucket_name"], Key=key, Body=artifact,
            ServerSideEncryption=S3_ENCRYPTION, ContentType=ZIP_CONTENT_TYPE,
        )
        request = {
            "baseImageArn": deployment["base_image_arn"],
            "buildRoleArn": deployment["build_role_arn"],
            "codeArtifact": {"uri": f"s3://{deployment['bucket_name']}/{key}"},
            "cpuConfigurations": [{"architecture": deployment["architecture"]}],
            "resources": [{"minimumMemoryInMiB": deployment["memory_mib"]}],
            "hooks": {"port": HOOK_PORT,
                      "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": RUN_HOOK_TIMEOUT_SECONDS},
                      "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": READY_HOOK_TIMEOUT_SECONDS}},
            "logging": {"cloudWatch": {"logGroup": deployment["log_group_name"], "logStream": f"build-{digest}"}},
            "description": f"Cloudbox source {digest}", "clientToken": str(uuid.uuid4()),
        }
        if deployment.get("base_image_version"):
            request["baseImageVersion"] = deployment["base_image_version"]
        if arguments.action == "create":
            request["name"] = deployment["image_name"]
            request["tags"] = {"Project": deployment["project_name"], "ManagedBy": IMAGE_OWNER}
            response = client.create_microvm_image(**request)
        else:
            request["imageIdentifier"] = image_arn
            response = client.update_microvm_image(**request)
        version = response["imageVersion"]
        if arguments.wait:
            response = wait_for_version(client, image_arn, version)
        result = {"ok": response.get("state") not in {"FAILED", "CREATION_FAILED", "UPDATE_FAILED"},
                  **version_summary(response), "source_uri": request["codeArtifact"]["uri"],
                  "selected_for_runs": False}
        emit(result)
        return 0 if result["ok"] else 1
    except (CloudboxError, BotoCoreError, ClientError, OSError, ValueError, KeyError) as error:
        emit(error_record(error))
        return 1
    except KeyboardInterrupt:
        emit({"ok": False, "error": {"code": "interrupted", "message": "The AWS build can still be running."}})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
