"""Set the API key without command-line or state exposure."""

import argparse
import getpass
import sys
import warnings
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

KEY_NAME = "OPENROUTER_API_KEY"
MAX_KEY_FILE_BYTES = 16_384


def key_from_file(path):
    # Parse only the key; never execute or expand dotenv text.
    with path.open("rb") as source:
        raw = source.read(MAX_KEY_FILE_BYTES + 1)
    if len(raw) > MAX_KEY_FILE_BYTES:
        raise CloudboxError("invalid_secret_file", "The key file is too large.")
    lines = [
        line.strip()
        for line in raw.decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) == 1 and "=" not in lines[0]:
        return lines[0]
    matches = []
    for line in lines:
        line = line.removeprefix("export ").strip()
        name, separator, value = line.partition("=")
        if separator and name.strip() == KEY_NAME:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            matches.append(value)
    if len(matches) != 1:
        raise CloudboxError(
            "invalid_secret_file",
            "Supply one OpenRouter key or one OPENROUTER_API_KEY entry.",
        )
    return matches[0]


def main(argv=None):
    try:
        parser = argparse.ArgumentParser(
            description="Store the OpenRouter key in Secrets Manager."
        )
        add_environment_argument(parser)
        parser.add_argument("--env-file", type=Path)
        arguments = parser.parse_args(argv)
        deployment = load_deployment(get_environment(arguments.env))
        session = operator_session(deployment)
        if arguments.env_file:
            secret = key_from_file(arguments.env_file)
        else:
            # Refuse echoed input; the key never enters an argument or Terraform state.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                secret = getpass.getpass("OpenRouter API key: ")
        if not secret or any(character.isspace() for character in secret):
            raise CloudboxError(
                "invalid_secret", "Supply a non-empty key without spaces."
            )
        session.client("secretsmanager", config=SDK_CONFIG).put_secret_value(
            SecretId=deployment["openrouter_secret_arn"],
            SecretString=secret,
        )
        emit({"ok": True, "secret_arn": deployment["openrouter_secret_arn"]})
        return 0
    except (
        CloudboxError,
        BotoCoreError,
        ClientError,
        OSError,
        EOFError,
        ValueError,
        getpass.GetPassWarning,
    ) as error:
        emit(error_record(error))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
