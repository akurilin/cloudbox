"""Check output publication, path limits, and the real Pi socket request."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

import publish  # noqa: E402

RUN_ID = "45e3a9d8-f176-4f28-bd66-622bcd744272"
DOWNLOAD_URL = "https://bucket.s3.test/file?X-Amz-Signature=private-value"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.s3 = Mock()
        self.s3.generate_presigned_url.return_value = DOWNLOAD_URL
        self.publisher = publish.ArtifactPublisher(
            self.s3,
            "bucket",
            RUN_ID,
            self.workspace,
            time.monotonic() + 60,
            (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        self.addCleanup(self.publisher.close)

    def write(self, name="data.bin", content=b"\x00\xffoutput"):
        path = self.publisher.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_upload_matches_content_and_records_private_download_metadata(self):
        content = b"n,square\n1,1\n2,4\n"
        self.write("squares.csv", content)
        artifact = self.publisher.publish("output/squares.csv")
        self.assertEqual(content, self.s3.put_object.call_args.kwargs["Body"])
        arguments = self.s3.put_object.call_args.kwargs
        self.assertEqual("bucket", arguments["Bucket"])
        self.assertEqual("*", arguments["IfNoneMatch"])
        self.assertNotIn("ACL", arguments)
        self.assertEqual("text/csv", arguments["ContentType"])
        self.assertRegex(
            artifact["key"], rf"^runs/{RUN_ID}/artifacts/[a-f0-9-]+/squares.csv$"
        )
        self.assertEqual(len(content), artifact["bytes"])
        self.assertEqual(hashlib.sha256(content).hexdigest(), artifact["sha256"])
        self.assertEqual(DOWNLOAD_URL, artifact["url"])
        self.assertEqual([artifact], self.publisher.snapshot())
        parameters = self.s3.generate_presigned_url.call_args.kwargs
        self.assertIn("attachment", parameters["Params"]["ResponseContentDisposition"])
        self.assertLess(parameters["ExpiresIn"], publish.URL_LIFETIME_SECONDS)

    def test_absolute_nested_paths_and_safe_filenames(self):
        path = self.write("nested/.. bad name.csv")
        artifact = self.publisher.publish(str(path))
        self.assertEqual("bad_name.csv", artifact["name"])
        self.assertTrue(artifact["key"].endswith("/bad_name.csv"))

    def test_repeated_publications_use_unique_keys(self):
        path = self.write()
        first = self.publisher.publish(str(path))
        second = self.publisher.publish(str(path))
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(2, len(self.publisher.snapshot()))

    def test_traversal_and_non_output_paths_fail_before_storage(self):
        self.write()
        for path in (
            "data.bin",
            "output/../output/data.bin",
            "/etc/passwd",
            "output",
            "",
            None,
        ):
            with self.subTest(path=path), self.assertRaises(publish.PublishError):
                self.publisher.publish(path)
        self.s3.put_object.assert_not_called()
        self.s3.generate_presigned_url.assert_not_called()

    def test_symlinks_hardlinks_directories_and_pipes_are_rejected(self):
        original = self.write()
        (self.publisher.output / "symlink").symlink_to(original)
        os.link(original, self.publisher.output / "hardlink")
        (self.publisher.output / "linked-directory").symlink_to(self.publisher.output)
        (self.publisher.output / "directory").mkdir()
        os.mkfifo(self.publisher.output / "pipe")
        for name in (
            "symlink",
            "hardlink",
            "linked-directory/data.bin",
            "directory",
            "pipe",
        ):
            with self.subTest(name=name), self.assertRaises(publish.PublishError):
                self.publisher.publish(f"output/{name}")
        self.s3.put_object.assert_not_called()

    def test_replaced_output_directory_cannot_redirect_file_reads(self):
        content = b"original output"
        self.write(content=content)
        outside = self.workspace / "outside"
        outside.mkdir()
        (outside / "data.bin").write_bytes(b"outside data")
        self.publisher.output.rename(self.workspace / "moved-output")
        self.publisher.output.symlink_to(outside)
        self.publisher.publish("output/data.bin")
        self.assertEqual(content, self.s3.put_object.call_args.kwargs["Body"])

    def test_file_size_total_size_and_count_limits(self):
        self.write(content=b"12345")
        with patch.object(publish, "MAX_ARTIFACT_BYTES", 4):
            with self.assertRaisesRegex(publish.PublishError, "32 MiB"):
                self.publisher.publish("output/data.bin")
        with patch.object(publish, "MAX_TOTAL_ARTIFACT_BYTES", 9):
            self.publisher.publish("output/data.bin")
            with self.assertRaisesRegex(publish.PublishError, "128 MiB"):
                self.publisher.publish("output/data.bin")
        with patch.object(publish, "MAX_ARTIFACTS", 1):
            with self.assertRaisesRegex(publish.PublishError, "32-file"):
                self.publisher.publish("output/data.bin")
        self.assertEqual(1, self.s3.put_object.call_count)

    def test_failed_upload_returns_safe_error_and_can_be_retried(self):
        path = self.write()
        self.s3.put_object.side_effect = RuntimeError("private access token")
        with self.assertRaises(publish.PublishError) as error:
            self.publisher.publish(str(path))
        self.assertNotIn("private", str(error.exception))
        self.assertEqual([], self.publisher.snapshot())
        self.assertEqual(0, self.publisher.total_bytes)
        self.s3.put_object.side_effect = None
        artifact = self.publisher.publish(str(path))
        self.assertEqual([artifact], self.publisher.snapshot())

    def test_deadline_and_credential_expiry_prevent_upload(self):
        path = self.write()
        self.publisher.deadline = time.monotonic()
        with self.assertRaisesRegex(publish.PublishError, "run time"):
            self.publisher.publish(str(path))
        self.publisher.deadline = time.monotonic() + 60
        self.publisher.credentials_expire = time.time()
        with self.assertRaisesRegex(publish.PublishError, "expire"):
            self.publisher.publish(str(path))
        self.s3.put_object.assert_not_called()

    def test_link_expiry_uses_signing_time_not_upload_completion(self):
        path = self.write()
        signed_at = int(time.time())
        self.publisher.credentials_expire = signed_at + 100
        with patch.object(publish.time, "time", return_value=signed_at):
            artifact = self.publisher.publish(str(path))
        expected = signed_at + 100 - publish.CREDENTIAL_EXPIRY_MARGIN_SECONDS
        self.assertEqual(
            expected, datetime.fromisoformat(artifact["expires_at"]).timestamp()
        )

    def test_pi_extension_publishes_through_the_real_socket(self):
        self.write(content=b"socket output")
        service = publish.PublishService(self.publisher)
        self.addCleanup(service.close)
        environment = service.start()
        script = (
            "const { default: registerPublish, PUBLISH_SOCKET_ENV } = await import(process.argv[1]);"
            "process.env[PUBLISH_SOCKET_ENV] = process.argv[2];"
            "let tool; registerPublish({registerTool: value => {tool = value}});"
            "const result = await tool.execute('test-call', {path: 'output/data.bin'});"
            "process.stdout.write(JSON.stringify(result));"
        )
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                script,
                (ROOT / "worker" / "publish.mjs").as_uri(),
                environment[publish.PUBLISH_SOCKET_ENV],
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        reply = json.loads(result.stdout)
        self.assertEqual(DOWNLOAD_URL, reply["details"]["artifact"]["url"])
        content = json.loads(reply["content"][0]["text"])
        receipt = Path(content.pop("receipt_path"))
        self.addCleanup(shutil.rmtree, receipt.parent)
        self.assertEqual(reply["details"]["receipt_path"], str(receipt))
        self.assertEqual(reply["details"]["artifact"], content)
        self.assertEqual(reply["details"]["artifact"], json.loads(receipt.read_text()))
        self.assertEqual(b"socket output", self.s3.put_object.call_args.kwargs["Body"])

    def test_socket_errors_do_not_expose_request_data(self):
        service = publish.PublishService(self.publisher)
        self.addCleanup(service.close)
        service.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(service.socket_path)
            connection.sendall(b'{"path":"output/a", "secret":"do-not-log"}\n')
            response = connection.recv(publish.MAX_REQUEST_BYTES)
        self.assertNotIn(b"do-not-log", response)
        self.assertIn("error", json.loads(response))
        self.s3.put_object.assert_not_called()

    def test_manifest_read_does_not_wait_for_an_active_upload(self):
        captured = []
        reader = threading.Thread(
            target=lambda: captured.append(self.publisher.snapshot()), daemon=True
        )
        self.publisher.lock.acquire()
        try:
            reader.start()
            reader.join(timeout=0.1)
            self.assertFalse(
                reader.is_alive(), "Manifest read waited on the upload lock."
            )
            self.assertEqual([[]], captured)
        finally:
            self.publisher.lock.release()
            reader.join(timeout=1)

    def test_stalled_upload_cannot_block_service_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        failures = []
        self.write()

        def stall_upload(**_arguments):
            entered.set()
            release.wait(timeout=5)

        self.s3.put_object.side_effect = stall_upload
        service = publish.PublishService(self.publisher)
        service.start()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(service.socket_path)
        connection.sendall(b'{"path":"output/data.bin"}\n')
        self.assertTrue(entered.wait(timeout=1))

        def close_service():
            try:
                service.close()
            except TimeoutError as error:
                failures.append(error)

        closer = threading.Thread(target=close_service, daemon=True)
        try:
            with patch.object(publish, "SHUTDOWN_TIMEOUT_SECONDS", 0.05, create=True):
                closer.start()
                closer.join(timeout=0.2)
                self.assertFalse(
                    closer.is_alive(), "Shutdown waited for the stalled upload."
                )
                self.assertEqual(1, len(failures))
        finally:
            release.set()
            closer.join(timeout=1)
            connection.close()
            service.close()


if __name__ == "__main__":
    unittest.main()
