"""Check one cloud job, its saved result, logs, and stopped VM."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudbox.common import RUN_SCHEMA_VERSION, CloudboxError, emit, error_record
from cloudbox.environments import add_environment_argument, get_environment

FACTOR_LEFT = 12345
FACTOR_RIGHT = 6789
OFFSET = 98765
EXPECTED_ANSWER = FACTOR_LEFT * FACTOR_RIGHT + OFFSET
RUN_TIMEOUT_SECONDS = 600
OBSERVATION_GRACE_SECONDS = 120
LOG_WAIT_SECONDS = 60
POLL_SECONDS = 5
COMMAND_TIMEOUT_SECONDS = 60
SUCCESS_STATUS = "succeeded"
COMPLETED_STATUS = "completed"
TERMINATED_STATE = "TERMINATED"
INTERRUPTED_EXIT_CODE = 130
REPO_ROOT = Path(__file__).resolve().parents[1]
RESULT_PATH = Path("result.json")
TEST_NAME = "cloud_math"


def command_records(environment, *arguments):
    """Use the public CLI; never include raw stderr in saved errors."""
    response = subprocess.run(
        [sys.executable, "-m", "cloudbox", "--env", environment.name, *arguments],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    try:
        records = [
            json.loads(line) for line in response.stdout.splitlines() if line.strip()
        ]
        if any(not isinstance(record, dict) for record in records):
            raise ValueError
    except ValueError as error:
        raise CloudboxError(
            "invalid_cli_output", "The CLI did not return JSON objects."
        ) from error
    if response.returncode or any(record.get("ok") is False for record in records):
        last = records[-1] if records else {}
        raise CloudboxError(
            "cli_failed",
            f"Cloudbox {arguments[0]} failed.",
            exit_code=response.returncode,
            cli_error=last.get("error", {}).get("code"),
            run_id=last.get("run_id"),
        )
    return records


def command(environment, *arguments):
    records = command_records(environment, *arguments)
    if len(records) != 1:
        raise CloudboxError("invalid_cli_output", "The CLI did not return one result.")
    return records[0]


def listed_run(environment, identity):
    cursor = None
    while True:
        arguments = ["list"]
        if cursor:
            arguments.extend(("--cursor", cursor))
        page = command(environment, *arguments)
        for run in page["runs"]:
            if run.get("run_id") == identity:
                return run
        cursor = page.get("next_cursor")
        if not cursor:
            raise CloudboxError(
                "run_not_listed", "The completed run is missing from the run list."
            )


def log_count(environment, identity):
    # Save counts only; CloudWatch messages need not enter the test report.
    records = command_records(environment, "logs", identity)
    if not records or records[-1].get("end_of_stream") is not True:
        raise CloudboxError(
            "invalid_log_output", "The CLI did not finish the log read."
        )
    return sum("event" in record for record in records)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run and validate one cloud math job. AWS and OpenRouter charges apply."
    )
    add_environment_argument(parser)
    parser.add_argument(
        "--output-directory",
        type=Path,
        help="Parent directory for this run's downloaded files.",
    )
    arguments = parser.parse_args(argv)
    environment = get_environment(arguments.env)
    prompt = (
        f"Calculate ({FACTOR_LEFT} * {FACTOR_RIGHT}) + {OFFSET}. "
        "Check with a tool. Set finish.result to a JSON object with an integer answer field."
    )
    identity, terminated, status, destination = None, False, None, None
    stage = "submit"
    try:
        submitted = command(
            environment, "submit", prompt, "--timeout", str(RUN_TIMEOUT_SECONDS)
        )
        identity = submitted["run_id"]
        parent = (
            arguments.output_directory
            or REPO_ROOT / ".cloudbox" / "smoke" / environment.name
        )
        destination = parent.expanduser().resolve() / identity
        emit(
            {
                "test": TEST_NAME,
                "environment": environment.name,
                "run_id": identity,
                "status": "waiting",
            }
        )
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS + OBSERVATION_GRACE_SECONDS

        # Compute must stop before download; the saved result must outlive the VM.
        stage = "status"
        while time.monotonic() < deadline:
            status = command(environment, "status", identity)
            if status["compute_state"] == TERMINATED_STATE:
                terminated = True
                break
            time.sleep(POLL_SECONDS)
        if not terminated:
            raise CloudboxError(
                "vm_not_stopped", "The VM did not stop before the test deadline."
            )
        if status["task_status"] != SUCCESS_STATUS:
            raise CloudboxError(
                "job_failed",
                "The math job did not succeed.",
                task_status=status["task_status"],
            )

        stage = "download"
        command(environment, "download", identity, "--output", str(destination))
        result_path = destination / RESULT_PATH
        saved = json.loads(result_path.read_text(encoding="utf-8"))
        report = saved.get("report") if isinstance(saved, dict) else None
        if (
            not isinstance(saved, dict)
            or saved.get("schema_version") != RUN_SCHEMA_VERSION
            or saved.get("status") != SUCCESS_STATUS
            or not isinstance(report, dict)
            or report.get("status") != COMPLETED_STATUS
            or not isinstance(report.get("summary"), str)
            or not report["summary"].strip()
        ):
            raise CloudboxError(
                "invalid_report",
                "The saved run and finish report must both show success.",
            )
        result = report.get("result")
        if not isinstance(result, dict) or "answer" not in result:
            raise CloudboxError(
                "invalid_answer",
                "The finish report must contain result.answer.",
            )
        if type(result["answer"]) is not int or result["answer"] != EXPECTED_ANSWER:
            raise CloudboxError(
                "wrong_answer", f"The answer does not equal {EXPECTED_ANSWER}."
            )

        stage = "list"
        listed = listed_run(environment, identity)
        if (
            listed["task_status"] != SUCCESS_STATUS
            or listed["compute_state"] != TERMINATED_STATE
        ):
            raise CloudboxError(
                "invalid_run_list",
                "The run list does not show success and a stopped VM.",
            )
        stage = "logs"
        log_deadline = time.monotonic() + LOG_WAIT_SECONDS
        events = log_count(environment, identity)
        while not events and time.monotonic() < log_deadline:
            time.sleep(POLL_SECONDS)
            events = log_count(environment, identity)
        if not events:
            raise CloudboxError(
                "logs_missing", "CloudWatch returned no events for this run."
            )
        emit(
            {
                "ok": True,
                "test": TEST_NAME,
                "environment": environment.name,
                "status": "passed",
                "run_id": identity,
                "answer": result["answer"],
                "result_path": str(result_path),
                "compute_state": status["compute_state"],
                "listed": True,
                "log_event_count": events,
            }
        )
        return 0
    except (Exception, KeyboardInterrupt) as error:
        # A lost launch reply can still include the run ID; never submit a second job.
        if not identity and isinstance(error, CloudboxError):
            identity = error.details.get("run_id")
        cancelled, cancellation_error = None, None
        if identity and not terminated:
            try:
                cancelled = command(environment, "cancel", identity).get(
                    "cancel_requested"
                )
            except Exception as cancel_error:
                cancellation_error = error_record(cancel_error)["error"]
        diagnostic_errors, events = [], None
        if identity:
            # Keep saved records before lifecycle teardown removes cloud data.
            if destination is None:
                parent = (
                    arguments.output_directory
                    or REPO_ROOT / ".cloudbox" / "smoke" / environment.name
                )
                destination = parent.expanduser().resolve() / identity
            try:
                if not destination.exists():
                    command(
                        environment, "download", identity, "--output", str(destination)
                    )
            except Exception as diagnostic_error:
                diagnostic_errors.append(error_record(diagnostic_error)["error"])
            try:
                events = log_count(environment, identity)
            except Exception as diagnostic_error:
                diagnostic_errors.append(error_record(diagnostic_error)["error"])
        emit(
            {
                **error_record(error),
                "test": TEST_NAME,
                "environment": environment.name,
                "status": "failed",
                "stage": stage,
                "run_id": identity,
                "compute_state": status.get("compute_state") if status else None,
                "cancel_requested": cancelled,
                "cancellation_error": cancellation_error,
                "download_directory": str(destination) if destination else None,
                "log_event_count": events,
                "diagnostic_errors": diagnostic_errors,
            }
        )
        return INTERRUPTED_EXIT_CODE if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
