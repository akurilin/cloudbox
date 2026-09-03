"""Check run prefixes and pages ordered by submission time."""

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from cloudbox.common import CloudboxError
from cloudbox.run_selection import (
    list_runs,
    resolve_run_id,
    short_run_ids,
    validate_run_reference,
)

OLDER_ID = "11111111-1111-4111-8111-111111111111"
NEWER_ID = "22222222-2222-4222-8222-222222222222"
NEWEST_ID = "33333333-3333-4333-8333-333333333333"
MISSING_DATE_ID = "44444444-4444-4444-8444-444444444444"
SHARED_PREFIX_ID = "11111111-2222-4222-8222-222222222222"
BUCKET = "test-runs"


def page(*identities, cursor=None):
    value = {
        "CommonPrefixes": [{"Prefix": f"runs/{identity}/"} for identity in identities]
    }
    if cursor:
        value["NextContinuationToken"] = cursor
    return value


def arguments(*, limit=2, cursor=None, status=None):
    return SimpleNamespace(limit=limit, cursor=cursor, status=status)


def saved_runs():
    runs = SimpleNamespace(s3=Mock(), bucket=BUCKET)
    specs = {
        OLDER_ID: {"submitted_at": "2026-09-01T12:00:00+00:00"},
        NEWER_ID: {"submitted_at": "2026-09-01T13:00:00+00:00"},
        NEWEST_ID: {"submitted_at": "2026-09-01T14:00:00+00:00"},
        MISSING_DATE_ID: {},
    }
    runs.record = Mock(side_effect=lambda identity, name: specs[identity])
    runs.status = Mock(
        side_effect=lambda identity: {
            "run_id": identity,
            "task_status": "failed" if identity == NEWER_ID else "succeeded",
        }
    )
    runs.s3.list_objects_v2.return_value = page(
        OLDER_ID, NEWER_ID, NEWEST_ID, MISSING_DATE_ID
    )
    return runs


class RunReferenceTests(unittest.TestCase):
    def assert_error(self, code, function, *args):
        with self.assertRaises(CloudboxError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, code)

    def test_accepts_only_canonical_uuid_prefixes(self):
        for value in (OLDER_ID, OLDER_ID[:8], OLDER_ID[:9], OLDER_ID[:20]):
            self.assertEqual(validate_run_reference(value), value)
        for value in (
            "1111111",
            "11111111_",
            "11111111-ABCD",
            "11111111//",
            OLDER_ID + "1",
            None,
        ):
            self.assert_error("invalid_run_id", validate_run_reference, value)

    def test_resolves_prefix_after_all_storage_pages(self):
        s3 = Mock()
        s3.list_objects_v2.side_effect = [
            page("invalid", cursor="next"),
            page(OLDER_ID),
        ]
        self.assertEqual(resolve_run_id(s3, BUCKET, OLDER_ID[:8]), OLDER_ID)
        self.assertEqual(s3.list_objects_v2.call_count, 2)
        self.assertEqual(
            s3.list_objects_v2.call_args_list[0].kwargs,
            {"Bucket": BUCKET, "Prefix": "runs/11111111", "Delimiter": "/"},
        )
        self.assertEqual(
            s3.list_objects_v2.call_args_list[1].kwargs["ContinuationToken"], "next"
        )

    def test_rejects_missing_and_ambiguous_prefixes(self):
        s3 = Mock()
        s3.list_objects_v2.return_value = page()
        self.assert_error("run_not_found", resolve_run_id, s3, BUCKET, OLDER_ID[:8])
        s3.list_objects_v2.side_effect = [
            page(OLDER_ID, cursor="next"),
            page(SHARED_PREFIX_ID),
        ]
        self.assert_error("ambiguous_run_id", resolve_run_id, s3, BUCKET, OLDER_ID[:8])

    def test_full_uuid_does_not_need_discovery(self):
        s3 = Mock()
        self.assertEqual(resolve_run_id(s3, BUCKET, OLDER_ID), OLDER_ID)
        s3.list_objects_v2.assert_not_called()

    def test_short_ids_extend_only_for_collisions(self):
        labels = short_run_ids([OLDER_ID, SHARED_PREFIX_ID, NEWER_ID])
        self.assertEqual(labels[OLDER_ID], "11111111-1")
        self.assertEqual(labels[SHARED_PREFIX_ID], "11111111-2")
        self.assertEqual(labels[NEWER_ID], "22222222")
        for label in labels.values():
            self.assertEqual(validate_run_reference(label), label)


