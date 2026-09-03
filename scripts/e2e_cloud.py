"""Rebuild, test, and remove the disposable test deployment."""

import argparse
import io
import json
import sys
import uuid
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudbox.common import (
    ROOT,
    CloudboxError,
    emit,
    error_record,
    operator_session,
    timestamp,
)
from cloudbox.environments import get_environment
from cloudbox.resources import check_resources, state_remains
from scripts import setup, smoke_cloud, teardown

TEST_ENVIRONMENT = "test"
PROD_ENVIRONMENT = "prod"
IDENTITY_FIELDS = ("aws_account_id", "aws_region", "aws_profile", "project_name")
REPORT_VERSION = 1
INTERRUPTED_EXIT_CODE = 130
TEST_NAME = "cloud_lifecycle"


def test_configuration(environment, expected=None):
    # Check account separation and unchanged targets before cloud changes.
    if environment.name != TEST_ENVIRONMENT:
        raise CloudboxError("test_only", "The lifecycle test accepts only --env test.")
    config = setup.read_config(environment)[0]["deployment"]
    if expected and any(config[field] != expected[field] for field in IDENTITY_FIELDS):
        raise CloudboxError(
            "test_target_changed",
            "The test account or deployment settings changed. Stop and inspect them.",
        )
    prod = get_environment(PROD_ENVIRONMENT)
    if not prod.input_path.exists():
        # Missing inputs are safe only when no saved prod deployment remains.
        try:
            if any(state_remains(prod, root) for root in prod.roots):
                raise CloudboxError(
                    "prod_config_missing",
                    "Restore the prod input file before the lifecycle test.",
                )
        except (OSError, ValueError, AttributeError) as error:
            raise CloudboxError(
                "invalid_state", "Check the saved prod state before the lifecycle test."
            ) from error
        return config
    other = setup.read_config(prod)[0]["deployment"]
    if config["aws_account_id"] == other["aws_account_id"]:
        raise CloudboxError(
            "test_account_shared", "Test must use a different account from prod."
        )
    return config


