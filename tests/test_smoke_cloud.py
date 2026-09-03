"""Check smoke links and job cleanup without AWS."""

import hashlib
import io
import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import Mock, patch

from cloudbox.common import CloudboxError
from scripts import smoke_cloud

RUN_ID = "00000000-0000-4000-8000-000000000001"
ARTIFACT_ID = "00000000-0000-4000-8000-000000000002"
DEPLOYMENT = {"bucket_name": "cloudbox-test", "aws_region": "us-east-1"}
RUNNING_STATE = "RUNNING"


class SmokeCloudTests(unittest.TestCase):
    def test_downloads_exact_urls_in_common_response_formats(self):
        artifacts, contents = [], {}
        for name in sorted(smoke_cloud.EXPECTED_NAMES):
            key = f"runs/{RUN_ID}/artifacts/{ARTIFACT_ID}/{name}"
            url = (
                f"https://cloudbox-test.s3.us-east-1.amazonaws.com/{key}?signature=abc"
            )
            body = name.encode()
            contents[url] = body
            artifacts.append(
                {
                    "name": name,
                    "key": key,
                    "url": url,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            )

        def download(request, *, timeout):
            remote = io.BytesIO(contents[request.full_url])
            remote.status = HTTPStatus.OK
            return remote

        # Response punctuation must not become part of the signed URL.
        for template in ("[file]({url})", "`{url}`", "Download {url}."):
            with (
                self.subTest(template=template),
                tempfile.TemporaryDirectory() as directory,
            ):
                opener = Mock()
                opener.open.side_effect = download
                response = "\n".join(
                    template.format(url=item["url"]) for item in artifacts
                )
                with patch.object(smoke_cloud, "build_opener", return_value=opener):
                    files = smoke_cloud.download_urls(
                        response, artifacts, DEPLOYMENT, RUN_ID, Path(directory)
                    )
                self.assertEqual(
                    files, {item["name"]: contents[item["url"]] for item in artifacts}
                )
                self.assertEqual(opener.open.call_count, len(artifacts))
                for name, body in files.items():
                    self.assertEqual((Path(directory) / name).read_bytes(), body)

    def test_failure_waits_until_its_job_stops(self):
        states = iter((RUNNING_STATE, smoke_cloud.TERMINATED_STATE))

        def execute(environment, prompt, directory, observed, **options):
            observed["run_id"] = RUN_ID
            raise CloudboxError("exec_failed", "The test job failed.")

        def command(environment, operation, identity):
            self.assertEqual(identity, RUN_ID)
            if operation == "cancel":
                return {
                    "ok": True,
                    "cancel_requested": True,
                    "compute_state": RUNNING_STATE,
                }
            self.assertEqual(operation, "status")
            return {
                "ok": True,
                "compute_state": next(states, smoke_cloud.TERMINATED_STATE),
            }

        # A stop request can return before the VM reaches its stopped state.
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(smoke_cloud, "load_deployment", return_value=DEPLOYMENT),
            patch.object(smoke_cloud, "execute", side_effect=execute),
            patch.object(smoke_cloud, "command", side_effect=command) as cli,
            patch.object(smoke_cloud.time, "sleep"),
            patch.object(smoke_cloud, "emit"),
        ):
            code = smoke_cloud.main(["--env", "test", "--output-directory", directory])
            snapshot = json.loads(
                next(Path(directory).glob("*/status.json")).read_text()
            )
        self.assertEqual(code, 1)
        self.assertEqual(snapshot["compute_state"], smoke_cloud.TERMINATED_STATE)
        self.assertEqual(
            sum(call.args[1] == "cancel" for call in cli.call_args_list), 1
        )
        self.assertGreaterEqual(
            sum(call.args[1] == "status" for call in cli.call_args_list), 2
        )


if __name__ == "__main__":
    unittest.main()
