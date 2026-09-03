"""Render command results for terminal readers."""

import json
import re
import shlex
import shutil
import unicodedata
from datetime import UTC, datetime

from .run_selection import short_run_ids, submitted_time

ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*?(?:\x07|\x1b\\))")
UNKNOWN = "unknown"
MISSING = "-"
AWS_TERMINATED = "TERMINATED"
AWS_RUNNING = "RUNNING"
DEFAULT_COLUMNS = 100
MIN_COLUMNS = 20
MIN_SUMMARY_COLUMNS = 12
COLUMN_GAP = "  "
BYTES_PER_UNIT = 1024
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
MILLISECONDS_PER_SECOND = 1000
LOCAL_TIME_FORMAT = "%Y-%m-%d %H:%M"
ELAPSED_MARK = "+"
ELAPSED_NOTE = "+ = elapsed; run is active."
AGENT_EVENTS = {
    "model_message",
    "tool_execution_start",
    "tool_execution_end",
    "agent_start",
    "agent_settled",
    "agent_launch",
    "auto_retry_start",
    "auto_retry_end",
}
LOG_FIELDS = (
    "script",
    "status",
    "reason",
    "exit_code",
    "error_type",
    "error_message",
    "attempt",
    "success",
    "confirmed",
    "written",
    "stop_reason",
)


def terminal_text(value):
    # Remove terminal commands while keeping response lines and tabs.
    value = ANSI_ESCAPE.sub("", value)
    return "".join(
        character
        for character in value
        if character in "\n\t" or unicodedata.category(character) not in {"Cc", "Cf"}
    )


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _line(value, default=MISSING):
    if value is None:
        return default
    return " ".join(terminal_text(str(value)).split()) or default


def _columns():
    return max(MIN_COLUMNS, shutil.get_terminal_size((DEFAULT_COLUMNS, 24)).columns)


def _width(text):
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def _clip(text, width):
    if _width(text) <= width:
        return text
    output = ""
    for character in text:
        if _width(output + character) >= width:
            break
        output += character
    return output + "…"


def _wrap(text, width):
    # Wrap on words, including file names that exceed the terminal width.
    lines, line = [], ""
    for word in text.split():
        if line and _width(line + " " + word) > width:
            lines.append(line)
            line = ""
        while _width(word) > width:
            chunk = ""
            for character in word:
                if _width(chunk + character) > width:
                    break
                chunk += character
            lines.append(chunk)
            word = word[len(chunk) :]
        line += (" " if line else "") + word
    if line:
        lines.append(line)
    return lines


def _size(value):
    if type(value) is not int or value < 0:
        return MISSING
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < BYTES_PER_UNIT or unit == "GiB":
            return f"{size:g} {unit}"
        size /= BYTES_PER_UNIT


def _artifacts(record):
    value = record.get("artifacts")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _summary(record):
    report = _mapping(_mapping(record.get("result")).get("report"))
    return _line(report.get("summary"))


def _environment(record):
    if not record.get("environment"):
        return []
    return [f"Environment: {_line(record['environment'])}"]


def _list_timing(record, now):
    # Prefer worker start time; submission alone does not measure execution.
    result = _mapping(record.get("result"))
    launch = _mapping(record.get("launch"))
    started = submitted_time(result.get("started_at")) or submitted_time(
        launch.get("started_at")
    )
    when = started or submitted_time(record.get("submitted_at"))
    try:
        local_time = when.astimezone().strftime(LOCAL_TIME_FORMAT) if when else MISSING
    except (ValueError, OverflowError, OSError):
        local_time = MISSING
    finished = submitted_time(result.get("finished_at"))
    active = record.get("result") is None and record.get("compute_state") == AWS_RUNNING
    end = now if active else finished
    duration = (
        _duration(started.isoformat(), end.isoformat()) if started and end else None
    )
    elapsed = active and duration is not None
    if elapsed:
        duration += ELAPSED_MARK
    return local_time, duration or MISSING, elapsed


