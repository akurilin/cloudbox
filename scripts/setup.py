"""Prepare Cloudbox infrastructure without submitting jobs."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import BotoCoreError, ClientError

from cloudbox.common import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    SDK_CONFIG,
    CloudboxError,
    emit,
    error_record,
    load_deployment,
    operator_session,
)
from cloudbox.environments import add_environment_argument, get_environment
from scripts.build_image import ensure_image
from scripts.set_secret import key_from_file

REQUIRED_TOOLS = ("terraform", "aws")
INPUT_FIELDS = {
    "aws_account_id",
    "aws_region",
    "aws_profile",
    "project_name",
    "sso_permission_set_name",
    "image_version",
    "base_image_version",
    "memory_mib",
    "default_model",
    "timeout_seconds",
}
REQUIRED_FIELDS = {"aws_account_id", "aws_region", "aws_profile", "project_name"}
MEMORY_SIZES = {512, 1024, 2048, 4096, 8192}
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
REQUEST_TIMEOUT_SECONDS = 30
ROLE_READY_TIMEOUT_SECONDS = 120
ROLE_READY_POLL_SECONDS = 5
ROLE_READY_RETRY_CODES = {"AccessDenied", "AccessDeniedException"}
MAX_KEY_RESPONSE_BYTES = 65_536
MAX_INPUT_BYTES = 65_536
ARN_PATTERN = re.compile(r"arn:aws:([a-z0-9-]+):([^:\s]*):([^:\s]*):")
REGION_PATTERN = re.compile(r"[a-z]{2}-[a-z]+-[0-9]+")


def read_config(environment):
    raw = environment.input_path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        raise CloudboxError("invalid_config", "The Terraform input file is too large.")
    document = json.loads(raw)
    if not isinstance(document, dict) or set(document) != {"deployment"}:
        raise CloudboxError(
            "invalid_config", "The input file must contain only deployment settings."
        )
    config = document["deployment"]
    if (
        not isinstance(config, dict)
        or config.keys() - INPUT_FIELDS
        or REQUIRED_FIELDS - config.keys()
    ):
        raise CloudboxError(
            "invalid_config",
            "Use the fields in the Terraform input example; do not add secrets.",
        )
    for field in REQUIRED_FIELDS:
        if not isinstance(config[field], str) or not config[field].strip():
            raise CloudboxError(
                "invalid_config", "Set the account, region, profile, and project name."
            )
    if not re.fullmatch(r"[0-9]{12}", config["aws_account_id"]):
        raise CloudboxError("invalid_config", "The account ID must contain 12 digits.")
    if not REGION_PATTERN.fullmatch(config["aws_region"]):
        raise CloudboxError("invalid_config", "Use a commercial AWS region.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,19}", config["project_name"]):
        raise CloudboxError(
            "invalid_config",
            "The project name must use 2-20 lowercase letters, digits, or hyphens.",
        )
    memory = config.get("memory_mib", 1024)
    timeout = config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if type(memory) is not int or memory not in MEMORY_SIZES:
        raise CloudboxError("invalid_config", "Use a supported MicroVM memory size.")
    if (
        type(timeout) is not int
        or not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS
    ):
        raise CloudboxError("invalid_config", "The timeout must be 60 to 3300 seconds.")
    for field in ("default_model", "sso_permission_set_name"):
        if field in config and (
            not isinstance(config[field], str)
            or not config[field]
            or any(c.isspace() for c in config[field])
        ):
            raise CloudboxError(
                "invalid_config",
                "Model and permission-set names must not be empty or contain spaces.",
            )
    for field in ("image_version", "base_image_version"):
        value = config.get(field)
        if value is None and field == "base_image_version":
            continue
        if field in config and (
            not isinstance(value, str)
            or any(c.isspace() for c in value)
            or value.lower() == "latest"
        ):
            raise CloudboxError(
                "invalid_config",
                "Use an exact image version, or leave it unset for setup.",
            )
    return document, raw


def state_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from state_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from state_strings(item)
    elif isinstance(value, str):
        yield value


def check_local_state(config, environment):
    # Check state before init or apply; IAM policy ARNs identify bootstrap's region.
    for directory in environment.roots:
        environment.check_backend(directory, required=False)
        state_path = environment.state_path(directory)
        if not state_path.exists():
            continue
        state = json.loads(state_path.read_bytes())
        if not isinstance(state, dict):
            raise CloudboxError("invalid_state", "A Terraform state file is invalid.")
        if not state.get("resources") and not state.get("outputs"):
            continue
        accounts, regions = set(), set()
        for value in state_strings(state):
            for _, region, account in ARN_PATTERN.findall(value):
                if re.fullmatch(r"[0-9]{12}", account):
                    accounts.add(account)
                if REGION_PATTERN.fullmatch(region):
                    regions.add(region)
        deployment = state.get("outputs", {}).get("cloudbox", {}).get("value", {})
        if isinstance(deployment, dict):
            if deployment.get("aws_account_id"):
                accounts.add(deployment["aws_account_id"])
            if deployment.get("aws_region"):
                regions.add(deployment["aws_region"])
            if (
                deployment.get("project_name", config["project_name"])
                != config["project_name"]
            ):
                raise CloudboxError(
                    "state_mismatch", "The state belongs to another project."
                )
        # Partial state can lack regional ARNs. Exact manifest checks still precede writes.
        if accounts - {config["aws_account_id"]} or regions - {config["aws_region"]}:
            raise CloudboxError(
                "state_mismatch",
                "The saved state conflicts with the configured account or region.",
            )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        # Keep the authorization header on the documented OpenRouter endpoint only.
        return None


def validate_key(secret):
    if not secret or any(character.isspace() for character in secret):
        raise CloudboxError(
            "invalid_secret", "Supply a non-empty OpenRouter key without spaces."
        )
    request = Request(OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {secret}"})
    try:
        with build_opener(NoRedirect()).open(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            if response.status != HTTPStatus.OK:
                raise CloudboxError(
                    "key_check_failed", "OpenRouter did not accept the key check."
                )
            raw = response.read(MAX_KEY_RESPONSE_BYTES + 1)
        if len(raw) > MAX_KEY_RESPONSE_BYTES:
            raise ValueError
        data = json.loads(raw)["data"]
        if not isinstance(data, dict):
            raise ValueError
    except (HTTPError, URLError, ValueError, KeyError) as error:
        raise CloudboxError(
            "key_check_failed", "Check the OpenRouter key and network access."
        ) from error
    if data.get("is_management_key") or data.get("is_provisioning_key"):
        raise CloudboxError(
            "invalid_secret", "Use an inference key, not a management key."
        )
    if (
        isinstance(data.get("limit_remaining"), (int, float))
        and data["limit_remaining"] <= 0
    ):
        raise CloudboxError(
            "key_limit_reached", "The OpenRouter key has no remaining allowance."
        )
    if data.get("expires_at"):
        expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        if expiry <= datetime.now(UTC):
            raise CloudboxError("key_expired", "The OpenRouter key has expired.")


def apply_stage(directory, plan_path, config, environment, *, outputs_only=False):
    from cloudbox.resources import check_plan_coverage

    check_local_state(config, environment)
    if json.loads(environment.input_path.read_bytes()).get("deployment") != config:
        raise CloudboxError(
            "config_changed", "The input file changed during setup. Run setup again."
        )
    plan_arguments = ["plan", "-input=false", "-no-color", f"-out={plan_path}"]
    if outputs_only:
        # Select the image in local outputs using state from the preceding apply.
        plan_arguments.append("-refresh=false")
    environment.terraform(directory, *plan_arguments)
    plan = json.loads(
        environment.terraform(directory, "show", "-json", str(plan_path), capture=True)
    )
    check_plan_coverage(directory, plan, environment)
    changes = [
        item.get("change", {}).get("actions", [])
        for item in plan.get("resource_changes", [])
    ]
    if any("delete" in actions for actions in changes):
        raise CloudboxError(
            "destructive_plan",
            "Setup will not delete or replace resources. Review Terraform changes separately.",
        )
    if outputs_only and any(actions != ["no-op"] for actions in changes):
        raise CloudboxError(
            "unexpected_plan",
            "Image selection must change outputs only. Review the plan separately.",
        )
    # Apply exactly the inspected plan; no further prompt is needed after setup approval.
    environment.terraform(
        directory, "apply", "-input=false", "-no-color", str(plan_path)
    )


def select_version(document, original, version, environment):
    input_path = environment.input_path
    if input_path.read_bytes() != original:
        raise CloudboxError(
            "config_changed", "The input file changed during setup. Run setup again."
        )
    if document["deployment"].get("image_version") == version:
        return
    document["deployment"]["image_version"] = version
    # Replace only the authoritative input file; keep every other setting and its mode.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=input_path.parent,
        prefix=".cloudbox-input-",
        delete=False,
    ) as target:
        temporary = Path(target.name)
        json.dump(document, target, indent=2)
        target.write("\n")
    try:
        os.chmod(temporary, stat.S_IMODE(input_path.stat().st_mode))
        os.replace(temporary, input_path)
    finally:
        temporary.unlink(missing_ok=True)


def wait_for_provisioner(config, environment):
    role_arn = json.loads(
        environment.terraform(
            environment.bootstrap_root,
            "output",
            "-json",
            "provisioner_role_arn",
            capture=True,
        )
    )
    if not isinstance(role_arn, str) or not role_arn.startswith(
        f"arn:aws:iam::{config['aws_account_id']}:role/"
    ):
        raise CloudboxError(
            "invalid_role_output", "Bootstrap returned an invalid provisioner role ARN."
        )
    deployment = {**config, "provisioner_role_arn": role_arn}
    deadline = time.monotonic() + ROLE_READY_TIMEOUT_SECONDS
    # New IAM trust can propagate after apply returns. Retry only this access check.
    while time.monotonic() < deadline:
        try:
            operator_session(deployment)
            return
        except ClientError as error:
            if (
                error.operation_name != "AssumeRole"
                or error.response.get("Error", {}).get("Code")
                not in ROLE_READY_RETRY_CODES
            ):
                raise
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(ROLE_READY_POLL_SECONDS, remaining))
    raise CloudboxError(
        "provisioner_not_ready",
        f"The provisioner role still denies access after {ROLE_READY_TIMEOUT_SECONDS} seconds. Run setup again.",
    )


def main(argv=None):
    stage = "preflight"
    try:
        parser = argparse.ArgumentParser(
            description="Prepare Cloudbox infrastructure and image without submitting jobs."
        )
        add_environment_argument(parser)
        parser.add_argument(
            "--env-file",
            type=Path,
            help="Read the OpenRouter key from this file instead of .env.<env>.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Approve all setup stages without a prompt.",
        )
        arguments = parser.parse_args(argv)
        environment = get_environment(arguments.env)
        for tool in REQUIRED_TOOLS:
            if shutil.which(tool) is None:
                raise CloudboxError("missing_tool", f"Install {tool} before setup.")
        document, original = read_config(environment)
        config = document["deployment"]
        check_local_state(config, environment)
        secret = key_from_file(arguments.env_file or environment.key_path)
        validate_key(secret)
        operator_session(config, provisioner=False)
        from cloudbox.resources import check_resources

        inventory = check_resources(environment)
        if inventory["untracked_resources"] or inventory["orphan_resources"]:
            raise CloudboxError(
                "untracked_resources",
                "Resolve resources outside the selected state before setup.",
            )
        if inventory["secret"] == "pending_deletion":
            raise CloudboxError(
                "secret_pending_deletion",
                "Remove or restore the pending secret before setup.",
            )
        print(
            f"Environment: {environment.name}\nAccount: {config['aws_account_id']}\nRegion: {config['aws_region']}\n"
            "Steps: IAM bootstrap; infrastructure; secret; image; image selection.\n"
            "AWS charges apply. Setup does not submit jobs or delete resources.",
            file=sys.stderr,
        )
        if not arguments.yes:
            # Keep approval text out of the JSON result stream.
            print("Approve setup? [y/N] ", end="", file=sys.stderr, flush=True)
            if input().strip().lower() not in {"y", "yes"}:
                raise CloudboxError("not_approved", "Setup was not approved.")
        with tempfile.TemporaryDirectory(prefix="cloudbox-setup-") as plan_directory:
            plans = Path(plan_directory)
            for stage, directory in (
                ("bootstrap", environment.bootstrap_root),
                ("infrastructure", environment.main_root),
            ):
                print(f"Setup: {stage}", file=sys.stderr, flush=True)
                check_local_state(config, environment)
                operator_session(config, provisioner=False)
                environment.terraform(directory, "init", "-input=false", "-no-color")
                apply_stage(directory, plans / f"{stage}.tfplan", config, environment)
                if directory == environment.bootstrap_root:
                    stage = "provisioner_readiness"
                    print(
                        "Setup: wait for provisioner access",
                        file=sys.stderr,
                        flush=True,
                    )
                    wait_for_provisioner(config, environment)
            stage = "secret"
            deployment = load_deployment(environment)
            session = operator_session(deployment)
            session.client("secretsmanager", config=SDK_CONFIG).put_secret_value(
                SecretId=deployment["openrouter_secret_arn"],
                SecretString=secret,
            )
            del secret
            stage = "image"
            print("Setup: build or reuse image", file=sys.stderr, flush=True)
            image = ensure_image(deployment, session=session, wait=True)
            version = image["imageVersion"]
            if image.get("state") != "SUCCESSFUL" or image.get("status") != "ACTIVE":
                raise CloudboxError(
                    "image_unavailable", "The image is not ready and active."
                )
            stage = "image_selection"
            check_local_state(config, environment)
            select_version(document, original, version, environment)
            apply_stage(
                environment.main_root,
                plans / "image-selection.tfplan",
                config,
                environment,
                outputs_only=True,
            )
        # Stop at image selection; the cloud math test is a separate operator action.
        emit(
            {
                "ok": True,
                "ready": True,
                "environment": environment.name,
                "account_id": config["aws_account_id"],
                "region": config["aws_region"],
                "image_version": version,
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
        emit({**error_record(error), "ready": False, "stage": stage})
        return 1
    except KeyboardInterrupt:
        emit(
            {
                "ok": False,
                "ready": False,
                "stage": stage,
                "error": {"code": "interrupted"},
            }
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
