"""Publish agent-selected output files without giving Pi the S3 credentials."""

import hashlib
import json
import mimetypes
import os
import re
import socketserver
import stat
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

OUTPUT_DIRECTORY = "output"
PUBLISH_SOCKET_ENV = "CLOUDBOX_PUBLISH_SOCKET"
MAX_ARTIFACTS = 32
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_FILENAME_CHARACTERS = 128
MAX_PATH_BYTES = 4096
MAX_REQUEST_BYTES = 8 * 1024
URL_LIFETIME_SECONDS = 3600
CREDENTIAL_EXPIRY_MARGIN_SECONDS = 30
PUBLISH_BUDGET_SECONDS = 8
SOCKET_TIMEOUT_SECONDS = 20
SERVER_POLL_SECONDS = 0.1
SHUTDOWN_TIMEOUT_SECONDS = 3
DEFAULT_CONTENT_TYPE = "application/octet-stream"
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class PublishError(ValueError):
    """A safe error message that the agent can use to correct its request."""


def credential_expiry(value):
    # Signed links cannot outlive the credentials used to create them.
    if not isinstance(value, str):
        raise ValueError("invalid_data_credentials_expiry")
    expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry.timestamp() <= time.time():
        raise ValueError("invalid_data_credentials_expiry")
    return expiry.timestamp()


def safe_filename(name):
    # Keep object names safe for downloads and terminal output.
    return (
        SAFE_FILENAME_PATTERN.sub("_", name).lstrip("._-")[:MAX_FILENAME_CHARACTERS]
        or "file"
    )


class ArtifactPublisher:
    def __init__(self, s3, bucket, run_id, workspace, deadline, expires_at):
        self.s3 = s3
        self.bucket = bucket
        self.prefix = f"runs/{run_id}/artifacts"
        self.workspace = Path(workspace).absolute()
        self.output = self.workspace / OUTPUT_DIRECTORY
        self.deadline = deadline
        self.credentials_expire = credential_expiry(expires_at)
        self.artifacts = []
        self.total_bytes = 0
        self.lock = threading.Lock()
        self.output.mkdir(mode=0o700)
        # Hold the original directory so a later symlink cannot redirect a read.
        self.output_fd = os.open(self.output, DIRECTORY_FLAGS)

    def close(self):
        if self.output_fd is not None:
            os.close(self.output_fd)
            self.output_fd = None

    def snapshot(self):
        # Completed records are appended once; a stalled upload must not block cleanup.
        return [artifact.copy() for artifact in tuple(self.artifacts)]

    def check_deadline(self):
        if self.deadline - time.monotonic() < PUBLISH_BUDGET_SECONDS:
            raise PublishError("Not enough run time remains to publish a file.")

    def path_parts(self, path):
        if not isinstance(path, str) or not path or "\0" in path:
            raise PublishError("Pass a file path inside output/.")
        try:
            if len(path.encode("utf-8")) > MAX_PATH_BYTES:
                raise PublishError("The file path is too long.")
        except UnicodeError as error:
            raise PublishError(
                "The file path must contain valid UTF-8 text."
            ) from error
        if ".." in path.split("/"):
            raise PublishError("Parent traversal is not allowed in file paths.")
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            parts = candidate.relative_to(self.output).parts
        except ValueError as error:
            raise PublishError("Publish only files inside output/.") from error
        if not parts:
            raise PublishError("Pass a regular file inside output/.")
        return parts

    def read_file(self, parts):
        # Open each component without following links; reject devices and pipes.
        directory_fd = os.dup(self.output_fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(parts[-1], FILE_FLAGS, dir_fd=directory_fd)
            with os.fdopen(file_fd, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise PublishError("Publish a regular file with no hard links.")
                if before.st_size > MAX_ARTIFACT_BYTES:
                    raise PublishError("The file exceeds the 32 MiB limit.")
                if self.total_bytes + before.st_size > MAX_TOTAL_ARTIFACT_BYTES:
                    raise PublishError("Published files exceed the 128 MiB run limit.")
                content = source.read(MAX_ARTIFACT_BYTES + 1)
                after = os.fstat(source.fileno())
                if (
                    len(content) != before.st_size
                    or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                ):
                    raise PublishError(
                        "The file changed during publication. Close it and retry."
                    )
                return content
        except OSError as error:
            raise PublishError(
                "Cannot read the file. Check its path and remove links."
            ) from error
        finally:
            os.close(directory_fd)

    def publish(self, path):
        with self.lock:
            self.check_deadline()
            if len(self.artifacts) >= MAX_ARTIFACTS:
                raise PublishError("The run has reached the 32-file publication limit.")
            parts = self.path_parts(path)
            content = self.read_file(parts)
            self.check_deadline()
            signed_at = int(time.time())
            lifetime = min(
                URL_LIFETIME_SECONDS,
                int(
                    self.credentials_expire
                    - signed_at
                    - CREDENTIAL_EXPIRY_MARGIN_SECONDS
                ),
            )
            if lifetime <= 0:
                raise PublishError("The download credentials are about to expire.")
            name = safe_filename(parts[-1])
            key = f"{self.prefix}/{uuid.uuid4()}/{name}"
            content_type = mimetypes.guess_type(name)[0] or DEFAULT_CONTENT_TYPE
            disposition = (
                f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"
            )
            try:
                # Sign first so an invalid signer cannot leave an unreported upload.
                url = self.s3.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": self.bucket,
                        "Key": key,
                        "ResponseContentDisposition": disposition,
                    },
                    ExpiresIn=lifetime,
                )
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=content,
                    ContentType=content_type,
                    ContentDisposition=disposition,
                    IfNoneMatch="*",
                )
            except Exception as error:
                # Provider exception text can contain signed URLs or credentials.
                raise PublishError(
                    "File upload failed. Retry, or report the failure in finish."
                ) from error
            artifact = {
                "name": name,
                "key": key,
                "bytes": len(content),
                "content_type": content_type,
                "sha256": hashlib.sha256(content).hexdigest(),
                "url": url,
                "expires_at": datetime.fromtimestamp(
                    signed_at + lifetime, UTC
                ).isoformat(),
            }
            self.artifacts.append(artifact)
            self.total_bytes += len(content)
            return artifact.copy()


