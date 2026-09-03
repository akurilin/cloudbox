"""Check one job on an existing deployment; keep shared infrastructure."""

import argparse
import csv
import hashlib
import io
import json
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http import HTTPStatus
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from cloudbox.common import (
    CloudboxError,
    emit,
    error_record,
    load_deployment,
    parse_timeout,
)
from cloudbox.environments import add_environment_argument, get_environment

RUN_TIMEOUT_SECONDS = 1200
OBSERVATION_GRACE_SECONDS = 120
STOP_POLL_SECONDS = 2
COMMAND_TIMEOUT_SECONDS = 60
DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_FILE_BYTES = 4 * 1024 * 1024
FIRST_NUMBER = 1
LAST_NUMBER = 10
EXPECTED_ANSWER = sum(
    number * number for number in range(FIRST_NUMBER, LAST_NUMBER + 1)
)
IMAGE_SIZE = (32, 32)
IMAGE_COLOR = (0, 0, 255, 255)
CSV_NAME = "squares.csv"
IMAGE_NAME = "blue.png"
ZIP_NAME = "bundle.zip"
EXPECTED_NAMES = {CSV_NAME, IMAGE_NAME, ZIP_NAME}
SUCCESS_STATUS = "succeeded"
COMPLETED_STATUS = "completed"
TERMINATED_STATE = "TERMINATED"
INTERRUPTED_EXIT_CODE = 130
RUN_ID_PATTERN = re.compile(r"^Run ID: ([0-9a-f-]{36})$")
URL_PATTERN = re.compile(r"https://[^\s<>\[\]()\"'`]+")
URL_SENTENCE_PUNCTUATION = ".,;:!?"
DEBUG_PREFIXES = {"agent": "[agent]", "supervisor": "[supervisor]"}
REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_NAME = "cloud_files"


