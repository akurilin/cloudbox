"""Run Pi once, save its JSON artifact, and stop the current AWS MicroVM."""

import base64
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from github_access import (
    child_environment, github_contract, github_environment, revoke_token, validate_github_spec,
)

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 3300
CLEANUP_SECONDS = 30
STOP_GRACE_SECONDS = 3
MAX_PROMPT_CHARACTERS = 128_000
MAX_RESULT_BYTES = 1024 * 1024
MAX_SPEC_BYTES = MAX_RESULT_BYTES
MAX_BLOCKED_REASON_CHARACTERS = 1024
STDERR_CHUNK_BYTES = 8192
MAX_TRACE_TEXT_BYTES = 8 * 1024
MAX_TRACE_RECORD_BYTES = 16 * 1024
TRACE_ENVELOPE_BYTES = 512
MAX_TRACE_METADATA_BYTES = MAX_TRACE_RECORD_BYTES - TRACE_ENVELOPE_BYTES
MAX_TRACE_DEPTH = 12
MAX_TRACE_ITEMS = 64
MAX_TRACE_NODES = 512
MAX_SECRET_FIELD_CHARACTERS = 128
REDACTED = "[redacted]"
OMITTED = "[omitted]"
TRUNCATED = "[truncated]"
HIDDEN_TRACE_TYPES = {"thinking", "reasoning", "redacted_thinking", "image", "audio", "video", "binary"}
HIDDEN_TRACE_FIELDS = {
    "thinking", "reasoning", "reasoningdetails", "thinkingsignature", "textsignature", "signature",
    "encryptedcontent", "image", "audio", "video", "binary",
}
SECRET_FIELD_PATTERN = re.compile(
    r"password|passwd|secret|credential|authorization|apikey|accesskey|sessiontoken|refreshtoken|"
    r"accesstoken|ghtoken|githubtoken|privatekey|amzsignature|^token$",
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )?PRIVATE KEY-----|$)", re.DOTALL),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,}|sk-[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9_./+=-]+", re.IGNORECASE),
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf'''(?<![A-Za-z0-9_.-])(?P<key>["']?[A-Za-z0-9_.-]{{0,{MAX_SECRET_FIELD_CHARACTERS}}}'''
    r'''(?:password|passwd|secret|credential|authorization|api[_.-]?key|access[_.-]?key|token|'''
    rf'''private[_.-]?key|amz[_.-]?signature)[A-Za-z0-9_.-]{{0,{MAX_SECRET_FIELD_CHARACTERS}}}["']?)\s*[:=]\s*'''
    r'''(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]\[{"'&]+)''',
    re.IGNORECASE,
)
URL_CREDENTIAL_PATTERN = re.compile(r"(https?://)[^/\s:@]+:[^@\s/]+@", re.IGNORECASE)
REASONING_TAG_PATTERN = re.compile(r"<(?:think|thinking|reasoning)>.*?(?:</(?:think|thinking|reasoning)>|$)", re.DOTALL | re.IGNORECASE)
OUTPUT_PATH = Path("output/result.json")
PROVIDER = "openrouter"
APPLICATION_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path("/tmp/cloudbox")
TOKEN_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
KNOWN_TOOLS = {"read", "bash", "edit", "write", "grep", "find", "ls"}
AWS_CONFIG = Config(
    connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 1}
)
LOG_LOCK = threading.Lock()
AGENT_CONTRACT = (
    "You are an unattended worker. Complete the user's task in the current directory. "
    f"Write the requested result as valid JSON to {OUTPUT_PATH}. Do not write AWS records. "
    "Do not inspect or print credentials. The result file must be non-empty and at most "
    f"{MAX_RESULT_BYTES} bytes. Your final reply must be only the JSON object "
    '{"status":"completed"}. If you cannot complete the task, do not wait for input; '
    'reply only {"status":"blocked","reason":"short reason"}.'
    " Use only access granted to this run. Do not discover or take credentials, exploit systems, "
    "or bypass access controls. You can install public dependencies inside the sandbox. "
    "If a required tool or permission cannot be obtained through authorized means, return "
    "the blocked status with the missing requirement in reason."
)


def utc_now():
    return datetime.now(UTC).isoformat()


