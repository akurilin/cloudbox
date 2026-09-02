"""Submit and inspect cloud runs; no local execution mode."""

import argparse
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from .common import (
    CREDENTIAL_MARGIN_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    GITHUB_SCHEMA_VERSION,
    MAX_PROMPT_CHARACTERS,
    MAX_RECORD_BYTES,
    MICROVM_SERVICE,
    SCHEMA_VERSION,
    SDK_CONFIG,
    TASK_STATUSES,
    CloudboxError,
    emit,
    error_record,
    get_record,
    load_deployment,
    operator_session,
    parse_timeout,
    put_record,
    run_prefix,
    scoped_data_credentials,
    timestamp,
    validate_spec,
)
from .environments import add_environment_argument, get_environment
from .github import prepare_github_access, revoke_quietly, token_deadline

LIST_PAGE_SIZE = 20
MAX_LIST_PAGE_SIZE = 100
LOG_POLL_SECONDS = 2
LOG_SETTLE_POLLS = 3
IDLE_SUSPEND_SECONDS = 28_800
MAX_HOOK_PAYLOAD_BYTES = 4096
AWS_TERMINATED = "TERMINATED"
UNKNOWN = "unknown"
DOWNLOAD_NAMES = ("spec.json", "launch.json", "result.json", "output/result.json")
REJECTED_LAUNCH_CODES = {
    "AccessDeniedException", "UnauthorizedException", "ValidationException",
    "ResourceNotFoundException", "ServiceQuotaExceededException", "TooManyRequestsException",
}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CloudboxError("invalid_arguments", message)


