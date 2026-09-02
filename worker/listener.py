"""Accept AWS lifecycle hooks. No task or credentials exist at snapshot time."""

import json
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from supervisor import supervise

HOOK_PORT = 8080
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"
MAX_HOOK_BODY_BYTES = 64 * 1024
SCHEMA_VERSIONS = {1, 2}
GITHUB_SCHEMA_VERSION = 2
RUN_LOCK = threading.Lock()
active_run = None


class HookHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # Hook bodies contain credentials; omit HTTP request logging.
        pass

    def respond(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        global active_run
        if self.path == f"{HOOK_PREFIX}/ready":
            self.respond(HTTPStatus.OK)
            return
        if self.path != f"{HOOK_PREFIX}/run":
            self.respond(HTTPStatus.NOT_FOUND)
            return

        try:
            body_size = int(self.headers.get("Content-Length", "0"))
            if not 0 < body_size <= MAX_HOOK_BODY_BYTES:
                raise ValueError("Invalid hook size")
            body = json.loads(self.rfile.read(body_size))
            payload = json.loads(body["runHookPayload"])
            microvm_id = body["microvmId"]
            # AWS IDs are opaque. A guessed prefix rejected valid cloud runs.
            if not isinstance(microvm_id, str) or not microvm_id:
                raise ValueError("Invalid VM ID")
            if payload["schema_version"] not in SCHEMA_VERSIONS:
                raise ValueError("Unsupported hook schema")
            if payload["schema_version"] == GITHUB_SCHEMA_VERSION:
                for field in ("github_token", "github_token_expires_at"):
                    if not isinstance(payload[field], str) or not payload[field]:
                        raise ValueError("Missing GitHub credentials")
            elif payload.get("github_token"):
                raise ValueError("Unexpected GitHub credentials")
            run_id = payload["run_id"]
            if str(uuid.UUID(run_id)) != run_id:
                raise ValueError("Invalid run ID")
            for field in (
                "bucket_name",
                "openrouter_secret_arn",
                "log_group_name",
                "aws_region",
            ):
                if not isinstance(payload[field], str) or not payload[field]:
                    raise ValueError("Missing runtime setting")
            for field in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
                if not isinstance(payload["data_credentials"][field], str):
                    raise ValueError("Missing data credentials")
        except (KeyError, TypeError, ValueError):
            self.respond(HTTPStatus.BAD_REQUEST)
            return

        # AWS can retry a hook. Start at most one supervisor in this VM.
        identity = (microvm_id, run_id)
        with RUN_LOCK:
            if active_run is not None and active_run != identity:
                self.respond(HTTPStatus.CONFLICT)
                return
            if active_run is None:
                active_run = identity
                threading.Thread(
                    target=supervise, args=(microvm_id, payload), daemon=True
                ).start()
        self.respond(HTTPStatus.OK)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), HookHandler).serve_forever()