class RunListTests(unittest.TestCase):
    def test_human_ids_account_for_runs_on_other_pages(self):
        runs = saved_runs()
        runs.s3.list_objects_v2.return_value = page(OLDER_ID, SHARED_PREFIX_ID)
        runs.record.return_value = {"submitted_at": "2026-09-01T12:00:00+00:00"}
        runs.record.side_effect = None
        result = list_runs(runs, arguments(limit=1), human=True)
        self.assertEqual(result["runs"][0]["run_id"], SHARED_PREFIX_ID)
        self.assertEqual(result["run_labels"], {SHARED_PREFIX_ID: "11111111-2"})
        runs.s3.list_objects_v2.assert_called_once()
        result = list_runs(runs, arguments(limit=1))
        self.assertNotIn("run_labels", result)
        self.assertEqual(result["runs"][0]["run_id"], SHARED_PREFIX_ID)

    def test_sorts_all_storage_pages_before_selecting_runs(self):
        runs = saved_runs()
        runs.s3.list_objects_v2.side_effect = [
            page(OLDER_ID, "invalid", cursor="storage-next"),
            page(NEWER_ID, NEWEST_ID, MISSING_DATE_ID),
        ]
        result = list_runs(runs, arguments())
        self.assertEqual(
            [row["run_id"] for row in result["runs"]], [NEWEST_ID, NEWER_ID]
        )
        self.assertTrue(result["next_cursor"])
        self.assertEqual(runs.status.call_count, 2)
        self.assertEqual(runs.record.call_count, 4)

    def test_pagination_keeps_anchor_when_newer_runs_arrive(self):
        runs = saved_runs()
        runs.s3.list_objects_v2.return_value = page(OLDER_ID, NEWER_ID)
        first = list_runs(runs, arguments(limit=1))
        runs.s3.list_objects_v2.return_value = page(OLDER_ID, NEWER_ID, NEWEST_ID)
        second = list_runs(runs, arguments(limit=1, cursor=first["next_cursor"]))
        self.assertEqual([row["run_id"] for row in second["runs"]], [OLDER_ID])
        self.assertIsNone(second["next_cursor"])

    def test_filter_applies_before_page_limit(self):
        runs = saved_runs()
        first = list_runs(runs, arguments(limit=2, status="succeeded"))
        self.assertEqual(
            [row["run_id"] for row in first["runs"]], [NEWEST_ID, OLDER_ID]
        )
        self.assertTrue(first["next_cursor"])
        second = list_runs(
            runs, arguments(limit=2, status="succeeded", cursor=first["next_cursor"])
        )
        self.assertEqual([row["run_id"] for row in second["runs"]], [MISSING_DATE_ID])
        self.assertIsNone(second["next_cursor"])

    def test_equivalent_time_zones_use_uuid_tie_breaker(self):
        runs = saved_runs()
        specs = {
            OLDER_ID: {"submitted_at": "2026-09-01T10:00:00-04:00"},
            NEWER_ID: {"submitted_at": "2026-09-01T14:00:00+00:00"},
            MISSING_DATE_ID: {"submitted_at": "invalid"},
        }
        runs.record.side_effect = lambda identity, name: specs[identity]
        runs.s3.list_objects_v2.return_value = page(OLDER_ID, NEWER_ID, MISSING_DATE_ID)
        first = list_runs(runs, arguments(limit=1))
        self.assertEqual(first["runs"][0]["run_id"], NEWER_ID)
        second = list_runs(runs, arguments(limit=2, cursor=first["next_cursor"]))
        self.assertEqual(
            [row["run_id"] for row in second["runs"]], [OLDER_ID, MISSING_DATE_ID]
        )

    def test_empty_list_has_no_cursor(self):
        runs = saved_runs()
        runs.s3.list_objects_v2.return_value = page()
        self.assertEqual(
            list_runs(runs, arguments()), {"ok": True, "runs": [], "next_cursor": None}
        )
        runs.status.assert_not_called()

    def test_rejects_bad_cursor_and_filter_change_before_aws(self):
        runs = saved_runs()
        first = list_runs(runs, arguments(limit=1, status="succeeded"))
        runs.s3.reset_mock()
        malformed = base64.urlsafe_b64encode(
            json.dumps({"version": 99}).encode()
        ).decode()
        for cursor in (
            "old-aws-token",
            "!",
            malformed,
            "a" * 2048,
            first["next_cursor"],
        ):
            with self.subTest(cursor=cursor[:30]):
                with self.assertRaises(CloudboxError) as raised:
                    list_runs(runs, arguments(cursor=cursor, status="failed"))
                self.assertEqual(raised.exception.code, "invalid_cursor")
        runs.s3.list_objects_v2.assert_not_called()

    def test_rejects_invalid_limits_before_aws(self):
        runs = saved_runs()
        for limit in (0, 101):
            with self.assertRaises(CloudboxError) as raised:
                list_runs(runs, arguments(limit=limit))
            self.assertEqual(raised.exception.code, "invalid_limit")
        runs.s3.list_objects_v2.assert_not_called()


if __name__ == "__main__":
    unittest.main()
