"""Read complete finish reports and retain historical run downloads."""

import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from botocore.exceptions import ClientError
from cloudbox.cli import Runs
from cloudbox.common import CloudboxError, MAX_PROMPT_CHARACTERS, get_record, validate_spec

RUN_ID = "3d752dd6-38ce-4e0b-8bf3-edbd5151d191"
REPORT_LIMIT_BYTES = 1_048_576
METADATA_RESERVE_BYTES = 16_384
FINISH_SCHEMA_VERSION = 3


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def maximum_result():
    report = {"status": "completed", "summary": "Done.", "result": {"text": ""}}
    report["result"]["text"] = "x" * (REPORT_LIMIT_BYTES - len(encoded(report)))
    return {"schema_version": FINISH_SCHEMA_VERSION, "run_id": RUN_ID, "status": "succeeded", "report": report}


class FinishClientTests(unittest.TestCase):
    def runs(self):
        runs = Runs.__new__(Runs)
        runs.bucket = "test-bucket"
        runs.environment = SimpleNamespace(name="test")
        runs.compute = Mock()
        runs.s3 = Mock()
        return runs

    def test_maximum_report_fits_record_read_and_download(self):
        result = maximum_result()
        body = encoded(result)
        self.assertEqual(len(encoded(result["report"])), REPORT_LIMIT_BYTES)
        self.assertGreater(len(body), REPORT_LIMIT_BYTES)
        runs = self.runs()

        def get_object(**kwargs):
            if kwargs["Key"].split("/")[-1] == "result.json" and "/output/" not in kwargs["Key"]:
                return {"Body": io.BytesIO(body)}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        runs.s3.get_object.side_effect = get_object
        self.assertEqual(get_record(runs.s3, runs.bucket, "result.json"), result)
        runs.status = Mock(return_value={"task_status": "succeeded"})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "download"
            downloaded = runs.download(RUN_ID, destination)
            self.assertFalse(downloaded["incomplete"])
            self.assertEqual((destination / "result.json").read_bytes(), body)
            self.assertFalse((destination / "output/result.json").exists())

    def test_report_plus_reserve_limit_is_enforced(self):
        runs = self.runs()
        value = {"result": ""}
        value["result"] = "x" * (REPORT_LIMIT_BYTES + METADATA_RESERVE_BYTES + 1 - len(encoded(value)))
        runs.s3.get_object.return_value = {"Body": io.BytesIO(encoded(value))}
        with self.assertRaises(CloudboxError) as raised:
            get_record(runs.s3, runs.bucket, "result.json")
        self.assertEqual(raised.exception.code, "record_invalid")

    def test_historical_artifact_downloads_remain_available(self):
        for version in (1, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                runs = self.runs()
                runs.status = Mock(return_value={"task_status": "succeeded"})
                records = {"result.json": encoded({"schema_version": version, "status": "succeeded"}),
                           "output/result.json": encoded({"answer": 42})}

                def get_object(**kwargs):
                    name = kwargs["Key"].removeprefix(f"runs/{RUN_ID}/")
                    if name not in records:
                        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
                    return {"Body": io.BytesIO(records[name])}

                runs.s3.get_object.side_effect = get_object
                destination = Path(directory) / "download"
                runs.download(RUN_ID, destination)
                self.assertEqual((destination / "output/result.json").read_bytes(), records["output/result.json"])

    def test_cancel_preserves_schema_and_legacy_fields_only_for_old_runs(self):
        for version, launch_schema in ((1, 1), (2, 2), (3, 3), (3, None)):
            with self.subTest(version=version, launch_schema=launch_schema):
                runs = self.runs()
                launch = {"microvm_id": "test-vm", "started_at": "test-start"}
                if launch_schema is not None:
                    launch["schema_version"] = launch_schema
                before = {"launch": launch, "compute_state": "RUNNING", "result": None}
                after = {**before, "compute_state": "TERMINATED"}
                runs.status = Mock(side_effect=[before, after, after])
                runs.record = Mock(return_value={"schema_version": version})
                runs.cancel(RUN_ID)
                result = json.loads(runs.s3.put_object.call_args.kwargs["Body"])
                self.assertEqual(result["schema_version"], version)
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual("artifact_key" in result, version < FINISH_SCHEMA_VERSION)
                self.assertEqual("artifact_complete" in result, version < FINISH_SCHEMA_VERSION)

    def test_user_input_schema_and_prompt_limit_do_not_change(self):
        spec = {"schema_version": 1, "prompt": "x" * MAX_PROMPT_CHARACTERS, "model": "test", "timeout_seconds": 60}
        self.assertEqual(validate_spec(spec), spec)
        for invalid in ({**spec, "schema_version": FINISH_SCHEMA_VERSION}, {**spec, "prompt": spec["prompt"] + "x"}):
            with self.assertRaises(CloudboxError):
                validate_spec(invalid)


if __name__ == "__main__":
    unittest.main()
