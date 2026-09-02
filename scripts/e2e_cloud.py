"""Build, test, and remove an initially empty test deployment."""

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudbox.common import ROOT, CloudboxError, emit, error_record, operator_session, timestamp
from cloudbox.environments import add_environment_argument, get_environment
from cloudbox.resources import check_resources
from scripts import setup, smoke_cloud, teardown

TEST_ENVIRONMENT = "test"
PROTECTED_ENVIRONMENTS = ("prod", "legacy")
IDENTITY_FIELDS = ("aws_account_id", "aws_region", "aws_profile", "project_name")
REPORT_VERSION = 1
INTERRUPTED_EXIT_CODE = 130
TEST_NAME = "cloud_lifecycle"


def test_configuration(environment, expected=None):
    # Refuse prod, reused account IDs, or changed targets before cloud changes.
    if environment.name != TEST_ENVIRONMENT:
        raise CloudboxError("test_only", "The lifecycle test accepts only --env test.")
    config = setup.read_config(environment)[0]["deployment"]
    if expected and any(config[field] != expected[field] for field in IDENTITY_FIELDS):
        raise CloudboxError("test_target_changed", "The test account or deployment settings changed. Stop and inspect them.")
    for name in PROTECTED_ENVIRONMENTS:
        protected = get_environment(name)
        if name == "legacy" and not protected.input_path.exists():
            for root in protected.roots:
                path = protected.state_path(root)
                if path.exists():
                    state = json.loads(path.read_bytes())
                    if state.get("resources") or state.get("outputs"):
                        raise CloudboxError("legacy_config_missing", "Restore the legacy input file before the lifecycle test.")
            continue
        other = setup.read_config(protected)[0]["deployment"]
        if config["aws_account_id"] == other["aws_account_id"]:
            raise CloudboxError("test_account_shared", f"Test must use a different account from {name}.")
    return config


class Report:
    def __init__(self, environment):
        identity = str(uuid.uuid4())
        self.directory = ROOT / ".cloudbox" / "e2e" / environment.name / identity
        self.directory.mkdir(parents=True, exist_ok=False)
        self.path = self.directory / "report.json"
        self.data = {"schema_version": REPORT_VERSION, "test": TEST_NAME,
                     "environment": environment.name, "execution_id": identity,
                     "started_at": timestamp(), "status": "running", "stages": [],
                     "primary_failure": None, "cleanup_failure": None,
                     "scope": "Cloudbox resources, not unrelated resources or AWS service history."}
        self.output = sys.stdout
        self.storage_error = None
        self.save()
        if self.storage_error:
            raise CloudboxError("report_unavailable", "The local test report could not be saved.")

    def save(self):
        # A report write failure must not prevent cloud cleanup.
        try:
            self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            self.storage_error = error_record(error)["error"]

    def event(self, stage, record):
        value = {"timestamp": timestamp(), "stage": stage, **record}
        self.data["stages"].append(value)
        self.save()
        try:
            self.output.write(json.dumps({"test": TEST_NAME, "environment": self.data["environment"], **value}) + "\n")
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
                raise CloudboxError("invalid_stage_output", "A lifecycle stage did not return a JSON object.")
            self.records.append(record)
            self.report.event(self.stage, {"status": "output", "result": record})
        return len(text)


def run_stage(report, name, entrypoint, arguments):
    report.event(name, {"status": "started"})
    output = StageOutput(report, name)
    with redirect_stdout(output):
        exit_code = entrypoint(arguments)
    result = output.records[-1] if output.records else None
    report.event(name, {"status": "passed" if exit_code == 0 else "failed", "exit_code": exit_code})
    if exit_code != 0 or not result or result.get("ok") is not True:
        raise CloudboxError("stage_failed", f"The {name} stage failed.",
                            stage=name, exit_code=exit_code, result=result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create, test, and remove Cloudbox in an empty test account.")
    add_environment_argument(parser)
    parser.add_argument("--env-file", type=Path, help="OpenRouter key file; defaults to .env.test.")
    parser.add_argument("--yes", action="store_true", help="Approve cloud usage and permanent deletion of test resources.")
    arguments = parser.parse_args(argv)
    report, setup_started = None, False
    environment = get_environment(arguments.env)
    stage = "preflight"
    try:
        config = test_configuration(environment)
        report = Report(environment)
        report.data.update({"account_id": config["aws_account_id"], "region": config["aws_region"],
                            "project": config["project_name"]})
        operator_session(config, provisioner=False)
        initial = check_resources(environment)
        report.data["before"] = initial
        report.save()
        if not initial["clean"] or not initial["local_state_empty"]:
            raise CloudboxError("test_not_empty", "Test resources or state already exist. This test will not delete them.")
        print(f"Test account: {config['aws_account_id']} ({config['aws_region']})\n"
              "Create infrastructure; run one math job; permanently delete test resources and the secret.\n"
              "AWS and OpenRouter charges apply. Do not use this test account during the test.", file=sys.stderr)
        if not arguments.yes:
            # Keep stdout as JSON even when approval is declined.
            print("Approve the full lifecycle test? [y/N] ", end="", file=sys.stderr, flush=True)
            if input().strip().lower() not in {"y", "yes"}:
                raise CloudboxError("not_approved", "The lifecycle test was not approved.")
        test_configuration(environment, config)
        # Recheck after approval; never remove a pre-existing test deployment.
        check_resources(environment, require_clean=True)
        setup_arguments = ["--env", environment.name, "--yes"]
        if arguments.env_file:
            setup_arguments.extend(("--env-file", str(arguments.env_file)))
        stage, setup_started = "setup", True
        run_stage(report, stage, setup.main, setup_arguments)
        stage = "math"
        test_configuration(environment, config)
        report.data["math"] = run_stage(report, stage, smoke_cloud.main,
                                        ["--env", environment.name, "--output-directory", str(report.directory / "run")])
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
                run_stage(report, "teardown", teardown.main,
                          ["--env", environment.name, "--yes", "--force-delete-secret"])
            except (Exception, KeyboardInterrupt) as error:
                report.data["cleanup_failure"] = error_record(error)
                report.event("teardown", {"status": "failed", "failure": error_record(error)})
            try:
                test_configuration(environment, config)
                final = check_resources(environment)
                report.data["after"] = final
                report.event("verify_empty", {"status": "passed" if final["clean"] else "failed", "clean": final["clean"]})
                if not final["clean"]:
                    report.data["cleanup_failure"] = report.data["cleanup_failure"] or {
                        "error": {"code": "resources_remain", "message": "Test resources or state remain."}}
            except (Exception, KeyboardInterrupt) as error:
                report.data["cleanup_failure"] = report.data["cleanup_failure"] or error_record(error)
                report.event("verify_empty", {"status": "failed", "failure": error_record(error)})
    success = setup_started and not report.data["primary_failure"] and not report.data["cleanup_failure"] and not report.storage_error
    report.data.update({"status": "passed" if success else "failed", "finished_at": timestamp(),
                        "report_error": report.storage_error})
    report.save()
    success = success and not report.storage_error
    emit({"ok": success, "test": TEST_NAME, "environment": environment.name,
          "status": "passed" if success else "failed", "report_path": str(report.path),
          "clean": report.data.get("after", {}).get("clean"),
          "primary_failure": report.data["primary_failure"], "cleanup_failure": report.data["cleanup_failure"]})
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
