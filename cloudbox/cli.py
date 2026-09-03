"""Submit and inspect cloud runs; no local execution mode."""

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from .common import (
    CREDENTIAL_MARGIN_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    GITHUB_SCHEMA_VERSION,
    MAX_PROMPT_CHARACTERS,
    MAX_RECORD_BYTES,
    MAX_TIMEOUT_SECONDS,
    MICROVM_SERVICE,
    RUN_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SDK_CONFIG,
    TASK_STATUSES,
    CloudboxError,
    credential_session,
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
LOG_MAX_PAGES_PER_POLL = 3
LOG_PAGE_SIZE = 100
LOG_FINAL_SETTLE_SECONDS = 10
WAIT_RESULT_GRACE_SECONDS = 30
WAIT_STOP_GRACE_SECONDS = 60
WAIT_MAX_READ_ERRORS = 3
WAIT_SDK_CONFIG = Config(
    retries={"mode": "standard", "total_max_attempts": 1},
    connect_timeout=3,
    read_timeout=5,
)
LINK_SDK_CONFIG = SDK_CONFIG.merge(
    Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"},
    )
)
MAX_ARTIFACTS = 32
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_ARTIFACT_NAME_CHARACTERS = 128
DOWNLOAD_CHUNK_BYTES = 64 * 1024
ARTIFACT_LINK_SECONDS = 3600
AGENT_EVENTS = {
    "model_message",
    "tool_execution_start",
    "tool_execution_end",
    "agent_start",
    "agent_settled",
    "agent_launch",
    "auto_retry_start",
    "auto_retry_end",
}
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*?(?:\x07|\x1b\\))")
IDLE_SUSPEND_SECONDS = 28_800
MAX_HOOK_PAYLOAD_BYTES = 4096
AWS_TERMINATED = "TERMINATED"
UNKNOWN = "unknown"
DOWNLOAD_NAMES = ("spec.json", "launch.json", "result.json", "output/result.json")
REJECTED_LAUNCH_CODES = {
    "AccessDeniedException",
    "UnauthorizedException",
    "ValidationException",
    "ResourceNotFoundException",
    "ServiceQuotaExceededException",
    "TooManyRequestsException",
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
    for name, help_text in (
        ("submit", "Start one cloud run."),
        ("exec", "Start a cloud run, wait, and print its response."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("prompt", nargs="?")
        command.add_argument("--spec", type=Path)
        command.add_argument("--model")
        command.add_argument("--timeout")
    listing = commands.add_parser("list", help="List saved runs.")
    listing.add_argument("--status", choices=sorted(TASK_STATUSES))
    listing.add_argument("--limit", type=int, default=LIST_PAGE_SIZE)
    listing.add_argument("--cursor")
    for name in ("status", "logs", "download", "cancel", "wait", "links"):
        command = commands.add_parser(name)
        command.add_argument("run_id", type=run_id)
        if name == "logs":
            command.add_argument("--follow", action="store_true")
        if name == "download":
            command.add_argument("--output", "--directory", dest="directory", type=Path)
    for name, command in commands.choices.items():
        if name in {"exec", "wait"}:
            command.add_argument("--debug-agent", action="store_true")
            command.add_argument("--debug-supervisor", action="store_true")
        command.add_argument(
            "--json",
            action="store_true",
            help="Print JSON. Other commands use JSON by default.",
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


def terminal_text(value):
    # Keep response layout but remove terminal commands from remote text.
    value = ANSI_ESCAPE.sub("", value)
    return "".join(
        character
        for character in value
        if character in "\n\t" or unicodedata.category(character) not in {"Cc", "Cf"}
    )


def diagnostic(message):
    print(terminal_text(message), file=sys.stderr, flush=True)


def final_response(status):
    result = status.get("result") or {}
    report = result.get("report") or {}
    if isinstance(report, dict):
        for field in ("response", "summary"):
            value = report.get(field)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def artifact_manifest(identity, result):
    artifacts = (result or {}).get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        raise CloudboxError("artifact_invalid", "The file list is invalid.")
    total, keys = 0, set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise CloudboxError("artifact_invalid", "A file record is invalid.")
        name, key = item.get("name"), item.get("key")
        size, digest = item.get("bytes"), item.get("sha256")
        if (
            not isinstance(name, str)
            or len(name) > MAX_ARTIFACT_NAME_CHARACTERS
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
            or not isinstance(key, str)
            or type(size) is not int
            or not 0 <= size <= MAX_ARTIFACT_BYTES
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise CloudboxError("artifact_invalid", "A file record is invalid.")
        prefix = run_prefix(identity) + "artifacts/"
        parts = key.removeprefix(prefix).split("/")
        if (
            not key.startswith(prefix)
            or len(parts) != 2
            or parts[1] != name
            or key in keys
        ):
            raise CloudboxError("artifact_invalid", "A file key is outside this run.")
        try:
            run_id(parts[0])
        except CloudboxError as error:
            raise CloudboxError("artifact_invalid", "A file key is invalid.") from error
        keys.add(key)
        total += size
    if total > MAX_TOTAL_ARTIFACT_BYTES:
        raise CloudboxError("artifact_invalid", "The files exceed the download limit.")
    return artifacts


class RunLogStream:
    def __init__(self, runs, identity, *, agent, supervisor):
        self.client = runs.session.client("logs", config=WAIT_SDK_CONFIG)
        self.group = runs.deployment["log_group_name"]
        self.identity = identity
        self.sources = {
            source
            for source, enabled in (("agent", agent), ("supervisor", supervisor))
            if enabled
        }
        self.token = None
        self.failed = False

    def poll(self):
        # Limit each log read so continuous output cannot delay status checks.
        for _ in range(LOG_MAX_PAGES_PER_POLL):
            request = {
                "logGroupName": self.group,
                "logStreamName": self.identity,
                "startFromHead": True,
                "limit": LOG_PAGE_SIZE,
            }
            if self.token:
                request["nextToken"] = self.token
            try:
                response = self.client.get_log_events(**request)
            except (BotoCoreError, ClientError) as error:
                if (
                    isinstance(error, ClientError)
                    and error.response.get("Error", {}).get("Code")
                    == "ResourceNotFoundException"
                ):
                    return True
                if not self.failed:
                    diagnostic("Logs unavailable; result checks continue.")
                self.failed = True
                return True
            self.failed = False
            for event in response.get("events", []):
                try:
                    record = json.loads(event.get("message", ""))
                except (ValueError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                source = record.get("source")
                if source not in {"agent", "supervisor"}:
                    source = (
                        "agent" if record.get("event") in AGENT_EVENTS else "supervisor"
                    )
                if source in self.sources:
                    diagnostic(
                        f"[{source}] "
                        + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    )
            next_token = response.get("nextForwardToken")
            drained = next_token == self.token
            self.token = next_token
            if drained:
                return True
        return False

    def settle(self):
        # CloudWatch may deliver final events after the VM has stopped.
        deadline = time.monotonic() + LOG_FINAL_SETTLE_SECONDS
        settled = 0
        while time.monotonic() < deadline:
            settled = settled + 1 if self.poll() else 0
            if self.failed or settled >= LOG_SETTLE_POLLS:
                return
            time.sleep(LOG_POLL_SECONDS)
        diagnostic("Log wait ended; use logs --follow to check later events.")


class Runs:
    def __init__(self, deployment, environment):
        self.deployment = deployment
        self.environment = environment
        self.session = operator_session(deployment)
        self.s3 = self.session.client("s3", config=SDK_CONFIG)
        self.compute = self.session.client(MICROVM_SERVICE, config=SDK_CONFIG)
        # Short read timeouts keep local waits bounded during service failures.
        self.status_s3 = self.session.client("s3", config=WAIT_SDK_CONFIG)
        self.status_compute = self.session.client(
            MICROVM_SERVICE, config=WAIT_SDK_CONFIG
        )
        self.bucket = deployment["bucket_name"]

    def record(self, identity, name, *, client=None):
        return get_record(client or self.s3, self.bucket, run_prefix(identity) + name)

    def submit(self, supplied, *, on_run_id=None):
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
        # User inputs keep schema 1; every new worker run uses finish reporting.
        spec["schema_version"] = RUN_SCHEMA_VERSION
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
        if on_run_id is not None:
            on_run_id(identity)
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
                "data_credentials_expires_at": credentials["Expiration"].isoformat(),
                "openrouter_secret_arn": deployment["openrouter_secret_arn"],
                "log_group_name": deployment["log_group_name"],
                "aws_region": deployment["aws_region"],
            }
            access = prepare_github_access(self.session, deployment)
            if access:
                spec["github"] = access.github
                payload.update(
                    {
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
                rejected = (
                    isinstance(error, ParamValidationError)
                    or isinstance(error, ClientError)
                    and error.response.get("ResponseMetadata", {}).get("RetryAttempts")
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
        s3 = getattr(self, "status_s3", self.s3)
        compute = getattr(self, "status_compute", self.compute)
        result = self.record(identity, "result.json", client=s3)
        launch = self.record(identity, "launch.json", client=s3)
        spec = self.record(identity, "spec.json", client=s3)
        state, compute_error = UNKNOWN, None
        if launch and launch.get("microvm_id"):
            try:
                response = compute.get_microvm(microvmIdentifier=launch["microvm_id"])
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
            "timeout_seconds": spec.get("timeout_seconds") if spec else None,
            "result": result,
            "launch": launch,
        }

    def wait(self, identity, *, debug_agent=False, debug_supervisor=False, launch=None):
        stream = (
            RunLogStream(self, identity, agent=debug_agent, supervisor=debug_supervisor)
            if debug_agent or debug_supervisor
            else None
        )
        started = time.monotonic()
        deadline = started + MAX_TIMEOUT_SECONDS + WAIT_STOP_GRACE_SECONDS
        deadline_loaded = False
        result_seen, stopped_seen, missing_seen = None, None, None
        read_errors = 0
        current = {
            "ok": False,
            "run_id": identity,
            "task_status": UNKNOWN,
            "compute_state": UNKNOWN,
            "result": None,
        }
        while True:
            try:
                current = self.status(identity)
                # Use the launch response if its S3 record could not be saved.
                if not current.get("launch") and launch and launch.get("microvm_id"):
                    compute = getattr(self, "status_compute", self.compute)
                    vm = compute.get_microvm(microvmIdentifier=launch["microvm_id"])
                    current["compute_state"] = vm.get("state", UNKNOWN)
                    current["compute_error"] = None
                read_errors = 0
            except (BotoCoreError, ClientError, CloudboxError) as error:
                read_errors += 1
                if read_errors >= WAIT_MAX_READ_ERRORS:
                    return self.wait_failure(
                        current,
                        "status_unavailable",
                        "Run status is unavailable. Use wait with this run ID later.",
                        stream,
                        cause=error_record(error)["error"]["code"],
                    )
            if stream:
                stream.poll()
            now = time.monotonic()
            timeout = current.get("timeout_seconds")
            if (
                not deadline_loaded
                and type(timeout) is int
                and 0 < timeout <= MAX_TIMEOUT_SECONDS
            ):
                remaining = timeout
                try:
                    submitted = datetime.fromisoformat(current["submitted_at"])
                    remaining -= (datetime.now(UTC) - submitted).total_seconds()
                except (ValueError, TypeError, KeyError):
                    pass
                deadline = (
                    now + max(0, min(timeout, remaining)) + WAIT_STOP_GRACE_SECONDS
                )
                deadline_loaded = True
            result = current.get("result")
            stopped = current["compute_state"] == AWS_TERMINATED
            if result is not None and stopped:
                if stream:
                    stream.settle()
                return {**current, "ok": current["task_status"] == "succeeded"}
            if result is not None:
                result_seen = now if result_seen is None else result_seen
                if now - result_seen >= WAIT_STOP_GRACE_SECONDS:
                    return self.wait_failure(
                        current, "vm_not_stopped", "The job VM has not stopped.", stream
                    )
            if stopped and result is None:
                stopped_seen = now if stopped_seen is None else stopped_seen
                if now - stopped_seen >= WAIT_RESULT_GRACE_SECONDS:
                    return self.wait_failure(
                        current,
                        "missing_result",
                        "The job VM stopped without a saved result.",
                        stream,
                    )
            if (
                result is None
                and not current.get("launch")
                and not launch
                and not read_errors
            ):
                missing_seen = now if missing_seen is None else missing_seen
                if now - missing_seen >= WAIT_RESULT_GRACE_SECONDS:
                    return self.wait_failure(
                        current,
                        "launch_unknown",
                        "No saved launch is available. Inspect this run before resubmitting.",
                        stream,
                    )
            else:
                missing_seen = None
            if now >= deadline:
                return self.wait_failure(
                    current,
                    "wait_deadline",
                    "The run deadline passed without a confirmed result and VM stop.",
                    stream,
                )
            time.sleep(LOG_POLL_SECONDS)

    @staticmethod
    def wait_failure(current, code, message, stream, **details):
        if stream:
            stream.settle()
        return {
            **current,
            "ok": False,
            "wait_error": {"code": code, "message": message, **details},
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
            schema_version = launch.get("schema_version")
            if schema_version is None:
                schema_version = (self.record(identity, "spec.json") or {}).get(
                    "schema_version", SCHEMA_VERSION
                )
            result = {
                "schema_version": schema_version,
                "run_id": identity,
                "status": "cancelled",
                "reason": "operator_cancelled",
                "started_at": launch.get("started_at"),
                "finished_at": timestamp(),
                "exit_code": None,
                "usage": {},
            }
            if schema_version in (SCHEMA_VERSION, GITHUB_SCHEMA_VERSION):
                result.update(artifact_key=None, artifact_complete=False)
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
        artifacts = artifact_manifest(identity, status.get("result"))
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
        for artifact in artifacts:
            key = artifact["key"]
            path = target / key.removeprefix(run_prefix(identity))
            path.parent.mkdir(parents=True, exist_ok=True)
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            size, digest = 0, hashlib.sha256()
            with response["Body"] as body, path.open("xb") as destination:
                while chunk := body.read(DOWNLOAD_CHUNK_BYTES):
                    size += len(chunk)
                    if size > artifact["bytes"]:
                        raise CloudboxError(
                            "artifact_size_mismatch", "A file exceeds its saved size."
                        )
                    digest.update(chunk)
                    destination.write(chunk)
            if size != artifact["bytes"] or digest.hexdigest() != artifact["sha256"]:
                raise CloudboxError(
                    "artifact_integrity_error",
                    "A file does not match its saved record.",
                )
            files.append({"key": key, "path": str(path), "bytes": size})
        return {
            "ok": True,
            "run_id": identity,
            "task_status": status["task_status"],
            "incomplete": status["task_status"] != "succeeded",
            "directory": str(target),
            "files": files,
        }

    def links(self, identity):
        # Get new credentials so old CLI sessions cannot produce expired links.
        session = operator_session(self.deployment)
        s3 = session.client("s3", config=SDK_CONFIG)
        result = get_record(s3, self.bucket, run_prefix(identity) + "result.json")
        if result is None:
            raise CloudboxError("missing_result", "No saved result is available.")
        artifacts = artifact_manifest(identity, result)
        if not artifacts:
            return {"ok": True, "run_id": identity, "artifacts": []}
        credentials = scoped_data_credentials(session, self.deployment, identity)
        signer = credential_session(credentials, self.deployment["aws_region"]).client(
            "s3", config=LINK_SDK_CONFIG
        )
        now = datetime.now(UTC)
        lifetime = min(
            ARTIFACT_LINK_SECONDS,
            int((credentials["Expiration"] - now).total_seconds())
            - CREDENTIAL_MARGIN_SECONDS,
        )
        if lifetime <= 0:
            raise CloudboxError("credentials_expired", "The new credentials expired.")
        expires_at = (now + timedelta(seconds=lifetime)).isoformat()
        return {
            "ok": True,
            "run_id": identity,
            "artifacts": [
                {
                    **artifact,
                    "url": signer.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": self.bucket, "Key": artifact["key"]},
                        ExpiresIn=lifetime,
                    ),
                    "expires_at": expires_at,
                }
                for artifact in artifacts
            ],
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
    arguments, identity = None, None

    def announce_run(value):
        nonlocal identity
        if identity != value:
            identity = value
            diagnostic(f"Run ID: {value}")

    try:
        arguments = build_parser().parse_args(argv)
        blocking = arguments.command in {"exec", "wait"}
        supplied = (
            input_spec(arguments) if arguments.command in {"submit", "exec"} else None
        )
        if arguments.command == "wait":
            announce_run(arguments.run_id)
        environment = get_environment(arguments.env)
        runs = Runs(load_deployment(environment), environment)
        if arguments.command == "submit":
            result = runs.submit(supplied)
        elif blocking:
            launch = None
            if arguments.command == "exec":
                launch = runs.submit(supplied, on_run_id=announce_run)
                announce_run(launch["run_id"])
            result = runs.wait(
                identity,
                debug_agent=arguments.debug_agent,
                debug_supervisor=arguments.debug_supervisor,
                launch=launch,
            )
            result = {"environment": environment.name, **result}
            if arguments.json:
                emit(result)
            else:
                response = final_response(result)
                if response:
                    print(terminal_text(response), flush=True)
                elif result["ok"]:
                    diagnostic("The saved result has no response text.")
                    return 1
            if result.get("wait_error"):
                diagnostic(result["wait_error"]["message"])
            elif not result["ok"]:
                reason = (result.get("result") or {}).get("reason", "no result")
                diagnostic(f"Run {result['task_status']}: {reason}")
            return 0 if result["ok"] else 1
        elif arguments.command == "list":
            result = runs.list(arguments)
        elif arguments.command == "status":
            result = runs.status(arguments.run_id)
        elif arguments.command == "cancel":
            result = runs.cancel(arguments.run_id)
        elif arguments.command == "download":
            result = runs.download(arguments.run_id, arguments.directory)
        elif arguments.command == "links":
            result = runs.links(arguments.run_id)
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
        record = error_record(error)
        if arguments and arguments.command in {"exec", "wait"}:
            if record.get("run_id"):
                announce_run(record["run_id"])
            if identity:
                record["run_id"] = identity
            if arguments.json:
                emit(record)
            else:
                diagnostic(record["error"].get("message", record["error"]["code"]))
        else:
            emit(record)
        return 1
    except KeyboardInterrupt:
        record = {"ok": False, "error": {"code": "interrupted"}}
        if identity:
            record["run_id"] = identity
        if arguments and arguments.command in {"exec", "wait"}:
            diagnostic("Local command stopped. Check the run status.")
            if identity:
                diagnostic(f"Run ID: {identity}")
            if arguments.json:
                emit(record)
        else:
            emit(record)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