def run_id(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as error:
        raise CloudboxError("invalid_run_id", "Use the full run UUID.") from error
    return value


def build_parser():
    parser = Parser(prog="cloudbox")
    add_environment_argument(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="Start one cloud run.")
    submit.add_argument("prompt", nargs="?")
    submit.add_argument("--spec", type=Path)
    submit.add_argument("--model")
    submit.add_argument("--timeout")
    listing = commands.add_parser("list", help="List saved runs.")
    listing.add_argument("--status", choices=sorted(TASK_STATUSES))
    listing.add_argument("--limit", type=int, default=LIST_PAGE_SIZE)
    listing.add_argument("--cursor")
    for name in ("status", "logs", "download", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("run_id", type=run_id)
        if name == "logs":
            command.add_argument("--follow", action="store_true")
        if name == "download":
            command.add_argument("--output", "--directory", dest="directory", type=Path)
    for command in commands.choices.values():
        command.add_argument(
            "--json", action="store_true", help="JSON output is always enabled."
        )
    return parser


def input_spec(arguments):
    # Read prompt sources before any AWS call; the spec is not a file attachment.
    if arguments.spec is not None and arguments.prompt is not None:
        raise CloudboxError("conflicting_input", "Use a prompt or --spec, not both.")
    values = {}
    if arguments.spec is not None:
        with arguments.spec.open("rb") as source:
            raw = source.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise CloudboxError("invalid_spec", "The job file is too large.")
        try:
            values = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise CloudboxError(
                "invalid_spec", "The job file is not valid JSON."
            ) from error
        if not isinstance(values, dict):
            raise CloudboxError("invalid_spec", "The job file must contain an object.")
    else:
        if arguments.prompt is None:
            raise CloudboxError(
                "missing_prompt", "Supply a prompt, - for stdin, or --spec."
            )
        values["prompt"] = (
            sys.stdin.read(MAX_PROMPT_CHARACTERS + 1)
            if arguments.prompt == "-"
            else arguments.prompt
        )
    if arguments.model is not None:
        values["model"] = arguments.model
    if arguments.timeout is not None:
        values["timeout_seconds"] = parse_timeout(arguments.timeout)
    # Validate supplied fields before loading deployment defaults.
    validate_spec(
        {
            "schema_version": SCHEMA_VERSION,
            "model": "default",
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            **values,
        }
    )
    return values


class Runs:
    def __init__(self, deployment, environment):
        self.deployment = deployment
        self.environment = environment
        self.session = operator_session(deployment)
        self.s3 = self.session.client("s3", config=SDK_CONFIG)
        self.compute = self.session.client(MICROVM_SERVICE, config=SDK_CONFIG)
        self.bucket = deployment["bucket_name"]

    def record(self, identity, name):
        return get_record(self.s3, self.bucket, run_prefix(identity) + name)

    def submit(self, supplied):
        deployment = self.deployment
        spec = validate_spec(
            {
                "schema_version": SCHEMA_VERSION,
                "model": deployment["default_model"],
                "timeout_seconds": deployment.get(
                    "timeout_seconds", DEFAULT_TIMEOUT_SECONDS
                ),
                **supplied,
            }
        )
        image_version = deployment.get("image_version")
        if (
            not isinstance(image_version, str)
            or not image_version.strip()
            or image_version.lower() == "latest"
        ):
            raise CloudboxError(
                "image_not_selected",
                "Select an explicit image version in Terraform inputs.",
            )
        image = self.compute.get_microvm_image_version(
            imageIdentifier=deployment["image_arn"],
            imageVersion=image_version,
        )
        if image.get("state") != "SUCCESSFUL" or image.get("status") != "ACTIVE":
            raise CloudboxError(
                "image_unavailable",
                "The selected image version is not ready and active.",
            )
        identity = str(uuid.uuid4())
        access = None
        preserve_token = False
        try:
            credentials = scoped_data_credentials(self.session, deployment, identity)
            payload = {
                "schema_version": spec["schema_version"],
                "run_id": identity,
                "bucket_name": self.bucket,
                "data_credentials": {
                    key: credentials[key]
                    for key in ("AccessKeyId", "SecretAccessKey", "SessionToken")
                },
                "openrouter_secret_arn": deployment["openrouter_secret_arn"],
                "log_group_name": deployment["log_group_name"],
                "aws_region": deployment["aws_region"],
            }
            access = prepare_github_access(self.session, deployment)
            if access:
                spec.update(
                    {"schema_version": GITHUB_SCHEMA_VERSION, "github": access.github}
                )
                payload.update(
                    {
                        "schema_version": GITHUB_SCHEMA_VERSION,
                        "github_token": access.token,
                        "github_token_expires_at": access.expires_at,
                    }
                )
            payload_text = json.dumps(payload, separators=(",", ":"))
            if len(payload_text.encode("utf-8")) > MAX_HOOK_PAYLOAD_BYTES:
                raise CloudboxError(
                    "payload_too_large", "Run credentials exceed the AWS payload limit."
                )
            spec.update(
                {
                    "run_id": identity,
                    "submitted_at": timestamp(),
                    "provider": "openrouter",
                    "image_arn": deployment["image_arn"],
                    "image_version": image_version,
                    "required_output": "output/result.json",
                    "resources": {
                        "memory_mib": deployment["memory_mib"],
                        "architecture": deployment["architecture"],
                    },
                }
            )
            seconds_left = (
                credentials["Expiration"] - datetime.now(UTC)
            ).total_seconds()
            if seconds_left < spec["timeout_seconds"] + CREDENTIAL_MARGIN_SECONDS:
                raise CloudboxError(
                    "credentials_expire_early",
                    "Run credentials expire before the deadline.",
                    run_id=identity,
                )
            if access:
                token_deadline(access.expires_at, spec["timeout_seconds"])
            put_record(
                self.s3,
                self.bucket,
                run_prefix(identity) + "spec.json",
                spec,
                exclusive=True,
            )
            try:
                # SDK retries reuse this UUID and this exact payload; a new submit
                # gets a new UUID. Keep credentials after uncertain launch so a
                # running VM can finish.
                preserve_token = True
                response = self.compute.run_microvm(
                    imageIdentifier=deployment["image_arn"],
                    imageVersion=image_version,
                    executionRoleArn=deployment["runtime_role_arn"],
                    ingressNetworkConnectors=[deployment["ingress_connector_arn"]],
                    idlePolicy={
                        "autoResumeEnabled": False,
                        "maxIdleDurationSeconds": IDLE_SUSPEND_SECONDS,
                        "suspendedDurationSeconds": 0,
                    },
                    logging={
                        "cloudWatch": {
                            "logGroup": deployment["log_group_name"],
                            "logStream": identity,
                        }
                    },
                    maximumDurationInSeconds=spec["timeout_seconds"],
                    runHookPayload=payload_text,
                    clientToken=identity,
                )
            except (BotoCoreError, ClientError) as error:
                rejected = isinstance(error, ParamValidationError) or (
                    isinstance(error, ClientError)
                    and error.response.get("ResponseMetadata", {}).get(
                        "RetryAttempts"
                    )
                    == 0
                    and error.response.get("Error", {}).get("Code")
                    in REJECTED_LAUNCH_CODES
                )
                if rejected:
                    preserve_token = False
                    raise CloudboxError(
                        "launch_rejected",
                        "AWS rejected the run before launch.",
                        run_id=identity,
                    ) from None
                raise CloudboxError(
                    "launch_unknown",
                    "AWS launch response is unavailable. Inspect this run before resubmitting.",
                    run_id=identity,
                    task_status=UNKNOWN,
                    compute_state=UNKNOWN,
                ) from None
        finally:
            if access and not preserve_token:
                revoke_quietly(access.token)
        launch = {
            "schema_version": spec["schema_version"],
            "run_id": identity,
            "microvm_id": response["microvmId"],
            "image_arn": deployment["image_arn"],
            "image_version": image_version,
            "log_group_name": deployment["log_group_name"],
            "log_stream_name": identity,
            "started_at": timestamp(),
        }
        saved = True
        try:
            put_record(
                self.s3,
                self.bucket,
                run_prefix(identity) + "launch.json",
                launch,
                exclusive=True,
            )
        except (BotoCoreError, ClientError):
            saved = False
        return {
            "ok": True,
            "run_id": identity,
            "microvm_id": response["microvmId"],
            "task_status": UNKNOWN,
            "compute_state": response.get("state", UNKNOWN),
            "launch_record_saved": saved,
            "spec_uri": f"s3://{self.bucket}/{run_prefix(identity)}spec.json",
        }

    def status(self, identity):
        result = self.record(identity, "result.json")
        launch = self.record(identity, "launch.json")
        spec = self.record(identity, "spec.json")
        state, compute_error = UNKNOWN, None
        if launch and launch.get("microvm_id"):
            try:
                response = self.compute.get_microvm(
                    microvmIdentifier=launch["microvm_id"]
                )
                state = response.get("state", UNKNOWN)
            except (BotoCoreError, ClientError) as error:
                compute_error = error_record(error)["error"]["code"]
        status = result.get("status", UNKNOWN) if result else UNKNOWN
        if status not in TASK_STATUSES:
            status = UNKNOWN
        return {
            "ok": True,
            "run_id": identity,
            "task_status": status,
            "compute_state": state,
            "compute_error": compute_error,
            "exists": bool(spec or result or launch),
            "submitted_at": spec.get("submitted_at") if spec else None,
            "result": result,
            "launch": launch,
        }

    def list(self, arguments):
        if not 1 <= arguments.limit <= MAX_LIST_PAGE_SIZE:
            raise CloudboxError(
                "invalid_limit", f"Use a limit from 1 to {MAX_LIST_PAGE_SIZE}."
            )
        request = {
            "Bucket": self.bucket,
            "Prefix": "runs/",
            "Delimiter": "/",
            "MaxKeys": arguments.limit,
        }
        if arguments.cursor:
            request["ContinuationToken"] = arguments.cursor
        page = self.s3.list_objects_v2(**request)
        runs = []
        for item in page.get("CommonPrefixes", []):
            identity = item["Prefix"].split("/")[1]
            try:
                run_id(identity)
            except CloudboxError:
                continue
            summary = self.status(identity)
            if arguments.status is None or summary["task_status"] == arguments.status:
                runs.append(summary)
        return {
            "ok": True,
            "runs": runs,
            "next_cursor": page.get("NextContinuationToken"),
        }

    def cancel(self, identity):
        before = self.status(identity)
        launch = before["launch"]
        if not launch or not launch.get("microvm_id"):
            raise CloudboxError(
                "compute_unknown",
                "No saved VM ID is available for cancellation.",
                run_id=identity,
            )
        requested = before["compute_state"] != AWS_TERMINATED
        if requested:
            self.compute.terminate_microvm(microvmIdentifier=launch["microvm_id"])
        after = self.status(identity)
        # A stop request is not a completed stop. Preserve any worker result.
        if (
            requested
            and after["compute_state"] == AWS_TERMINATED
            and after["result"] is None
        ):
            result = {
                "schema_version": SCHEMA_VERSION,
                "run_id": identity,
                "status": "cancelled",
                "reason": "operator_cancelled",
                "started_at": launch.get("started_at"),
                "finished_at": timestamp(),
                "exit_code": None,
                "artifact_key": None,
                "artifact_complete": False,
                "usage": {},
            }
            put_record(
                self.s3,
                self.bucket,
                run_prefix(identity) + "result.json",
                result,
                exclusive=True,
            )
            after = self.status(identity)
        return {**after, "cancel_requested": requested}

    def download(self, identity, directory):
        status = self.status(identity)
        target = (
            (directory or Path.cwd() / "downloads" / self.environment.name / identity)
            .expanduser()
            .resolve()
        )
        if target.exists():
            raise CloudboxError("destination_exists", "Use a new download directory.")
        # Fixed relative names exclude S3-controlled paths and directory traversal.
        target.mkdir(parents=True, exist_ok=False)
        files = []
        for name in DOWNLOAD_NAMES:
            key = run_prefix(identity) + name
            try:
                response = self.s3.get_object(Bucket=self.bucket, Key=key)
            except ClientError as error:
                if error.response["Error"]["Code"] in {"NoSuchKey", "NotFound", "404"}:
                    continue
                raise
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with response["Body"] as body, path.open("xb") as destination:
                contents = body.read(MAX_RECORD_BYTES + 1)
                if len(contents) > MAX_RECORD_BYTES:
                    raise CloudboxError(
                        "artifact_too_large",
                        "The saved file exceeds the download limit.",
                    )
                destination.write(contents)
            files.append({"key": key, "path": str(path), "bytes": len(contents)})
        return {
            "ok": True,
            "run_id": identity,
            "task_status": status["task_status"],
            "incomplete": status["task_status"] != "succeeded",
            "directory": str(target),
            "files": files,
        }

    def logs(self, identity, follow):
        logs = self.session.client("logs", config=SDK_CONFIG)
        token, settled = None, 0
        while True:
            request = {
                "logGroupName": self.deployment["log_group_name"],
                "logStreamName": identity,
                "startFromHead": True,
            }
            if token:
                request["nextToken"] = token
            try:
                response = logs.get_log_events(**request)
            except ClientError as error:
                if error.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                response = {"events": [], "nextForwardToken": token}
            next_token = response.get("nextForwardToken")
            for event in response.get("events", []):
                emit(
                    {
                        "ok": True,
                        "environment": self.environment.name,
                        "run_id": identity,
                        "event": event,
                    }
                )
            drained = next_token == token
            token = next_token
            if not follow and drained:
                break
            if follow and drained:
                state = self.status(identity)["compute_state"]
                settled = settled + 1 if state == AWS_TERMINATED else 0
                if settled >= LOG_SETTLE_POLLS:
                    break
                time.sleep(LOG_POLL_SECONDS)
        emit(
            {
                "ok": True,
                "environment": self.environment.name,
                "run_id": identity,
                "end_of_stream": True,
            }
        )


def main(argv=None):
    try:
        arguments = build_parser().parse_args(argv)
        supplied = input_spec(arguments) if arguments.command == "submit" else None
        environment = get_environment(arguments.env)
        runs = Runs(load_deployment(environment), environment)
        if arguments.command == "submit":
            result = runs.submit(supplied)
        elif arguments.command == "list":
            result = runs.list(arguments)
        elif arguments.command == "status":
            result = runs.status(arguments.run_id)
        elif arguments.command == "cancel":
            result = runs.cancel(arguments.run_id)
        elif arguments.command == "download":
            result = runs.download(arguments.run_id, arguments.directory)
        else:
            runs.logs(arguments.run_id, arguments.follow)
            return 0
        emit({"environment": environment.name, **result})
        return 0
    except (
        CloudboxError,
        BotoCoreError,
        ClientError,
        OSError,
        ValueError,
        KeyError,
    ) as error:
        emit(error_record(error))
        return 1
    except KeyboardInterrupt:
        emit({"ok": False, "error": {"code": "interrupted"}})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