def emit(run_id, event_type, **metadata):
    # Pi content passes the activity filter before managed CloudWatch forwarding.
    record = {"run_id": run_id, "timestamp": utc_now(), "event": event_type, **metadata}
    with LOG_LOCK:
        print(json.dumps(record, separators=(",", ":"), allow_nan=False), flush=True)


def put_json(s3, bucket, key, value):
    # The first terminal record wins if cancel and normal completion race.
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(value, allow_nan=False).encode(),
            ContentType="application/json",
            IfNoneMatch="*",
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == "PreconditionFailed":
            return False
        raise


def number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def field_name(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def secret_field(value):
    return bool(SECRET_FIELD_PATTERN.search(field_name(value)))


def runtime_trace_secrets(session, credentials):
    # Use the normal SDK provider; never read credential files for logging.
    values = list(credentials.values())
    try:
        source = session.get_credentials()
        if source is not None:
            frozen = source.get_frozen_credentials()
            values.extend((frozen.access_key, frozen.secret_key, frozen.token))
    except Exception:
        # Known data and environment credentials still pass through redaction.
        pass
    return tuple(value for value in values if isinstance(value, str) and value)


class ActivityFilter:
    def __init__(self, secrets):
        variants = set()
        for value in secrets:
            if isinstance(value, str) and value:
                variants.update((value, quote(value, safe=""), json.dumps(value)[1:-1],
                                 base64.b64encode(value.encode()).decode(),
                                 base64.urlsafe_b64encode(value.encode()).decode()))
        self.secrets = sorted(variants, key=len, reverse=True)
        self.truncated = False
        self.nodes_left = MAX_TRACE_NODES

    def text(self, value):
        # Redact the full value first, so truncation cannot expose half a token.
        for secret in self.secrets:
            value = value.replace(secret, REDACTED)
        value = REASONING_TAG_PATTERN.sub(OMITTED, value)
        for pattern in SECRET_VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        value = URL_CREDENTIAL_PATTERN.sub(r"\1" + REDACTED + "@", value)

        def redact_assignment(match):
            if secret_field(match.group("key")):
                return match.group()[:match.start("value") - match.start()] + REDACTED
            return match.group()

        value = SECRET_ASSIGNMENT_PATTERN.sub(redact_assignment, value)
        raw = value.encode("utf-8", errors="replace")
        if len(raw) > MAX_TRACE_TEXT_BYTES:
            self.truncated = True
            return raw[:MAX_TRACE_TEXT_BYTES - len(TRUNCATED)].decode("utf-8", errors="ignore") + TRUNCATED
        return value

    def value(self, value, depth=0):
        self.nodes_left -= 1
        if depth > MAX_TRACE_DEPTH or self.nodes_left < 0:
            self.truncated = True
            return TRUNCATED
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, bool) or number(value):
            return value
        if isinstance(value, dict):
            if isinstance(value.get("type"), str) and value["type"] in HIDDEN_TRACE_TYPES:
                return OMITTED
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= MAX_TRACE_ITEMS or self.nodes_left <= 0:
                    self.truncated = True
                    result["trace_truncated"] = True
                    break
                if not isinstance(key, str) or field_name(key) in HIDDEN_TRACE_FIELDS:
                    continue
                result[self.text(key)] = REDACTED if secret_field(key) else self.value(item, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            result = []
            for index, item in enumerate(value):
                if index >= MAX_TRACE_ITEMS or self.nodes_left <= 0:
                    self.truncated = True
                    result.append(TRUNCATED)
                    break
                result.append(self.value(item, depth + 1))
            return result
        # Do not call repr: unknown objects and binary values can contain secrets.
        return OMITTED

    def record(self, metadata):
        self.truncated = False
        self.nodes_left = MAX_TRACE_NODES
        try:
            safe = self.value(metadata)
            if self.truncated:
                safe["trace_truncated"] = True
            encoded = json.dumps(safe, separators=(",", ":"), allow_nan=False)
            if len(encoded.encode()) <= MAX_TRACE_METADATA_BYTES:
                return safe
            # Escaped Unicode and many short fields can exceed the per-value cap.
            preview = encoded[:MAX_TRACE_TEXT_BYTES]
            while True:
                fallback = {"trace_preview": preview + TRUNCATED, "trace_truncated": True}
                if len(json.dumps(fallback).encode()) <= MAX_TRACE_METADATA_BYTES:
                    return fallback
                preview = preview[:len(preview) // 2]
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return {"trace_unavailable": True}


def visible_content(value):
    if not isinstance(value, list):
        return []
    return [{"type": "text", "text": item["text"]} for item in value
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]


def visible_result(value):
    if not isinstance(value, dict):
        return {"unavailable": True}
    result = {"content": visible_content(value.get("content"))}
    if "details" in value:
        result["details"] = value["details"]
    return result


class PiEvents:
    def __init__(self, run_id, secrets=()):
        self.run_id = run_id
        self.activity_filter = ActivityFilter(secrets)
        self.final_message = None
        self.usage = {key: 0 for key in TOKEN_FIELDS}
        self.usage["estimated_cost_usd"] = 0
        self.seen_messages = set()
        self.tools_started = {}

    def trace(self, event_type, **metadata):
        emit(self.run_id, event_type, **self.activity_filter.record(metadata))

    def read_stdout(self, stream):
        for line in stream:
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    self.accept(event)
            except (ValueError, TypeError, KeyError):
                # Pi diagnostics are not task status and can contain model text.
                continue

    def accept(self, event):
        event_type = event.get("type")
        if event_type == "message_end":
            message = event.get("message", {})
            if message.get("role") != "assistant":
                return
            self.final_message = message
            identity = message.get("responseId") or message.get("timestamp")
            if identity is not None and identity in self.seen_messages:
                return
            if identity is not None:
                self.seen_messages.add(identity)
            usage = message.get("usage", {})
            selected_usage = {
                key: usage[key] for key in TOKEN_FIELDS if number(usage.get(key))
            }
            for key, value in selected_usage.items():
                self.usage[key] += value
            cost = usage.get("cost", {}).get("total")
            if number(cost):
                self.usage["estimated_cost_usd"] += cost
            text = "".join(item["text"] for item in visible_content(message.get("content")))
            metadata = {
                "stop_reason": message.get("stopReason"),
                "usage": selected_usage,
                "text": text,
            }
            if isinstance(message.get("errorMessage"), str):
                metadata["error_message"] = message["errorMessage"]
            self.trace("model_message", **metadata)
        elif event_type in {"tool_execution_start", "tool_execution_end"}:
            tool_id = event.get("toolCallId")
            tool_name = event.get("toolName")
            if tool_name not in KNOWN_TOOLS:
                tool_name = "other"
            metadata = {"tool_call_id": tool_id, "tool_name": tool_name}
            if event_type == "tool_execution_start":
                self.tools_started[tool_id] = time.monotonic()
                metadata["arguments"] = event.get("args")
            else:
                started = self.tools_started.pop(tool_id, None)
                metadata["outcome"] = "error" if event.get("isError") else "ok"
                if started is not None:
                    metadata["duration_seconds"] = round(time.monotonic() - started, 3)
                metadata["result"] = visible_result(event.get("result"))
            self.trace(event_type, **metadata)
        elif event_type in {
            "agent_start",
            "agent_settled",
            "auto_retry_start",
            "auto_retry_end",
        }:
            metadata = {
                key: event[key] for key in ("attempt", "success") if key in event
            }
            self.trace(event_type, **metadata)

    def completion(self):
        message = self.final_message
        if not message or message.get("stopReason") != "stop":
            return "failed", "agent_terminal_error", None
        text = "".join(
            item.get("text", "")
            for item in message.get("content", [])
            if item.get("type") == "text"
        )
        try:
            declaration = json.loads(text)
        except (ValueError, TypeError):
            return "failed", "invalid_completion_signal", None
        if declaration == {"status": "completed"}:
            return "succeeded", "completed", None
        if isinstance(declaration, dict) and declaration.get("status") == "blocked":
            reason = declaration.get("reason")
            if isinstance(reason, str) and reason.strip():
                return "failed", "agent_blocked", reason[:MAX_BLOCKED_REASON_CHARACTERS]
        return "failed", "invalid_completion_signal", None


def stop_process(process):
    # Kill the process group so shell tools do not outlive Pi or a timed-out hook.
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        if process.poll() is None:
            process.wait(timeout=STOP_GRACE_SECONDS)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=STOP_GRACE_SECONDS)


def run_script(name, workspace, deadline, run_id):
    emit(run_id, "lifecycle_start", script=name)
    process = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(name, 0)
        process = subprocess.Popen(
            ["/bin/sh", str(APPLICATION_DIR / name)],
            cwd=workspace,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        exit_code = process.wait(timeout=remaining)
        emit(run_id, "lifecycle_end", script=name, exit_code=exit_code)
        if exit_code:
            raise RuntimeError("lifecycle_failed")
    finally:
        stop_process(process)


def run_pi(spec, key, workspace, deadline, run_id, access_environment=None, redaction_secrets=()):
    environment = child_environment()
    environment.update(
        {
            "OPENROUTER_API_KEY": key,
            "PI_CODING_AGENT_DIR": str(workspace / ".pi" / "agent"),
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "CLOUDBOX_RESULT_PATH": str(workspace / OUTPUT_PATH),
        }
    )
    environment.update(access_environment or {})
    secrets = [*redaction_secrets, key]
    secrets.extend(value for name, value in environment.items() if secret_field(name))
    events = PiEvents(run_id, secrets)
    command = [
        "pi",
        "--mode",
        "json",
        "--no-session",
        "--offline",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
        "--provider",
        PROVIDER,
        "--model",
        spec["model"],
        "--append-system-prompt",
        AGENT_CONTRACT + github_contract(spec),
    ]
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )

    def drain_stderr():
        while process.stderr.read(STDERR_CHUNK_BYTES):
            pass

    def send_prompt():
        # A stuck Pi startup must not block the supervisor while it writes stdin.
        try:
            process.stdin.write(spec["prompt"])
            process.stdin.close()
        except BrokenPipeError:
            pass

    readers = [
        threading.Thread(
            target=events.read_stdout, args=(process.stdout,), daemon=True
        ),
        threading.Thread(target=drain_stderr, daemon=True),
        threading.Thread(target=send_prompt, daemon=True),
    ]
    for reader in readers:
        reader.start()
    events.trace("agent_launch", model=spec["model"], provider=PROVIDER)
    timed_out = False
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("pi", 0)
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        timed_out = True
        emit(run_id, "deadline")
    finally:
        stop_process(process)
        for reader in readers:
            reader.join(timeout=STOP_GRACE_SECONDS)
    emit(run_id, "agent_exit", exit_code=process.returncode)
    return process.returncode, timed_out, events


def reject_json_constant(_value):
    raise ValueError("Non-finite JSON number")


def artifact_bytes(workspace):
    path = workspace / OUTPUT_PATH
    if path.is_symlink() or not path.is_file():
        return None, "missing_output"
    with path.open("rb") as stream:
        body = stream.read(MAX_RESULT_BYTES + 1)
    if not body:
        return None, "empty_output"
    if len(body) > MAX_RESULT_BYTES:
        return None, "oversized_output"
    try:
        json.loads(body.decode("utf-8"), parse_constant=reject_json_constant)
    except (ValueError, UnicodeError):
        return body, "invalid_output_json"
    return body, None


def validate_spec(spec):
    validate_github_spec(spec)
    prompt = spec.get("prompt")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt) > MAX_PROMPT_CHARACTERS
    ):
        raise ValueError("invalid_prompt")
    if not isinstance(spec.get("model"), str) or not spec["model"]:
        raise ValueError("invalid_model")
    timeout = spec.get("timeout_seconds")
    if type(timeout) is not int or not CLEANUP_SECONDS < timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError("invalid_timeout")