def _listing(record):
    width = _columns()
    lines = _environment(record)
    runs = record.get("runs") or []
    labels = short_run_ids(
        run["run_id"] for run in runs if isinstance(run.get("run_id"), str)
    )
    labels.update(_mapping(record.get("run_labels")))
    rows, has_elapsed = [], False
    now = datetime.now(UTC)
    for run in runs:
        when, duration, elapsed = _list_timing(run, now)
        has_elapsed = has_elapsed or elapsed
        identity = run.get("run_id")
        rows.append(
            [
                _line(labels.get(identity, identity)),
                when,
                duration,
                _line(run.get("task_status"), UNKNOWN),
                _line(run.get("compute_state"), UNKNOWN),
                str(len(_artifacts(_mapping(run.get("result"))))),
                _summary(run),
            ]
        )
    if rows:
        headers = ["RUN", "WHEN (LOCAL)", "DURATION", "TASK", "VM", "FILES", "SUMMARY"]
        widths = [
            max(_width(row[index]) for row in [headers, *rows])
            for index in range(len(headers) - 1)
        ]
        summary_width = width - sum(widths) - len(COLUMN_GAP) * len(widths)
        lines.append("")
        if summary_width >= MIN_SUMMARY_COLUMNS:
            # Shorten summaries only; keep time and independent task/VM states.
            for row in [headers, *rows]:
                cells = [
                    cell + " " * (size - _width(cell))
                    for cell, size in zip(row, widths, strict=False)
                ]
                lines.append(COLUMN_GAP.join([*cells, _clip(row[-1], summary_width)]))
        else:
            for index, row in enumerate(rows):
                if index:
                    lines.append("")
                lines.extend(_wrap(row[0], width))
                for label, value in zip(
                    ("When (local)", "Duration", "Task", "VM", "Files", "Summary"),
                    row[1:],
                    strict=True,
                ):
                    lines.extend(_wrap(f"{label}: {value}", width))
    else:
        lines.append("No runs.")
    if has_elapsed:
        lines.extend(["", *_wrap(ELAPSED_NOTE, width)])
    if record.get("next_cursor"):
        command = record.get("next_command")
        if not command:
            command = shlex.join(
                [
                    "cloudbox",
                    "--env",
                    _line(record.get("environment")),
                    "list",
                    "--cursor",
                    str(record["next_cursor"]),
                ]
            )
        lines.extend(["", f"Next: {terminal_text(str(command))}"])
    return "\n".join(lines)


def _duration(started, finished):
    try:
        seconds = int(
            (
                datetime.fromisoformat(finished) - datetime.fromisoformat(started)
            ).total_seconds()
        )
    except (ValueError, TypeError, OverflowError):
        return None
    if seconds < 0:
        return None
    hours, seconds = divmod(seconds, SECONDS_PER_HOUR)
    minutes, seconds = divmod(seconds, SECONDS_PER_MINUTE)
    parts = ([f"{hours}h"] if hours else []) + ([f"{minutes}m"] if minutes else [])
    return " ".join([*parts, f"{seconds}s"])


def _status(record):
    result = _mapping(record.get("result"))
    started = result.get("started_at") or _mapping(record.get("launch")).get(
        "started_at"
    )
    finished = result.get("finished_at")
    fields = [
        ("Run", record.get("run_id")),
        ("Task", record.get("task_status") or UNKNOWN),
        ("VM", record.get("compute_state") or UNKNOWN),
        ("VM error", record.get("compute_error")),
        ("Submitted", record.get("submitted_at")),
        ("Started", started),
        ("Finished", finished),
        ("Duration", _duration(started, finished)),
        ("Reason", result.get("reason")),
        ("Exit code", result.get("exit_code")),
    ]
    lines = _environment(record)
    lines.extend(
        f"{label}: {_line(value)}" for label, value in fields if value is not None
    )
    if record.get("exists") is False:
        lines.append("No saved run records.")
    summary = _summary(record)
    if summary != MISSING:
        lines.extend(_wrap(f"Summary: {summary}", _columns()))
    artifacts = _artifacts(result)
    lines.append(f"Files: {len(artifacts)}")
    lines.extend(
        f"  {_line(item.get('name'))} ({_size(item.get('bytes'))})"
        for item in artifacts
    )
    return "\n".join(lines)


