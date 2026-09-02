"""Store the GitHub App key outside command arguments and Terraform state."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import BotoCoreError, ClientError

from cloudbox.common import (
    SDK_CONFIG,
    CloudboxError,
    emit,
    error_record,
    load_deployment,
    operator_session,
)
from cloudbox.environments import add_environment_argument, get_environment
from cloudbox.github import MAX_PRIVATE_KEY_BYTES, validate_private_key

CURRENT_SECRET_STAGE = "AWSCURRENT"
GITHUB_SECRET_SUFFIX = "github-app-private-key"


def private_key_from_file(path):
    # Bound the file read and validate RSA before any secret write.
    with path.expanduser().open("rb") as source:
        raw = source.read(MAX_PRIVATE_KEY_BYTES + 1)
    if len(raw) > MAX_PRIVATE_KEY_BYTES:
        raise CloudboxError(
            "github_private_key_invalid", "The GitHub private key file is too large."
        )
    return validate_private_key(raw)


def secret_has_value(session, secret_id):
    # Metadata is enough to reuse a configured key; do not download it during setup.
    response = session.client("secretsmanager", config=SDK_CONFIG).describe_secret(
        SecretId=secret_id
    )
    return not response.get("DeletedDate") and any(
        CURRENT_SECRET_STAGE in stages
        for stages in response.get("VersionIdsToStages", {}).values()
    )


def store_private_key(session, deployment, secret):
    secret_arn = deployment.get("github_private_key_secret_arn")
    if not secret_arn:
        raise CloudboxError(
            "github_not_configured",
            "Configure and apply GitHub settings before loading the key.",
        )
    session.client("secretsmanager", config=SDK_CONFIG).put_secret_value(
        SecretId=secret_arn,
        SecretString=secret,
    )


def main(argv=None):
    try:
        parser = argparse.ArgumentParser(
            description="Store the GitHub App private key in Secrets Manager."
        )
        add_environment_argument(parser)
        parser.add_argument(
            "--key-file",
            type=Path,
            required=True,
            help="Read the private key from this PEM file.",
        )
        arguments = parser.parse_args(argv)
        deployment = load_deployment(get_environment(arguments.env))
        secret = private_key_from_file(arguments.key_file)
        store_private_key(operator_session(deployment), deployment, secret)
        emit({"ok": True, "secret_arn": deployment["github_private_key_secret_arn"]})
        return 0
    except (CloudboxError, BotoCoreError, ClientError, OSError, ValueError) as error:
        emit(error_record(error))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
