"""Wait for image builds before cleanup, without calling AWS."""

import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError, EndpointConnectionError

from cloudbox.common import CloudboxError
from scripts import build_image, teardown

IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:cloudbox-worker"
NAMES = {"image_arn": IMAGE_ARN}
CONFIG = {"project_name": "cloudbox"}
TAGS = {"Project": "cloudbox", "ManagedBy": build_image.IMAGE_OWNER}
TEST_WAIT_SECONDS = 25
IMAGE_NAME = "cloudbox-worker"


class Clock:
    def __init__(self):
        self.now = 0

    def sleep(self, seconds):
        self.now += seconds


class ImageCleanupTests(unittest.TestCase):
    def client(self, parent_states, version_states):
        client = Mock()
        current = {"parent": None, "version": None, "deleted": False}
        parents, versions = iter(parent_states), iter(version_states)

        def image(**kwargs):
            if current["deleted"]:
                return {"state": "DELETED"}
            current["parent"] = next(parents, current["parent"])
            return {"state": current["parent"], "tags": TAGS}

        def list_versions(**kwargs):
            current["version"] = next(versions, current["version"])
            return {"items": [{"imageVersion": "1.0", "state": current["version"]}]}

        def delete(**kwargs):
            # AWS refuses a delete while any image version still builds.
            if (
                current["parent"] in {"CREATING", "UPDATING"}
                or current["version"] != "SUCCESSFUL"
            ):
                raise ClientError(
                    {"Error": {"Code": "ValidationException"}}, "DeleteMicrovmImage"
                )
            current["deleted"] = True

        client.get_microvm_image.side_effect = image
        client.list_microvm_image_versions.side_effect = list_versions
        client.delete_microvm_image.side_effect = delete
        client.list_microvms.return_value = {"items": []}
        return client

    def test_teardown_waits_for_parent_and_version_builds_before_delete(self):
        client = self.client(
            ["CREATING", "CREATED", "CREATED"], ["IN_PROGRESS", "SUCCESSFUL"]
        )
        session = Mock()
        session.client.return_value = client
        with patch.object(build_image.time, "sleep"):
            teardown.remove_compute(session, NAMES, CONFIG)
        client.delete_microvm_image.assert_called_once_with(imageIdentifier=IMAGE_ARN)

    def test_deletion_wait_retries_transient_dns_failure(self):
        client = Mock()
        client.get_microvm_image.side_effect = [
            EndpointConnectionError(endpoint_url="https://test.invalid"),
            {"state": "DELETED"},
        ]
        with patch.object(build_image.time, "sleep"):
            build_image.wait_for_deletion(client, IMAGE_ARN)
        self.assertEqual(client.get_microvm_image.call_count, 2)

    def test_active_build_wait_is_bounded_without_delete(self):
        client = self.client(["CREATED"], ["IN_PROGRESS"])
        session = Mock()
        session.client.return_value = client
        clock = Clock()
        with (
            patch.object(build_image, "BUILD_WAIT_SECONDS", TEST_WAIT_SECONDS),
            patch.object(build_image.time, "monotonic", side_effect=lambda: clock.now),
            patch.object(build_image.time, "sleep", side_effect=clock.sleep),
        ):
            with self.assertRaises(CloudboxError):
                teardown.remove_compute(session, NAMES, CONFIG)
        self.assertEqual(clock.now, TEST_WAIT_SECONDS)
        client.delete_microvm_image.assert_not_called()

    def test_transient_read_failures_stop_at_deadline_without_delete(self):
        client = Mock()
        client.get_microvm_image.side_effect = EndpointConnectionError(
            endpoint_url="https://test.invalid"
        )
        clock = Clock()
        with (
            patch.object(build_image, "BUILD_WAIT_SECONDS", TEST_WAIT_SECONDS),
            patch.object(build_image.time, "monotonic", side_effect=lambda: clock.now),
            patch.object(build_image.time, "sleep", side_effect=clock.sleep),
        ):
            with self.assertRaises(CloudboxError) as raised:
                build_image.wait_until_deletable(client, IMAGE_ARN, wait=True)
        self.assertEqual(raised.exception.code, "image_check_unavailable")
        self.assertNotIn("https://test.invalid", str(raised.exception))
        self.assertEqual(clock.now, TEST_WAIT_SECONDS)
        client.delete_microvm_image.assert_not_called()

    def test_both_delete_paths_disable_sdk_write_retries_only(self):
        for entrypoint in ("teardown", "image_command"):
            with self.subTest(entrypoint=entrypoint):
                read_client, write_client, session = Mock(), Mock(), Mock()
                read_client.get_microvm_image.return_value = {
                    "state": "CREATED",
                    "tags": TAGS,
                }
                read_client.list_microvm_image_versions.return_value = {
                    "items": [{"state": "SUCCESSFUL"}]
                }
                read_client.list_microvms.return_value = {"items": []}

                # Bind each case's clients before installing the callback.
                def client(
                    service,
                    *,
                    config,
                    read_client=read_client,
                    write_client=write_client,
                ):
                    return (
                        write_client
                        if config.retries.get("total_max_attempts") == 1
                        else read_client
                    )

                session.client.side_effect = client
                if entrypoint == "teardown":
                    with patch.object(teardown, "wait_for_deletion") as wait:
                        teardown.remove_compute(session, NAMES, CONFIG)
                else:
                    deployment = {"image_name": IMAGE_NAME, "image_arn": IMAGE_ARN}
                    with (
                        patch.object(
                            build_image, "load_deployment", return_value=deployment
                        ),
                        patch.object(
                            build_image, "operator_session", return_value=session
                        ),
                        patch.object(build_image, "emit"),
                        patch.object(build_image, "wait_for_deletion") as wait,
                    ):
                        self.assertEqual(
                            build_image.main(
                                [
                                    "--env",
                                    "test",
                                    "delete",
                                    "--confirm-name",
                                    IMAGE_NAME,
                                    "--wait",
                                ]
                            ),
                            0,
                        )
                write_client.delete_microvm_image.assert_called_once_with(
                    imageIdentifier=IMAGE_ARN
                )
                read_client.delete_microvm_image.assert_not_called()
                wait.assert_called_once_with(read_client, IMAGE_ARN)


if __name__ == "__main__":
    unittest.main()