class PublishHandler(socketserver.StreamRequestHandler):
    def read_request(self):
        # Enforce total read time, including callers that send one byte at a time.
        deadline = min(
            self.server.publisher.deadline, time.monotonic() + SOCKET_TIMEOUT_SECONDS
        )
        raw = bytearray()
        while b"\n" not in raw and len(raw) <= MAX_REQUEST_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PublishError("Publication request timed out.")
            self.connection.settimeout(remaining)
            chunk = self.connection.recv(MAX_REQUEST_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            raise PublishError("Invalid publication request.")
        return json.loads(raw)

    def handle(self):
        try:
            request = self.read_request()
            if not isinstance(request, dict) or request.keys() != {"path"}:
                raise PublishError("Pass only path to publish_file.")
            reply = {"artifact": self.server.publisher.publish(request["path"])}
        except PublishError as error:
            reply = {"error": str(error)}
        except Exception:
            reply = {"error": "Publication failed. Check the file and retry."}
        try:
            self.connection.settimeout(SOCKET_TIMEOUT_SECONDS)
            self.wfile.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
        except OSError:
            # Keep a completed upload in the manifest when the caller disconnects.
            pass


class PublishService:
    def __init__(self, publisher):
        self.publisher = publisher
        self.directory = None
        self.server = None
        self.thread = None
        self.shutdown_thread = None
        self.socket_path = None

    def start(self):
        # A short private path fits the Unix socket limit on Linux and macOS.
        self.directory = tempfile.TemporaryDirectory(
            prefix="cloudbox-publish-", dir="/tmp"
        )
        self.socket_path = str(Path(self.directory.name) / "service.sock")
        self.server = socketserver.UnixStreamServer(self.socket_path, PublishHandler)
        self.server.publisher = self.publisher
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": SERVER_POLL_SECONDS},
            daemon=True,
        )
        self.thread.start()
        return {PUBLISH_SOCKET_ENV: self.socket_path}

    def close(self):
        if self.thread is not None:
            if self.shutdown_thread is None:
                self.shutdown_thread = threading.Thread(
                    target=self.server.shutdown, daemon=True
                )
                self.shutdown_thread.start()
            self.thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            if self.thread.is_alive():
                # The VM stop closes resources still used by a stalled upload.
                raise TimeoutError("file_publication_shutdown_timeout")
        if self.server is not None:
            self.server.server_close()
        if self.directory is not None:
            self.directory.cleanup()
        self.publisher.close()
