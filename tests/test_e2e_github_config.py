"""Protect the replacement key before a disposable test reset."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from cloudbox.common import CloudboxError
from scripts import e2e_cloud


class GitHubRebuildInputs(unittest.TestCase):
    def test_missing_key_stops_before_resource_access(self):
        with (
            patch.object(
                e2e_cloud, "test_configuration", return_value={"github_app_id": 1}
            ),
            patch.object(e2e_cloud, "Report") as report,
            patch.object(e2e_cloud, "operator_session") as session,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = e2e_cloud.main([])
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue())["error"]["code"], "github_key_required"
        )
        report.assert_not_called()
        session.assert_not_called()

    def test_invalid_key_stops_before_resource_access(self):
        with (
            patch.object(
                e2e_cloud, "test_configuration", return_value={"github_app_id": 1}
            ),
            patch.object(
                e2e_cloud,
                "private_key_from_file",
                side_effect=CloudboxError("bad_key", "Invalid key."),
            ),
            patch.object(e2e_cloud, "Report") as report,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = e2e_cloud.main(["--github-key-file", "/unused/key.pem"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "bad_key")
        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
