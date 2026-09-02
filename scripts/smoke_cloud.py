"""Run the one spike test against the deployed AWS worker."""

import json
from pathlib import Path
import subprocess
import sys
import time


FACTOR_LEFT = 12345
FACTOR_RIGHT = 6789
OFFSET = 98765
EXPECTED_ANSWER = FACTOR_LEFT * FACTOR_RIGHT + OFFSET
RUN_TIMEOUT_SECONDS = 600
OBSERVATION_GRACE_SECONDS = 120
POLL_SECONDS = 5
COMMAND_TIMEOUT_SECONDS = 60
SUCCESS_STATUS = "succeeded"
TERMINATED_STATE = "TERMINATED"
REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = Path("output/result.json")


def command(*arguments: str) -> dict:
    """Exercise the public CLI, including its JSON response contract."""
    response = subprocess.run(
        [sys.executable, "-m", "cloudbox", *arguments],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if response.returncode:
        raise RuntimeError(response.stderr.strip() or response.stdout.strip())
    return json.loads(response.stdout)


def main() -> int:
    prompt = (
        f"Calculate ({FACTOR_LEFT} * {FACTOR_RIGHT}) + {OFFSET}. "
        "Use a tool to check your calculation. Write the integer answer to "
        'output/result.json as a JSON object with exactly one key, "answer". '
        "Do not put Markdown in the file."
    )
    run_id = None
    terminated = False
    try:
        submitted = command("submit", prompt, "--timeout", str(RUN_TIMEOUT_SECONDS))
        run_id = submitted["run_id"]
        print(json.dumps({"test": "cloud_math", "run_id": run_id, "status": "waiting"}), flush=True)
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS + OBSERVATION_GRACE_SECONDS

        # Wait for compute to stop, not just for the model to finish its reply.
        while time.monotonic() < deadline:
            status = command("status", run_id)
            if status["compute_state"] == TERMINATED_STATE:
                terminated = True
                break
            time.sleep(POLL_SECONDS)
        if not terminated:
            raise RuntimeError("The VM did not reach TERMINATED before the test deadline.")
        if status["task_status"] != SUCCESS_STATUS:
            raise RuntimeError(f"Run {run_id} ended with {status['task_status']}.")

        # Download after termination to prove that the result outlives the VM.
        destination = REPO_ROOT / ".cloudbox" / "smoke" / run_id
        command("download", run_id, "--output", str(destination))
        artifact = destination / ARTIFACT_PATH
        result = json.loads(artifact.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or set(result) != {"answer"}:
            raise RuntimeError("The downloaded file must contain only an answer field.")
        if type(result["answer"]) is not int or result["answer"] != EXPECTED_ANSWER:
            raise RuntimeError(f"The answer does not equal {EXPECTED_ANSWER}.")
        print(json.dumps({
            "test": "cloud_math", "status": "passed", "run_id": run_id,
            "answer": result["answer"], "artifact_path": str(artifact),
        }))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        if run_id and not terminated:
            try:
                command("cancel", run_id)
            except Exception:
                print("Cancellation failed; the AWS run deadline remains active.", file=sys.stderr)
        print(f"Cloud test failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
