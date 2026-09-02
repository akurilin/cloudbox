"""Bounded GitHub requests shared by the CLI and worker."""

import json
from http import HTTPStatus
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_ORIGIN = "https://api.github.com"
API_VERSION = "2026-03-10"
API_ACCEPT = "application/vnd.github+json"
API_TIMEOUT_SECONDS = 10
MAX_API_RESPONSE_BYTES = 2_097_152
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class GitHubAPIError(Exception):
    """A safe error without request bodies, credentials, or remote text."""

    def __init__(self, code, message, *, status=None, uncertain=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.uncertain = uncertain


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        # A redirect must never send the credential to a second host.
        return None


class GitHubClient:
    def __init__(self, token, *, timeout=API_TIMEOUT_SECONDS, opener=None):
        self.token = token
        self.timeout = timeout
        self.opener = opener or build_opener(NoRedirects())

    def request(self, method, path, data=None):
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or any(ord(character) < 32 for character in path)
        ):
            raise GitHubAPIError("github_path_invalid", "Use a GitHub API path.")
        method = method.upper()
        is_write = method in WRITE_METHODS
        body = (
            None if data is None else json.dumps(data, allow_nan=False).encode("utf-8")
        )
        request = Request(
            API_ORIGIN + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": API_ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "cloudbox-agent",
                "Content-Type": "application/json",
            },
        )
        try:
            # Do not retry writes: a missing response can follow a successful write.
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status == HTTPStatus.NO_CONTENT:
                    return None
                raw = response.read(MAX_API_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            uncertain = is_write and (
                status >= HTTPStatus.INTERNAL_SERVER_ERROR
                or status == HTTPStatus.REQUEST_TIMEOUT
            )
            raise GitHubAPIError(
                "github_http_error",
                "GitHub rejected the request.",
                status=status,
                uncertain=uncertain,
            ) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise GitHubAPIError(
                "github_unavailable",
                "The GitHub response is unavailable.",
                uncertain=is_write,
            ) from None
        if len(raw) > MAX_API_RESPONSE_BYTES:
            raise GitHubAPIError(
                "github_response_too_large",
                "The GitHub response exceeds the size limit.",
                uncertain=is_write,
            )
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise GitHubAPIError(
                "github_response_invalid",
                "The GitHub response is not valid JSON.",
                uncertain=is_write,
            ) from None