class Report:
    def __init__(self, environment):
        identity = str(uuid.uuid4())
        self.directory = ROOT / ".cloudbox" / "e2e" / environment.name / identity
        self.directory.mkdir(parents=True, exist_ok=False)
        self.path = self.directory / "report.json"
        self.data = {
            "schema_version": REPORT_VERSION,
            "test": TEST_NAME,
            "environment": environment.name,
            "execution_id": identity,
            "started_at": timestamp(),
            "status": "running",
            "stages": [],
            "primary_failure": None,
            "cleanup_failure": None,
            "scope": "Cloudbox resources, not unrelated resources or AWS service history.",
        }
        self.output = sys.stdout
        self.storage_error = None
        self.save()
        if self.storage_error:
            raise CloudboxError(
                "report_unavailable", "The local test report could not be saved."
            )

    def save(self):
        # A report write failure must not prevent cloud cleanup.
        try:
            self.path.write_text(
                json.dumps(self.data, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as error:
            self.storage_error = error_record(error)["error"]

    def event(self, stage, record):
        value = {"timestamp": timestamp(), "stage": stage, **record}
        self.data["stages"].append(value)
        self.save()
        try:
            self.output.write(
                json.dumps(
                    {
                        "test": TEST_NAME,
                        "environment": self.data["environment"],
                        **value,
                    }
                )
                + "\n"
            )
            self.output.flush()
        except OSError:
            # A closed output pipe must not leave test resources running.
            pass


class StageOutput(io.TextIOBase):
    def __init__(self, report, stage):
        self.report, self.stage = report, stage
        self.pending = ""
        self.records = []

    def write(self, text):
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise CloudboxError(
                    "invalid_stage_output",
                    "A lifecycle stage did not return a JSON object.",
                )
            self.records.append(record)
            self.report.event(self.stage, {"status": "output", "result": record})
        return len(text)


def run_stage(report, name, entrypoint, arguments):
    report.event(name, {"status": "started"})
    output = StageOutput(report, name)
    with redirect_stdout(output):
        exit_code = entrypoint(arguments)
    result = output.records[-1] if output.records else None
    report.event(
        name,
        {"status": "passed" if exit_code == 0 else "failed", "exit_code": exit_code},
    )
    if exit_code != 0 or not result or result.get("ok") is not True:
        raise CloudboxError(
            "stage_failed",
            f"The {name} stage failed.",
            stage=name,
            exit_code=exit_code,
            result=result,
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rebuild, test, and remove Cloudbox test resources without approval."
    )
    parser.add_argument(
        "--env",
        choices=(TEST_ENVIRONMENT,),
        default=TEST_ENVIRONMENT,
        help="Defaults to test; other environments are rejected.",
    )
    parser.add_argument(
        "--env-file", type=Path, help="OpenRouter key file; defaults to .env.test."
    )
    # Accept old commands; test resources no longer need interactive approval.
    parser.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    report, reset_started, setup_started = None, False, False
    environment = get_environment(arguments.env)
    teardown_arguments = ["--env", environment.name, "--yes", "--force-delete-secret"]
    stage = "preflight"
    try:
        config = test_configuration(environment)
        report = Report(environment)
        report.data.update(
            {
                "account_id": config["aws_account_id"],
                "region": config["aws_region"],
                "project": config["project_name"],
            }
        )
        operator_session(config, provisioner=False)
        initial = check_resources(environment)
        report.data["before"] = initial
        report.save()
        print(
            f"Test account: {config['aws_account_id']} ({config['aws_region']})\n"
            "Remove existing Cloudbox test resources; rebuild; run one math job; remove test resources and the secret.\n"
            "AWS and OpenRouter charges apply. Do not use this test account during the test.",
            file=sys.stderr,
        )
        test_configuration(environment, config)
        if not initial["clean"] or not initial["local_state_empty"]:
            # Reset tracked test resources; teardown retains state and ownership guards.
            stage, reset_started = "reset", True
            run_stage(report, stage, teardown.main, teardown_arguments)
        stage = "verify_clean"
        test_configuration(environment, config)
        check_resources(environment, require_clean=True)
        report.event(stage, {"status": "passed"})
        setup_arguments = ["--env", environment.name, "--yes"]
        if arguments.env_file:
            setup_arguments.extend(("--env-file", str(arguments.env_file)))
        stage, setup_started = "setup", True
        run_stage(report, stage, setup.main, setup_arguments)
        stage = "math"
        test_configuration(environment, config)
        report.data["math"] = run_stage(
            report,
            stage,
            smoke_cloud.main,
            [
                "--env",
                environment.name,
                "--output-directory",
                str(report.directory / "run"),
            ],
        )
    except (Exception, KeyboardInterrupt) as error:
        failure = {"stage": stage, **error_record(error)}
        if report is None:
            emit(failure)
            return INTERRUPTED_EXIT_CODE if isinstance(error, KeyboardInterrupt) else 1
        report.data["primary_failure"] = failure
        report.event(stage, {"status": "failed", "failure": failure})
    finally:
        if report is not None and setup_started:
            # Run cleanup after setup errors and Ctrl-C; retain both failure records.
            try:
                test_configuration(environment, config)
                run_stage(report, "teardown", teardown.main, teardown_arguments)
            except (Exception, KeyboardInterrupt) as error:
                report.data["cleanup_failure"] = error_record(error)
                report.event(
                    "teardown", {"status": "failed", "failure": error_record(error)}
                )
        if report is not None and (reset_started or setup_started):
            # Inspect a failed reset without repeating its destructive operation.
            try:
                test_configuration(environment, config)
                final = check_resources(environment)
                report.data["after"] = final
                report.event(
                    "verify_empty",
                    {
                        "status": "passed" if final["clean"] else "failed",
                        "clean": final["clean"],
                    },
                )
                if not final["clean"]:
                    report.data["cleanup_failure"] = report.data["cleanup_failure"] or {
                        "error": {
                            "code": "resources_remain",
                            "message": "Test resources or state remain.",
                        }
                    }
            except (Exception, KeyboardInterrupt) as error:
                report.data["cleanup_failure"] = report.data[
                    "cleanup_failure"
                ] or error_record(error)
                report.event(
                    "verify_empty", {"status": "failed", "failure": error_record(error)}
                )
    success = (
        setup_started
        and not report.data["primary_failure"]
        and not report.data["cleanup_failure"]
        and not report.storage_error
    )
    report.data.update(
        {
            "status": "passed" if success else "failed",
            "finished_at": timestamp(),
            "report_error": report.storage_error,
        }
    )
    report.save()
    success = success and not report.storage_error
    emit(
        {
            "ok": success,
            "test": TEST_NAME,
            "environment": environment.name,
            "status": "passed" if success else "failed",
            "report_path": str(report.path),
            "clean": report.data.get("after", {}).get("clean"),
            "primary_failure": report.data["primary_failure"],
            "cleanup_failure": report.data["cleanup_failure"],
        }
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