def _confirmation(command, record):
    lines = _environment(record)
    identity = _line(record.get("run_id"))
    state = _line(record.get("compute_state"), UNKNOWN)
    if command == "submit":
        lines.extend([f"Submitted run: {identity}", f"VM: {state}"])
        if record.get("launch_record_saved") is False:
            lines.append(
                "Warning: Launch record was not saved. "
                "Check this run before submitting again."
            )
    else:
        if state == AWS_TERMINATED:
            action = "VM stopped"
        elif record.get("cancel_requested"):
            action = "Stop requested"
        else:
            action = "Stop not requested"
        lines.extend(
            [
                f"{action}: {identity}",
                f"Task: {_line(record.get('task_status'), UNKNOWN)}",
                f"VM: {state}",
            ]
        )
    return "\n".join(lines)


def _download(record):
    lines = [*_environment(record), f"Run: {_line(record.get('run_id'))}"]
    lines.append(f"Directory: {_line(record.get('directory'))}")
    files = record.get("files") or []
    lines.append(f"Saved files: {len(files)}")
    lines.extend(
        f"  {_line(item.get('path'))} ({_size(item.get('bytes'))})" for item in files
    )
    if record.get("incomplete"):
        lines.append(
            "Warning: Files may be incomplete. "
            f"Task: {_line(record.get('task_status'), UNKNOWN)}."
        )
    return "\n".join(lines)


def _links(record):
    lines = [*_environment(record), f"Run: {_line(record.get('run_id'))}"]
    artifacts = _artifacts(record)
    if not artifacts:
        lines.append("No output files.")
    for item in artifacts:
        lines.extend(
            [
                "",
                f"{_line(item.get('name'))} ({_size(item.get('bytes'))})",
                f"Expires: {_line(item.get('expires_at'))}",
                _line(item.get("url")),
            ]
        )
    return "\n".join(lines)


def render_result(command, record):
    if command == "list":
        return _listing(record)
    if command == "status":
        return _status(record)
    if command in {"submit", "cancel"}:
        return _confirmation(command, record)
    if command == "download":
        return _download(record)
    if command == "links":
        return _links(record)
    raise ValueError(f"No human output format for {command}.")


def _log_timestamp(event, record):
    value = event.get("timestamp")
    if type(value) in {int, float}:
        try:
            return datetime.fromtimestamp(
                value / MILLISECONDS_PER_SECOND, UTC
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError, OSError):
            pass
    return _line(record.get("timestamp"), "")


def render_log(event):
    # Show useful event fields without repeating CloudWatch or worker envelopes.
    message = event.get("message", "")
    try:
        record = _mapping(json.loads(message))
    except (ValueError, TypeError):
        record = {}
    stamp = _log_timestamp(event, record)
    prefix = f"{stamp} " if stamp else ""
    event_name = record.get("event")
    if not isinstance(event_name, str) or not event_name.strip():
        return prefix + terminal_text(str(message))
    name = _line(event_name, "message")
    source = record.get("source")
    if not isinstance(source, str) or source not in {"agent", "supervisor"}:
        source = "agent" if name in AGENT_EVENTS else "supervisor"
    label = {
        "tool_execution_start": "tool start",
        "tool_execution_end": "tool end",
    }.get(name, name.replace("_", " "))
    heading = f"{prefix}[{source}] {label}"
    details = []
    if name in {"tool_execution_start", "tool_execution_end"}:
        heading += f": {_line(record.get('tool_name'), UNKNOWN)}"
        if record.get("outcome"):
            heading += f" ({_line(record['outcome'])})"
        seconds = record.get("duration_seconds")
        if type(seconds) in {int, float}:
            heading += f" {seconds:g}s"
        arguments = _mapping(record.get("arguments"))
        details.extend(
            f"{key}: {_line(arguments[key])}"
            for key in ("command", "path")
            if key in arguments
        )
        content = _mapping(record.get("result")).get("content")
        if isinstance(content, list):
            details.extend(
                item["text"]
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            )
    message = record.get("text")
    visible_text = terminal_text(message) if isinstance(message, str) else ""
    if visible_text.strip():
        message_lines = visible_text.splitlines()
        heading += ": " + message_lines[0]
        details.extend(message_lines[1:])
    for key in LOG_FIELDS:
        value = record.get(key)
        if value is not None:
            if isinstance(value, bool):
                value = "yes" if value else "no"
            else:
                value = _line(value)
            details.append(f"{key.replace('_', ' ')}: {value}")
    lines = [heading]
    for detail in details:
        lines.extend(f"  {line}" for line in terminal_text(detail).splitlines())
    return "\n".join(lines)