def supervise(microvm_id, payload):
    run_id = payload["run_id"]
    bucket = payload["bucket_name"]
    prefix = f"runs/{run_id}"
    started_at = utc_now()
    started = time.monotonic()
    deadline = started + DEFAULT_TIMEOUT_SECONDS
    workspace = WORKSPACE_ROOT / run_id
    s3 = None
    events = None
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "failed",
        "reason": "worker_error",
        "started_at": started_at,
        "exit_code": None,
        "artifact_key": None,
        "artifact_complete": False,
        "agent_version": os.environ.get("PI_VERSION"),
    }
    # Create AWS clients after restore. Data credentials cannot grant runtime actions.
    runtime = boto3.Session(region_name=payload["aws_region"])
    try:
        credentials = payload["data_credentials"]
        data_session = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=payload["aws_region"],
        )
        s3 = data_session.client("s3", config=AWS_CONFIG)
        response = s3.get_object(Bucket=bucket, Key=f"{prefix}/spec.json")
        try:
            spec_bytes = response["Body"].read(MAX_SPEC_BYTES + 1)
        finally:
            response["Body"].close()
        if len(spec_bytes) > MAX_SPEC_BYTES:
            raise ValueError("oversized_spec")
        spec = json.loads(spec_bytes)
        validate_spec(spec)
        deadline = started + spec["timeout_seconds"]
        result["schema_version"] = spec["schema_version"]
        access_environment = github_environment(spec, payload, workspace, deadline)
        put_json(
            s3,
            bucket,
            f"{prefix}/launch.json",
            {
                "schema_version": spec["schema_version"],
                "run_id": run_id,
                "microvm_id": microvm_id,
                "image_arn": spec["image_arn"],
                "image_version": spec["image_version"],
                "log_group_name": payload["log_group_name"],
                "log_stream_name": run_id,
                "started_at": started_at,
            },
        )
        workspace.mkdir(parents=True, exist_ok=False)
        (workspace / OUTPUT_PATH.parent).mkdir()
        emit(run_id, "worker_start", microvm_id=microvm_id)
        secret = runtime.client("secretsmanager", config=AWS_CONFIG).get_secret_value(
            SecretId=payload["openrouter_secret_arn"],
        )
        key = secret.get("SecretString", "").strip()
        if not key or key.startswith("{"):
            raise ValueError("secret_must_be_plain_api_key")
        work_deadline = deadline - CLEANUP_SECONDS
        run_script("startup.sh", workspace, work_deadline, run_id)
        exit_code, timed_out, events = run_pi(
            spec,
            key,
            workspace,
            work_deadline,
            run_id,
            access_environment,
            runtime_trace_secrets(runtime, credentials),
        )
        result["exit_code"] = exit_code
        if timed_out:
            result.update(status="timed_out", reason="deadline")
        elif exit_code:
            result.update(status="failed", reason="agent_exit_error")
        else:
            status, reason, blocked_reason = events.completion()
            result.update(status=status, reason=reason)
            if blocked_reason:
                result["blocked_reason"] = blocked_reason
    except subprocess.TimeoutExpired:
        result.update(status="timed_out", reason="deadline")
    except Exception as error:
        # Exception text can include provider responses. Emit only its type.
        emit(run_id, "worker_error", error_type=type(error).__name__)
    finally:
        try:
            if s3 is not None:
                body, output_error = artifact_bytes(workspace)
                if output_error and result["status"] == "succeeded":
                    result.update(status="failed", reason=output_error)
                emit(run_id, "output_validation", outcome=output_error or "valid")
                if body is not None:
                    artifact_key = f"{prefix}/{OUTPUT_PATH}"
                    s3.put_object(
                        Bucket=bucket,
                        Key=artifact_key,
                        Body=body,
                        ContentType="application/json",
                    )
                    result["artifact_key"] = artifact_key
                    result["artifact_complete"] = result["status"] == "succeeded"
                    emit(run_id, "output_uploaded", size_bytes=len(body))
        except Exception as error:
            if result["status"] == "succeeded":
                result.update(status="failed", reason="output_upload_error")
            emit(run_id, "output_error", error_type=type(error).__name__)
        if payload.get("github_token"):
            result["github_token_revoked"] = revoke_token(payload["github_token"], deadline - STOP_GRACE_SECONDS)
            emit(run_id, "github_token_revocation", confirmed=result["github_token_revoked"])
        try:
            if workspace.exists():
                run_script(
                    "teardown.sh", workspace, deadline - STOP_GRACE_SECONDS, run_id
                )
        except Exception as error:
            result["cleanup_error"] = type(error).__name__
            emit(run_id, "cleanup_error", error_type=type(error).__name__)
        result["finished_at"] = utc_now()
        result["usage"] = events.usage if events else {}
        try:
            if s3 is not None:
                saved = put_json(s3, bucket, f"{prefix}/result.json", result)
                emit(run_id, "result_saved", status=result["status"], written=saved)
        except Exception as error:
            emit(run_id, "result_upload_error", error_type=type(error).__name__)
        finally:
            try:
                emit(run_id, "terminate_requested", microvm_id=microvm_id)
                runtime.client("lambda-microvms", config=AWS_CONFIG).terminate_microvm(
                    microvmIdentifier=microvm_id,
                )
            except Exception as error:
                # AWS maximumDurationInSeconds remains the independent hard stop.
                emit(run_id, "terminate_error", error_type=type(error).__name__)
