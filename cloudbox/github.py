"""Give a run short-lived GitHub App access to configured repositories."""

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .common import SDK_CONFIG, CloudboxError
from .github_api import GitHubAPIError, GitHubClient

MAX_PRIVATE_KEY_BYTES = 16_384
MAX_TOKEN_REPOSITORIES = 500
JWT_CLOCK_MARGIN_SECONDS = 60
JWT_LIFETIME_SECONDS = 600
TOKEN_STARTUP_MARGIN_SECONDS = 120
TOKEN_CLEANUP_MARGIN_SECONDS = 60
REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
APP_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
RUN_PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "metadata": "read",
}
GITHUB_SETTINGS = (
    "github_app_id",
    "github_installation_id",
    "github_repository_ids",
    "github_private_key_secret_arn",
)


@dataclass(frozen=True)
class GitHubAccess:
    github: dict
    token: str = field(repr=False)
    expires_at: str


def validate_private_key(value):
    try:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_PRIVATE_KEY_BYTES:
            raise ValueError
        key = load_pem_private_key(raw, password=None)
        if not isinstance(key, RSAPrivateKey):
            raise ValueError
        return raw.decode("utf-8")
    except (TypeError, ValueError, UnicodeDecodeError):
        raise CloudboxError(
            "github_private_key_invalid", "Supply an unencrypted RSA private key."
        ) from None


def app_jwt(private_key, app_id):
    now = int(time.time())
    return jwt.encode(
        {
            "iat": now - JWT_CLOCK_MARGIN_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": str(app_id),
        },
        validate_private_key(private_key),
        algorithm="RS256",
    )


def token_deadline(expires_at, timeout_seconds, *, now=None):
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise CloudboxError(
            "github_token_invalid", "GitHub returned an invalid token expiry."
        ) from None
    required = (
        timeout_seconds + TOKEN_STARTUP_MARGIN_SECONDS + TOKEN_CLEANUP_MARGIN_SECONDS
    )
    if (expiry - (now or datetime.now(UTC))).total_seconds() < required:
        raise CloudboxError(
            "github_token_expires_early",
            "GitHub access expires before the run can finish.",
        )
    return expiry


def revoke_token(token):
    GitHubClient(token).request("DELETE", "/installation/token")


def revoke_quietly(token):
    try:
        revoke_token(token)
    except GitHubAPIError:
        # Expiry still limits access when revocation cannot be confirmed.
        return False
    return True


def github_settings(deployment):
    app_id, installation_id, allowed_ids, secret_arn = (
        deployment.get(key) for key in GITHUB_SETTINGS
    )
    if (
        type(app_id) is not int
        or app_id <= 0
        or type(installation_id) is not int
        or installation_id <= 0
        or not isinstance(allowed_ids, list)
        or not 1 <= len(allowed_ids) <= MAX_TOKEN_REPOSITORIES
        or any(type(identity) is not int or identity <= 0 for identity in allowed_ids)
        or len(set(allowed_ids)) != len(allowed_ids)
        or not isinstance(secret_arn, str)
        or not secret_arn
    ):
        raise CloudboxError(
            "github_not_configured",
            "Configure the GitHub App, installation, repository IDs, and key secret.",
        )
    return app_id, installation_id, allowed_ids, secret_arn


def prepare_github_access(session, deployment):
    if all(deployment.get(key) in (None, "", []) for key in GITHUB_SETTINGS):
        return None
    app_id, installation_id, allowed_ids, secret_arn = github_settings(deployment)
    secret = session.client("secretsmanager", config=SDK_CONFIG).get_secret_value(
        SecretId=secret_arn
    )
    app = GitHubClient(app_jwt(secret.get("SecretString"), app_id))
    token = None
    try:
        app_info = app.request("GET", "/app")
        if (
            not isinstance(app_info, dict)
            or app_info.get("id") != app_id
            or not isinstance(app_info.get("slug"), str)
            or not APP_SLUG.fullmatch(app_info["slug"])
        ):
            raise CloudboxError(
                "github_identity_invalid", "GitHub returned an invalid App identity."
            )
        # Request the exact configured scope; the prompt does not change access.
        response = app.request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            {"repository_ids": allowed_ids, "permissions": RUN_PERMISSIONS},
        )
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("token"), str)
            or not response["token"]
        ):
            raise CloudboxError(
                "github_token_invalid", "GitHub returned no installation token."
            )
        token = response["token"]
        repositories = response.get("repositories")
        if (
            response.get("permissions") != RUN_PERMISSIONS
            or not isinstance(repositories, list)
            or len(repositories) != len(allowed_ids)
            or any(
                not isinstance(repo, dict)
                or type(repo.get("id")) is not int
                or not isinstance(repo.get("full_name"), str)
                or not REPOSITORY_NAME.fullmatch(repo["full_name"])
                for repo in repositories
            )
            or {repo["id"] for repo in repositories} != set(allowed_ids)
        ):
            raise CloudboxError(
                "github_scope_invalid",
                "GitHub returned unexpected repository access or permissions.",
            )
        expires_at = response.get("expires_at")
        token_deadline(expires_at, 0)
        bot_login = f"{app_info['slug']}[bot]"
        bot = GitHubClient(token).request("GET", f"/users/{quote(bot_login, safe='')}")
        if (
            not isinstance(bot, dict)
            or type(bot.get("id")) is not int
            or bot["id"] <= 0
            or bot.get("login") != bot_login
        ):
            raise CloudboxError(
                "github_identity_invalid", "GitHub returned an invalid bot identity."
            )
        github = {
            "repositories": [
                {"id": repo["id"], "full_name": repo["full_name"]}
                for repo in repositories
            ],
            "permissions": dict(RUN_PERMISSIONS),
            "git_identity": {
                "name": bot_login,
                "email": f"{bot['id']}+{bot_login}@users.noreply.github.com",
            },
        }
        return GitHubAccess(github, token, expires_at)
    except GitHubAPIError as error:
        if token:
            revoke_quietly(token)
        raise CloudboxError(
            error.code, str(error), github_status=error.status
        ) from None
    except Exception:
        if token:
            revoke_quietly(token)
        raise
