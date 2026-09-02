"""Report Cloudbox resources without changing AWS."""

import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import BotoCoreError, ClientError
from cloudbox.common import CloudboxError, emit, error_record
from cloudbox.environments import add_environment_argument, get_environment
from cloudbox.resources import check_resources


def main(argv=None):
    try:
        parser = argparse.ArgumentParser(description="Check Terraform targets and project resource residue.")
        add_environment_argument(parser)
        parser.add_argument("--require-clean", action="store_true", help="Fail if cloud resources or state remain.")
        arguments = parser.parse_args(argv)
        emit(check_resources(get_environment(arguments.env), require_clean=arguments.require_clean))
        return 0
    except (CloudboxError, BotoCoreError, ClientError, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError) as error:
        emit(error_record(error))
        return 1
    except KeyboardInterrupt:
        emit({"ok": False, "error": {"code": "interrupted"}})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