def command(environment, *arguments):
    response = subprocess.run(
        [sys.executable, "-m", "cloudbox", "--env", environment.name, *arguments],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    try:
        record = json.loads(response.stdout)
        if not isinstance(record, dict):
            raise ValueError
    except ValueError as error:
        raise CloudboxError(
            "invalid_cli_output", "The CLI did not return one JSON object."
        ) from error
    if response.returncode or record.get("ok") is False:
        raise CloudboxError(
            "cli_failed",
            f"Cloudbox {arguments[0]} failed.",
            cli_error=record.get("error", {}).get("code"),
        )
    return record


def execute(
    environment, prompt, directory, observed, *, timeout_seconds=RUN_TIMEOUT_SECONDS
):
    # Read both pipes while exec runs, so buffered logs cannot pass the live check.
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cloudbox",
            "--env",
            environment.name,
            "exec",
            prompt,
            "--timeout",
            str(timeout_seconds),
            "--debug-agent",
            "--debug-supervisor",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    events = queue.Queue()

    def read_stream(name, stream):
        with stream:
            for line in stream:
                events.put((name, line, time.monotonic(), process.poll() is None))
        events.put((name, None, time.monotonic(), False))

    readers = [
        threading.Thread(target=read_stream, args=(name, stream), daemon=True)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout_seconds + OBSERVATION_GRACE_SECONDS
    complete = set()
    stdout = []
    try:
        with (
            (directory / "response.txt").open("w", encoding="utf-8") as output,
            (directory / "debug.log").open("w", encoding="utf-8") as debug,
        ):
            while len(complete) < len(readers):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CloudboxError(
                        "exec_timeout",
                        "The CLI did not finish before the test deadline.",
                    )
                try:
                    name, line, received, running = events.get(
                        timeout=min(remaining, 1)
                    )
                except queue.Empty:
                    continue
                if line is None:
                    complete.add(name)
                    continue
                target = output if name == "stdout" else debug
                target.write(line)
                target.flush()
                if name == "stdout":
                    stdout.append(line)
                    observed.setdefault("response_at", received)
                else:
                    match = RUN_ID_PATTERN.fullmatch(line.strip())
                    if match:
                        observed["run_id"] = str(uuid.UUID(match.group(1)))
                    for source, prefix in DEBUG_PREFIXES.items():
                        if line.startswith(prefix) and running:
                            observed.setdefault(f"{source}_at", received)
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if code:
            raise CloudboxError(
                "exec_failed", "The cloud job command failed.", exit_code=code
            )
        return "".join(stdout)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for reader in readers:
            reader.join(timeout=1)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise CloudboxError(
            "download_redirect", "A file URL redirected outside its stored location."
        )


def download_urls(response, artifacts, deployment, identity, directory):
    # Fetch the exact printed URLs without AWS credentials, after the VM has stopped.
    if not isinstance(artifacts, list) or len(artifacts) != len(EXPECTED_NAMES):
        raise CloudboxError("missing_files", "The job must publish three files.")
    if {item.get("name") for item in artifacts} != EXPECTED_NAMES:
        raise CloudboxError(
            "wrong_files", "The published filenames do not match the requested files."
        )
    links = {
        link.rstrip(URL_SENTENCE_PUNCTUATION) for link in URL_PATTERN.findall(response)
    }
    if len(links) != len(EXPECTED_NAMES):
        raise CloudboxError(
            "missing_links", "The response must contain three download links."
        )
    opener = build_opener(NoRedirect())
    files = {}
    host = f"{deployment['bucket_name']}.s3.{deployment['aws_region']}.amazonaws.com"
    for artifact in artifacts:
        url = artifact.get("url")
        if url not in links:
            raise CloudboxError(
                "changed_link", "The response does not contain a published file URL."
            )
        parsed = urlsplit(url)
        key = artifact.get("key", "")
        if (
            parsed.scheme != "https"
            or parsed.netloc != host
            or unquote(parsed.path) != "/" + key
            or not key.startswith(f"runs/{identity}/artifacts/")
        ):
            raise CloudboxError(
                "invalid_link", "A download link does not belong to this run."
            )
        with opener.open(Request(url), timeout=DOWNLOAD_TIMEOUT_SECONDS) as remote:
            if remote.status != HTTPStatus.OK:
                raise CloudboxError(
                    "download_failed", "A published file could not be downloaded."
                )
            contents = remote.read(MAX_FILE_BYTES + 1)
        if len(contents) > MAX_FILE_BYTES:
            raise CloudboxError(
                "file_too_large", "A test file exceeds the download limit."
            )
        if len(contents) != artifact.get("bytes") or hashlib.sha256(
            contents
        ).hexdigest() != artifact.get("sha256"):
            raise CloudboxError(
                "file_changed", "Downloaded bytes differ from the published file."
            )
        files[artifact["name"]] = contents
        (directory / artifact["name"]).write_bytes(contents)
    return files


def check_files(files):
    # Check task content independently of the agent's report and upload metadata.
    rows = list(csv.reader(io.StringIO(files[CSV_NAME].decode("utf-8-sig"))))
    expected = [
        [str(number), str(number * number)]
        for number in range(FIRST_NUMBER, LAST_NUMBER + 1)
    ]
    if not rows or rows[0] != ["n", "square"] or rows[1:] != expected:
        raise CloudboxError("wrong_csv", "The CSV numbers or squares are incorrect.")
    with Image.open(io.BytesIO(files[IMAGE_NAME])) as picture:
        if picture.format != "PNG" or picture.size != IMAGE_SIZE:
            raise CloudboxError("wrong_image", "The image must be a 32 by 32 PNG.")
        pixels = picture.convert("RGBA")
        if pixels.getextrema() != tuple((value, value) for value in IMAGE_COLOR):
            raise CloudboxError("wrong_image", "The image pixels must be solid blue.")
    with zipfile.ZipFile(io.BytesIO(files[ZIP_NAME])) as archive:
        if (
            set(archive.namelist()) != {CSV_NAME, IMAGE_NAME}
            or len(archive.infolist()) != 2
        ):
            raise CloudboxError(
                "wrong_zip", "The ZIP must contain only the CSV and PNG."
            )
        for entry in archive.infolist():
            if (
                entry.file_size > MAX_FILE_BYTES
                or archive.read(entry) != files[entry.filename]
            ):
                raise CloudboxError(
                    "wrong_zip", "An archived file differs from its separate download."
                )


def stop_job(environment, identity):
    # A cancellation request is not proof that the job VM has stopped.
    cancelled = command(environment, "cancel", identity)
    deadline = time.monotonic() + OBSERVATION_GRACE_SECONDS
    current = cancelled
    while current.get("compute_state") != TERMINATED_STATE:
        if time.monotonic() >= deadline:
            raise CloudboxError(
                "vm_not_stopped",
                "The test job VM did not stop before the cleanup deadline.",
            )
        time.sleep(STOP_POLL_SECONDS)
        current = command(environment, "status", identity)
    return cancelled.get("cancel_requested"), current


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check response, logs, and files on an existing deployment."
    )
    add_environment_argument(parser)
    parser.add_argument(
        "--output-directory", type=Path, help="Parent directory for test evidence."
    )
    parser.add_argument(
        "--timeout",
        type=parse_timeout,
        default=RUN_TIMEOUT_SECONDS,
        help="Job deadline; default 20m. Use seconds, m, or h.",
    )
    arguments = parser.parse_args(argv)
    environment = get_environment(arguments.env)
    test_id = str(uuid.uuid4())
    parent = (
        arguments.output_directory
        or REPO_ROOT / ".cloudbox" / "smoke" / environment.name
    )
    directory = parent.expanduser().resolve() / test_id
    directory.mkdir(parents=True, exist_ok=False)
    observed = {}
    status = None
    stage = "deployment"
    result = {
        "test": TEST_NAME,
        "environment": environment.name,
        "test_id": test_id,
        "directory": str(directory),
        "timeout_seconds": arguments.timeout,
    }
    prompt = (
        f"Create {CSV_NAME} with columns n,square for integers {FIRST_NUMBER}-{LAST_NUMBER}. "
        f"Create a {IMAGE_SIZE[0]}x{IMAGE_SIZE[1]} solid-blue PNG named {IMAGE_NAME}. "
        f"Create {ZIP_NAME} containing those two files at its root. Publish all three. "
        f"In your final response, include the sum of the squares, test ID {test_id}, "
        "and a download link for each file."
    )
    try:
        deployment = load_deployment(environment)
        stage = "exec"
        response = execute(
            environment, prompt, directory, observed, timeout_seconds=arguments.timeout
        )
        identity = observed.get("run_id")
        result["run_id"] = identity
        if not identity:
            raise CloudboxError("missing_run_id", "The CLI did not report a run ID.")
        if test_id not in response or not re.search(
            rf"\b{EXPECTED_ANSWER}\b", response
        ):
            raise CloudboxError(
                "wrong_response",
                "The final response must contain the test ID and correct sum.",
            )
        if any(
            line.startswith(tuple(DEBUG_PREFIXES.values()))
            for line in response.splitlines()
        ):
            raise CloudboxError(
                "mixed_output", "Debug events entered the final response."
            )
        for source in DEBUG_PREFIXES:
            if observed.get(f"{source}_at", float("inf")) >= observed.get(
                "response_at", 0
            ):
                raise CloudboxError(
                    "logs_not_live",
                    f"No live {source} event preceded the final response.",
                )
        stage = "status"
        status = command(environment, "status", identity)
        if (
            status.get("task_status") != SUCCESS_STATUS
            or status.get("compute_state") != TERMINATED_STATE
        ):
            raise CloudboxError(
                "job_incomplete", "The CLI returned before success and VM termination."
            )
        saved = status.get("result") or {}
        report = saved.get("report") or {}
        if (
            report.get("status") != COMPLETED_STATUS
            or response != report.get("response", "") + "\n"
        ):
            raise CloudboxError(
                "response_changed",
                "The CLI response differs from the saved agent response.",
            )
        (directory / "result.json").write_text(
            json.dumps(saved, indent=2) + "\n", encoding="utf-8"
        )
        stage = "files"
        files = download_urls(
            response, saved.get("artifacts"), deployment, identity, directory
        )
        check_files(files)
        result.update(
            ok=True,
            status="passed",
            answer=EXPECTED_ANSWER,
            compute_state=status["compute_state"],
            files=sorted(files),
            live_logs=True,
        )
        return_code = 0
    except (Exception, KeyboardInterrupt) as error:
        identity = observed.get("run_id")
        result.update(
            error_record(error), status="failed", stage=stage, run_id=identity
        )
        if identity:
            # Stop only this job on failure; retain shared resources and saved evidence.
            try:
                if not status or status.get("compute_state") != TERMINATED_STATE:
                    result["cancel_requested"], status = stop_job(environment, identity)
            except Exception as cleanup_error:
                result["cleanup_error"] = error_record(cleanup_error)["error"]
            try:
                snapshot = command(environment, "status", identity)
                (directory / "status.json").write_text(
                    json.dumps(snapshot, indent=2) + "\n", encoding="utf-8"
                )
            except Exception as diagnostic_error:
                result["diagnostic_error"] = error_record(diagnostic_error)["error"]
        return_code = (
            INTERRUPTED_EXIT_CODE if isinstance(error, KeyboardInterrupt) else 1
        )
    (directory / "verification.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    emit(result)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
