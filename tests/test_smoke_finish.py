"""Check math results from finish reports, with no cloud calls."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import smoke_cloud

RUN_ID = "3d752dd6-38ce-4e0b-8bf3-edbd5151d191"


class SmokeFinishTests(unittest.TestCase):
    def smoke(self, report_status="completed", run_status="succeeded", answer=None):
        calls = []
        result = {"schema_version": 3, "status": run_status, "report": {
            "status": report_status, "summary": "Calculation checked.",
            "result": {"answer": smoke_cloud.EXPECTED_ANSWER if answer is None else answer},
        }}

        def command(environment, *arguments):
            calls.append(arguments)
            if arguments[0] == "submit":
                return {"run_id": RUN_ID}
            if arguments[0] == "status":
                return {"task_status": "succeeded", "compute_state": "TERMINATED"}
            if arguments[0] == "download":
                destination = Path(arguments[3])
                destination.mkdir(parents=True)
                (destination / "result.json").write_text(json.dumps(result))
                return {"ok": True}
            self.fail(f"Unexpected CLI command: {arguments[0]}")

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(smoke_cloud, "get_environment", return_value=SimpleNamespace(name="test")), \
             patch.object(smoke_cloud, "command", side_effect=command), \
             patch.object(smoke_cloud, "listed_run", return_value={"task_status": "succeeded", "compute_state": "TERMINATED"}), \
             patch.object(smoke_cloud, "log_count", return_value=1), \
             redirect_stdout(io.StringIO()) as output:
            code = smoke_cloud.main(["--env", "test", "--output-directory", directory])
        return code, json.loads(output.getvalue().splitlines()[-1]), calls

    def test_success_needs_only_the_saved_finish_report(self):
        code, result, calls = self.smoke()
        self.assertEqual(code, 0)
        self.assertEqual(result["answer"], smoke_cloud.EXPECTED_ANSWER)
        self.assertTrue(result["result_path"].endswith("/result.json"))
        self.assertNotIn("artifact_path", result)
        self.assertNotIn("output/result.json", calls[0][1])

    def test_report_and_supervisor_status_must_both_succeed(self):
        for report_status, run_status in (("blocked", "succeeded"), ("completed", "failed")):
            with self.subTest(report_status=report_status, run_status=run_status):
                code, result, _ = self.smoke(report_status=report_status, run_status=run_status)
                self.assertEqual(code, 1)
                self.assertEqual(result["error"]["code"], "invalid_report")

    def test_report_answer_must_be_the_expected_integer(self):
        for answer in (True, str(smoke_cloud.EXPECTED_ANSWER), smoke_cloud.EXPECTED_ANSWER + 1):
            with self.subTest(answer=answer):
                code, result, _ = self.smoke(answer=answer)
                self.assertEqual(code, 1)
                self.assertEqual(result["error"]["code"], "wrong_answer")


if __name__ == "__main__":
    unittest.main()
