"""Check readable output without changing saved run records."""

import json
import os
import time
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from cloudbox.output import render_log, render_result, terminal_text

RUN_ID = "80c7a578-1d79-4c99-b08a-4741eaf3b795"
OTHER_RUN_ID = "80c7a578-1d79-4c99-b08a-4741eaf3b796"
SIGNED_URL = "https://example.test/file?X-Amz-Signature=private"


def saved_run(**changes):
    return {
        "run_id": RUN_ID,
        "task_status": "succeeded",
        "compute_state": "TERMINATED",
        "submitted_at": "2026-09-03T00:00:00+00:00",
        "launch": {"started_at": "2026-09-03T00:00:02+00:00"},
        "result": {
            "started_at": "2026-09-03T00:00:03+00:00",
            "finished_at": "2026-09-03T00:01:05+00:00",
            "reason": "completed",
            "exit_code": 0,
            "report": {"summary": "Created a chart.", "response": SIGNED_URL},
            "artifacts": [{"name": "chart.png", "bytes": 2048, "url": SIGNED_URL}],
        },
        **changes,
    }


class ResultOutputTests(unittest.TestCase):
    def test_list_has_one_row_per_run_without_saved_envelopes(self):
        record = {
            "environment": "test",
            "runs": [saved_run(), saved_run(run_id=OTHER_RUN_ID, result=None)],
        }
        with patch(
            "cloudbox.output.shutil.get_terminal_size",
            return_value=os.terminal_size((120, 30)),
        ):
            text = render_result("list", record)
        self.assertIn("Environment: test", text)
        self.assertIn("RUN", text)
        self.assertIn("WHEN (LOCAL)", text)
        self.assertIn("DURATION", text)
        self.assertIn("TASK", text)
        self.assertIn("VM", text)
        self.assertIn("FILES", text)
        self.assertIn("SUMMARY", text)
        self.assertEqual(1, text.count(RUN_ID))
        self.assertEqual(1, text.count(OTHER_RUN_ID))
        self.assertIn("Created a chart.", text)
        self.assertNotIn(SIGNED_URL, text)
        self.assertNotIn("artifacts", text)

    def test_narrow_list_keeps_ids_and_fits_terminal(self):
        run = saved_run()
        run["result"]["report"]["summary"] = "A long summary. " * 30
        with patch(
            "cloudbox.output.shutil.get_terminal_size",
            return_value=os.terminal_size((40, 30)),
        ):
            text = render_result("list", {"environment": "test", "runs": [run]})
        self.assertIn(RUN_ID[:8], text)
        self.assertNotIn(RUN_ID, text)
        self.assertIn("When (local):", text)
        self.assertIn("Duration: 1m 2s", text)
        self.assertIn("Task: succeeded", text)
        self.assertIn("VM: TERMINATED", text)
        self.assertLessEqual(max(map(len, text.splitlines())), 40)

    def test_list_uses_global_labels_to_avoid_hidden_collisions(self):
        record = {
            "runs": [saved_run()],
            "run_labels": {RUN_ID: RUN_ID[:10]},
        }
        before = json.dumps(record)
        text = render_result("list", record)
        self.assertIn(RUN_ID[:10] + " ", text)
        self.assertNotIn(RUN_ID, text)
        self.assertEqual(before, json.dumps(record))

    def test_list_converts_start_time_to_local_date(self):
        try:
            with patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                text = render_result("list", {"runs": [saved_run()]})
        finally:
            time.tzset()
        self.assertIn("2026-09-02 20:00", text)
        self.assertIn("1m 2s", text)

    def test_list_uses_valid_launch_or_submission_when_start_is_missing(self):
        run = saved_run()
        run["result"]["started_at"] = "invalid"
        run["launch"]["started_at"] = "2026-09-03T00:00:04+00:00"
        try:
            with patch.dict(os.environ, {"TZ": "UTC"}):
                time.tzset()
                text = render_result("list", {"runs": [run]})
                self.assertIn("1m 1s", text)
                run["launch"]["started_at"] = "invalid"
                run["submitted_at"] = "2026-08-31T23:59:00+00:00"
                with patch(
                    "cloudbox.output.shutil.get_terminal_size",
                    return_value=os.terminal_size((40, 30)),
                ):
                    text = render_result("list", {"runs": [run]})
        finally:
            time.tzset()
        self.assertIn("When (local): 2026-08-31 23:59", text)
        self.assertIn("Duration: -", text)

    def test_list_marks_elapsed_time_only_for_active_runs(self):
        active = saved_run(result=None, compute_state="RUNNING")
        with patch("cloudbox.output.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 3, 0, 1, 5, tzinfo=UTC)
            text = render_result("list", {"runs": [active]})
            self.assertIn("1m 3s+", text)
            self.assertEqual(1, text.count("+ = elapsed; run is active."))
            completed = render_result(
                "list", {"runs": [saved_run(compute_state="RUNNING")]}
            )
            self.assertNotIn("+", completed)
        with patch(
            "cloudbox.output.shutil.get_terminal_size",
            return_value=os.terminal_size((40, 30)),
        ):
            active["compute_state"] = "TERMINATED"
            stopped = render_result("list", {"runs": [active]})
            self.assertIn("Duration: -", stopped)
            missing = render_result("list", {"runs": [{"run_id": RUN_ID}]})
            self.assertIn("When (local): -", missing)
            self.assertIn("Duration: -", missing)
        self.assertNotIn("+", stopped)

    def test_empty_page_keeps_next_command(self):
        text = render_result(
            "list",
            {
                "environment": "test",
                "runs": [],
                "next_cursor": "next-token",
                "next_command": (
                    "cloudbox --env test list --status failed --cursor next-token"
                ),
            },
        )
        self.assertIn("No runs.", text)
        self.assertIn("--status failed --cursor next-token", text)

    def test_status_shows_timing_reason_and_files(self):
        text = render_result("status", {"environment": "test", **saved_run()})
        for expected in (
            RUN_ID,
            "Task: succeeded",
            "VM: TERMINATED",
            "Submitted:",
            "Started:",
            "Finished:",
            "Duration: 1m 2s",
            "Reason: completed",
            "Exit code: 0",
            "chart.png (2 KiB)",
            "Created a chart.",
        ):
            self.assertIn(expected, text)
        self.assertNotIn(SIGNED_URL, text)

    def test_status_handles_legacy_missing_and_invalid_report_fields(self):
        for result in (
            None,
            {"artifact_key": "old.zip", "report": "legacy"},
            {"report": {"summary": None}, "artifacts": None},
        ):
            with self.subTest(result=result):
                text = render_result("status", {"run_id": RUN_ID, "result": result})
                self.assertIn("Task: unknown", text)
                self.assertIn("VM: unknown", text)
                self.assertNotIn("None", text)

    def test_status_does_not_infer_task_success_from_vm_state(self):
        text = render_result("status", saved_run(task_status="unknown", result=None))
        self.assertIn("Task: unknown", text)
        self.assertIn("VM: TERMINATED", text)
        self.assertNotIn("succeeded", text)

    def test_submit_and_cancel_do_not_claim_an_unconfirmed_stop(self):
        record = {
            "run_id": RUN_ID,
            "compute_state": "RUNNING",
            "cancel_requested": True,
        }
        self.assertIn("Stop requested", render_result("cancel", record))
        self.assertNotIn("VM stopped", render_result("cancel", record))
        record["compute_state"] = "TERMINATED"
        self.assertIn("VM stopped", render_result("cancel", record))
        record["launch_record_saved"] = False
        text = render_result("submit", record)
        self.assertIn(RUN_ID, text)
        self.assertIn("Launch record was not saved", text)

    def test_download_shows_paths_and_incomplete_warning(self):
        text = render_result(
            "download",
            {
                "run_id": RUN_ID,
                "directory": "/tmp/download",
                "incomplete": True,
                "task_status": "failed",
                "files": [{"path": "/tmp/download/spec.json", "bytes": 21}],
            },
        )
        self.assertIn("/tmp/download/spec.json", text)
        self.assertIn("incomplete", text)
        self.assertIn("failed", text)

    def test_links_show_only_requested_urls_with_expiration(self):
        text = render_result(
            "links",
            {
                "run_id": RUN_ID,
                "artifacts": [
                    {
                        "name": "chart.png",
                        "bytes": 2048,
                        "url": SIGNED_URL,
                        "expires_at": "2026-09-03T01:00:00+00:00",
                    }
                ],
            },
        )
        self.assertIn("chart.png (2 KiB)", text)
        self.assertIn("Expires:", text)
        self.assertIn(SIGNED_URL, text)
        self.assertIn("No output files.", render_result("links", {"run_id": RUN_ID}))

    def test_remote_fields_cannot_emit_terminal_commands(self):
        run = saved_run()
        run["result"]["report"]["summary"] = "\x1b[31mDone\x1b[0m\r\u202e\x00"
        text = render_result("status", run)
        self.assertIn("Done", text)
        for control in ("\x1b", "\r", "\u202e", "\x00"):
            self.assertNotIn(control, text)
        self.assertEqual(
            "Link\nnext\tcell",
            terminal_text("\x1b]8;;https://x\x07Link\x1b]8;;\x07\nnext\tcell"),
        )


class LogOutputTests(unittest.TestCase):
    def event(self, **record):
        return {"timestamp": 1788393600000, "message": json.dumps(record)}

    def test_model_text_is_readable_with_timestamp_and_source(self):
        text = render_log(
            self.event(source="agent", event="model_message", text="Done.\nNext line.")
        )
        self.assertIn("2026-09-03T00:00:00Z [agent] model message: Done.", text)
        self.assertIn("  Next line.", text)
        self.assertNotIn('"text"', text)

    def test_model_text_with_only_terminal_commands_is_empty(self):
        text = render_log(
            self.event(event="model_message", text="\x1b[31m\x1b[0m\r\u202e\x00")
        )
        self.assertIn("[agent] model message", text)
        self.assertNotIn("\x1b", text)

    def test_other_json_keeps_message(self):
        event = {"message": '{"message":"disk full"}'}
        self.assertEqual(event["message"], render_log(event))

    def test_invalid_source_is_safe(self):
        text = render_log(self.event(source=[], event="model_message", text="Done."))
        self.assertIn("[agent] model message: Done.", text)

    def test_tool_events_show_command_result_and_outcome(self):
        start = render_log(
            self.event(
                event="tool_execution_start",
                tool_name="bash",
                arguments={"command": "pwd", "irrelevant": "private"},
            )
        )
        self.assertIn("[agent] tool start: bash", start)
        self.assertIn("command: pwd", start)
        self.assertNotIn("private", start)
        end = render_log(
            self.event(
                event="tool_execution_end",
                tool_name="bash",
                outcome="ok",
                duration_seconds=1.25,
                result={"content": [{"type": "text", "text": "/work"}]},
            )
        )
        self.assertIn("tool end: bash", end)
        self.assertIn("ok", end)
        self.assertIn("1.25s", end)
        self.assertIn("/work", end)

    def test_supervisor_and_plain_logs_keep_readable_details(self):
        text = render_log(
            self.event(
                source="supervisor",
                event="result_saved",
                status="failed",
                written=True,
            )
        )
        self.assertIn("[supervisor] result saved", text)
        self.assertIn("status: failed", text)
        self.assertIn("written: yes", text)
        self.assertEqual(
            "plain text", render_log({"message": "\x1b[31mplain text\x1b[0m"})
        )
        self.assertEqual("[]", render_log({"message": "[]"}))


if __name__ == "__main__":
    unittest.main()
