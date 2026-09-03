"""Select saved runs by UUID prefix or submission time."""

import base64
import binascii
import json
import re
from datetime import UTC, datetime
from itertools import pairwise
from os.path import commonprefix

from cloudbox.common import TASK_STATUSES, CloudboxError

MIN_RUN_PREFIX_LENGTH = 8
RUN_ID_LENGTH = 36
MAX_LIST_PAGE_SIZE = 100
RUNS_PREFIX = "runs/"
RUN_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
RUN_ID_HYPHEN_POSITIONS = frozenset((8, 13, 18, 23))
HEX_DIGITS = frozenset("0123456789abcdef")
CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 1024
CURSOR_FIELDS = frozenset(("version", "submitted_at", "run_id", "status"))
EARLIEST_TIME = datetime.min.replace(tzinfo=UTC)


def validate_run_reference(value):
    # Check UUID positions before any AWS request, including partial UUIDs.
    if (
        not isinstance(value, str)
        or not MIN_RUN_PREFIX_LENGTH <= len(value) <= RUN_ID_LENGTH
        or any(
            character != "-"
            if index in RUN_ID_HYPHEN_POSITIONS
            else character not in HEX_DIGITS
            for index, character in enumerate(value)
        )
    ):
        raise CloudboxError(
            "invalid_run_id",
            "Use the full run UUID or a unique prefix of at least "
            f"{MIN_RUN_PREFIX_LENGTH} characters.",
        )
    return value


def saved_run_ids(s3, bucket, prefix=RUNS_PREFIX):
    # Read every storage page because S3 orders UUID keys, not submission times.
    request = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/"}
    identities = set()
    while True:
        page = s3.list_objects_v2(**request)
        for item in page.get("CommonPrefixes", []):
            key = item.get("Prefix", "")
            if (
                not isinstance(key, str)
                or not key.startswith(prefix)
                or not key.endswith("/")
            ):
                continue
            identity = key.removeprefix(RUNS_PREFIX).removesuffix("/")
            if RUN_ID_PATTERN.fullmatch(identity):
                identities.add(identity)
        continuation = page.get("NextContinuationToken")
        if not continuation:
            return identities
        request["ContinuationToken"] = continuation


def resolve_run_id(s3, bucket, value):
    value = validate_run_reference(value)
    if len(value) == RUN_ID_LENGTH:
        return value
    matches = saved_run_ids(s3, bucket, f"{RUNS_PREFIX}{value}")
    if not matches:
        raise CloudboxError("run_not_found", "No run matches this prefix.")
    if len(matches) > 1:
        raise CloudboxError(
            "ambiguous_run_id", "More than one run matches. Use a longer prefix."
        )
    return matches.pop()


def short_run_ids(identities):
    # Compare sorted neighbours so each prefix selects only one saved run.
    ordered = sorted({value for value in identities if isinstance(value, str)})
    lengths = dict.fromkeys(ordered, MIN_RUN_PREFIX_LENGTH)
    for left, right in pairwise(ordered):
        needed = len(commonprefix((left, right))) + 1
        lengths[left] = max(lengths[left], needed)
        lengths[right] = max(lengths[right], needed)
    return {identity: identity[: lengths[identity]] for identity in ordered}


def submitted_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def ordering_key(submitted, identity):
    return (submitted is not None, submitted or EARLIEST_TIME, identity)


def read_cursor(value, status):
    # Bind the cursor to its filter so a changed query cannot skip runs silently.
    try:
        if not isinstance(value, str) or not value or len(value) > MAX_CURSOR_LENGTH:
            raise ValueError
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        cursor = json.loads(decoded)
        if (
            not isinstance(cursor, dict)
            or set(cursor) != CURSOR_FIELDS
            or type(cursor["version"]) is not int
            or cursor["version"] != CURSOR_VERSION
            or cursor["status"] != status
            or not isinstance(cursor["run_id"], str)
            or not RUN_ID_PATTERN.fullmatch(cursor["run_id"])
        ):
            raise ValueError
        submitted = submitted_time(cursor["submitted_at"])
        if cursor["submitted_at"] is not None and (
            submitted is None or submitted.isoformat() != cursor["submitted_at"]
        ):
            raise ValueError
        return ordering_key(submitted, cursor["run_id"])
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as error:
        raise CloudboxError(
            "invalid_cursor", "Use a cursor from list with the same status filter."
        ) from error


def write_cursor(key, status):
    has_date, submitted, identity = key
    payload = {
        "version": CURSOR_VERSION,
        "submitted_at": submitted.isoformat() if has_date else None,
        "run_id": identity,
        "status": status,
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def list_runs(runs, arguments, *, human=False):
    if (
        type(arguments.limit) is not int
        or not 1 <= arguments.limit <= MAX_LIST_PAGE_SIZE
    ):
        raise CloudboxError(
            "invalid_limit", f"Use a limit from 1 to {MAX_LIST_PAGE_SIZE}."
        )
    if arguments.status is not None and arguments.status not in TASK_STATUSES:
        raise CloudboxError("invalid_status", "Use a supported task status.")
    anchor = (
        read_cursor(arguments.cursor, arguments.status)
        if arguments.cursor is not None
        else None
    )
    ordered = []
    identities = saved_run_ids(runs.s3, runs.bucket)
    for identity in identities:
        spec = runs.record(identity, "spec.json")
        submitted = (
            submitted_time(spec.get("submitted_at")) if isinstance(spec, dict) else None
        )
        key = ordering_key(submitted, identity)
        if anchor is None or key < anchor:
            ordered.append(key)
    ordered.sort(reverse=True)

    # Limit live VM reads to selected rows when no status filter is needed.
    summaries = []
    selected_key = None
    has_more = False
    for key in ordered:
        if arguments.status is None and len(summaries) == arguments.limit:
            has_more = True
            break
        summary = runs.status(key[-1])
        if arguments.status is not None and summary["task_status"] != arguments.status:
            continue
        if len(summaries) == arguments.limit:
            has_more = True
            break
        summaries.append(summary)
        selected_key = key
    result = {
        "ok": True,
        "runs": summaries,
        "next_cursor": (
            write_cursor(selected_key, arguments.status) if has_more else None
        ),
    }
    if human:
        # Use the full history for labels; another page can share a prefix.
        labels = short_run_ids(identities)
        result["run_labels"] = {
            row["run_id"]: labels[row["run_id"]] for row in summaries
        }
    return result
