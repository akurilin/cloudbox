"""Shared input and AWS access for the cloud spike."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
TERRAFORM_OUTPUT = "cloudbox"
SCHEMA_VERSION = 1
GITHUB_SCHEMA_VERSION = 2
DEFAULT_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 3300
MIN_TIMEOUT_SECONDS = 60
MAX_PROMPT_CHARACTERS = 128_000
MAX_RECORD_BYTES = 1_048_576
ROLE_SESSION_SECONDS = 3600
CREDENTIAL_MARGIN_SECONDS = 60
AWS_POLICY_VERSION = "2012-10-17"
JSON_CONTENT_TYPE = "application/json"
S3_ENCRYPTION = "AES256"
MICROVM_SERVICE = "lambda-microvms"
SDK_CONFIG = Config(
    retries={"mode": "standard", "total_max_attempts": 3},
    connect_timeout=10,
    read_timeout=30,
)
MISSING_OBJECT_CODES = {"NoSuchKey", "NotFound", "404"}
PRECONDITION_FAILED_CODES = {"PreconditionFailed", "412"}
TASK_STATUSES = {"succeeded", "failed", "timed_out", "cancelled", "unknown"}


class CloudboxError(Exception):
    """An error safe to return without AWS request contents."""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def timestamp():
    return datetime.now(UTC).isoformat()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def error_record(error):
    # Do not expose SDK messages: they can contain request values.
    if isinstance(error, CloudboxError):
        return {
            "ok": False,
            "error": {"code": error.code, "message": str(error)},
            **error.details,
        }
    if isinstance(error, ClientError):
        return {
            "ok": False,
            "error": {
                "code": error.response.get("Error", {}).get("Code", "aws_error"),
                "operation": error.operation_name,
            },
        }
    return {"ok": False, "error": {"code": type(error).__name__}}


def load_deployment(environment):
    # Read only the named output; do not refresh state or cache credentials.
    try:
        data = json.loads(
            environment.terraform(
                environment.main_root,
                "output",
                "-json",
                TERRAFORM_OUTPUT,
                capture=True,
            )
        )
        config = json.loads(environment.input_path.read_bytes())["deployment"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CloudboxError(
            "deployment_unavailable", "Read the initialized Terraform output first."
        ) from error
    required = (
        "aws_account_id",
        "aws_region",
        "aws_profile",
        "project_name",
        "bucket_name",
        "provisioner_role_arn",
        "run_data_role_arn",
        "runtime_role_arn",
        "openrouter_secret_arn",
        "log_group_name",
        "image_arn",
    )
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str) or not data[key] for key in required
    ):
        raise CloudboxError("deployment_invalid", "The Terraform output is incomplete.")
    if not re.fullmatch(r"\d{12}", data["aws_account_id"]):
        raise CloudboxError("deployment_invalid", "The AWS account ID is invalid.")
    identity_fields = ("aws_account_id", "aws_region", "aws_profile", "project_name")
    if not isinstance(config, dict) or any(
        data[field] != config.get(field) for field in identity_fields
    ):
        raise CloudboxError(
            "state_mismatch",
            "Terraform output does not match the selected environment inputs.",
        )
    return data


def credential_session(credentials, region):
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


def check_account(session, deployment):
    account = session.client("sts", config=SDK_CONFIG).get_caller_identity()["Account"]
    if account != deployment["aws_account_id"]:
        raise CloudboxError(
            "wrong_account", "AWS credentials belong to another account."
        )


def operator_session(deployment, *, provisioner=True):
    session = boto3.Session(
        profile_name=deployment["aws_profile"], region_name=deployment["aws_region"]
    )
    check_account(session, deployment)
    if not provisioner:
        return session
    # Reuse the restricted provisioner for the spike; do not deploy with SSO admin access.
    response = session.client("sts", config=SDK_CONFIG).assume_role(
        RoleArn=deployment["provisioner_role_arn"],
        RoleSessionName="cloudbox-operator",
        DurationSeconds=ROLE_SESSION_SECONDS,
    )
    session = credential_session(response["Credentials"], deployment["aws_region"])
    check_account(session, deployment)
    return session


def run_prefix(run_id):
    return f"runs/{run_id}/"


def scoped_data_credentials(session, deployment, run_id):
    # Session policy intersects the role policy, so only this prefix is accessible.
    bucket_arn = f"arn:aws:s3:::{deployment['bucket_name']}"
    prefix = run_prefix(run_id)
    policy = {
        "Version": AWS_POLICY_VERSION,
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": f"{bucket_arn}/{prefix}*",
            },
        ],
    }
    response = session.client("sts", config=SDK_CONFIG).assume_role(
        RoleArn=deployment["run_data_role_arn"],
        RoleSessionName=f"run-{run_id}",
        DurationSeconds=ROLE_SESSION_SECONDS,
        Policy=json.dumps(policy, separators=(",", ":")),
    )
    credentials = response["Credentials"]
    check_account(credential_session(credentials, deployment["aws_region"]), deployment)
    return credentials


def put_record(s3, bucket, key, value, *, exclusive=False):
    arguments = {
        "Bucket": bucket,
        "Key": key,
        "Body": json_bytes(value),
        "ContentType": JSON_CONTENT_TYPE,
        "ServerSideEncryption": S3_ENCRYPTION,
    }
    if exclusive:
        arguments["IfNoneMatch"] = "*"
    try:
        s3.put_object(**arguments)
        return True
    except ClientError as error:
        if exclusive and error.response["Error"]["Code"] in PRECONDITION_FAILED_CODES:
            return False
        raise


def get_record(s3, bucket, key):
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in MISSING_OBJECT_CODES:
            return None
        raise
    with response["Body"] as body:
        raw = body.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise CloudboxError("record_invalid", "A run record exceeds the size limit.")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise CloudboxError(
            "record_invalid", "A run record is not valid JSON."
        ) from error
    if not isinstance(value, dict):
        raise CloudboxError("record_invalid", "A run record must be an object.")
    return value


def parse_timeout(value):
    match = re.fullmatch(r"([0-9]+)(s|m|h)?", value)
    if not match:
        raise CloudboxError("invalid_timeout", "Use seconds, or a value such as 10m.")
    unit_seconds = {None: 1, "s": 1, "m": 60, "h": 3600}
    return int(match.group(1)) * unit_seconds[match.group(2)]


def validate_spec(value):
    allowed = {"schema_version", "prompt", "model", "timeout_seconds"}
    if not isinstance(value, dict) or value.keys() - allowed:
        raise CloudboxError("invalid_spec", "The job contains unsupported fields.")
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise CloudboxError("invalid_spec", "The schema version is not supported.")
    prompt = value.get("prompt")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt) > MAX_PROMPT_CHARACTERS
    ):
        raise CloudboxError(
            "invalid_prompt", f"Supply 1 to {MAX_PROMPT_CHARACTERS} prompt characters."
        )
    model = value.get("model")
    if (
        not isinstance(model, str)
        or not model
        or any(character.isspace() for character in model)
    ):
        raise CloudboxError("invalid_model", "Supply a model ID without spaces.")
    timeout = value.get("timeout_seconds")
    if (
        type(timeout) is not int
        or not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS
    ):
        raise CloudboxError(
            "invalid_timeout",
            f"The timeout must be {MIN_TIMEOUT_SECONDS} to {MAX_TIMEOUT_SECONDS} seconds.",
        )
    return value
