"""Expose GitHub tools and the access granted to this generic agent run."""

import json
import os
import re
import time
from datetime import datetime

from github_api import GitHubClient

ARTIFACT_SCHEMA_VERSION = 1
GITHUB_SCHEMA_VERSION = 2
API_TIMEOUT_SECONDS = 10
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GITHUB_PERMISSIONS = {"contents", "issues", "pull_requests", "metadata"}
ACCESS_LEVELS = {"read", "write"}
ASCII_FIRST_PRINTABLE = 32
ASCII_DELETE = 127
GIT_CONFIG = (
    ("credential.helper", ""),
    ("credential.https://github.com.helper", "!gh auth git-credential"),
    ("credential.https://github.com.useHttpPath", "true"),
)


def validate_github_spec(spec):
    # Version 2 requires explicit capabilities; older workers reject these jobs.
    access = spec.get("github")
    if access is None:
        if spec.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ValueError("invalid_spec")
        return
    if spec.get("schema_version") != GITHUB_SCHEMA_VERSION or not isinstance(access, dict):
        raise ValueError("invalid_github_access")
    repositories = access.get("repositories")
    permissions = access.get("permissions")
    identity = access.get("git_identity")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("invalid_github_repositories")
    for repository in repositories:
        if (not isinstance(repository, dict) or type(repository.get("id")) is not int
                or repository["id"] <= 0 or not isinstance(repository.get("full_name"), str)
                or not REPOSITORY_PATTERN.fullmatch(repository["full_name"])):
            raise ValueError("invalid_github_repository")
    if (not isinstance(permissions, dict) or not permissions
            or not permissions.keys() <= GITHUB_PERMISSIONS
            or any(value not in ACCESS_LEVELS for value in permissions.values())):
        raise ValueError("invalid_github_permissions")
    if not isinstance(identity, dict):
        raise ValueError("invalid_git_identity")
    for field in ("name", "email"):
        value = identity.get(field)
        if (not isinstance(value, str) or not value.strip()
                or any(ord(character) < ASCII_FIRST_PRINTABLE or ord(character) == ASCII_DELETE
                       for character in value)):
            raise ValueError("invalid_git_identity")


def github_environment(spec, payload, workspace, deadline):
    # Runtime credentials stay in child environments, not prompts or Git files.
    validate_github_spec(spec)
    if not spec.get("github"):
        if payload.get("github_token"):
            raise ValueError("unexpected_github_token")
        return {}
    token = payload.get("github_token")
    expires_at = payload.get("github_token_expires_at")
    if not isinstance(token, str) or not token or not isinstance(expires_at, str):
        raise ValueError("missing_github_token")
    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry.timestamp() <= time.time() + max(0, deadline - time.monotonic()):
        raise ValueError("github_token_expiring")
    environment = {"GH_TOKEN": token, "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1",
                   "GH_CONFIG_DIR": str(workspace / ".gh"), "GIT_TERMINAL_PROMPT": "0",
                   "GIT_CONFIG_COUNT": str(len(GIT_CONFIG))}
    identity = spec["github"]["git_identity"]
    environment.update({"GIT_AUTHOR_NAME": identity["name"], "GIT_AUTHOR_EMAIL": identity["email"],
                        "GIT_COMMITTER_NAME": identity["name"], "GIT_COMMITTER_EMAIL": identity["email"]})
    for index, (key, value) in enumerate(GIT_CONFIG):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def github_contract(spec):
    if not spec.get("github"):
        return ""
    access = json.dumps(spec["github"], separators=(",", ":"))
    return (
        " Git, GitHub CLI (gh), and uv are installed. GitHub authentication and the Git credential "
        "helper and Git commit identity are configured for this run. "
        "Use these tools as required by the user's task. "
        f"Granted GitHub access: {access}."
    )


def child_environment():
    # Inherited tracing and auth settings must not expose or replace run credentials.
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_", "GH_", "GITHUB_"))}


def revoke_token(token, deadline, client_factory=GitHubClient):
    # Attempt revocation after success or failure, without logging credential errors.
    if not token:
        return None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        client_factory(token, timeout=min(API_TIMEOUT_SECONDS, remaining)).request("DELETE", "/installation/token")
        return True
    except Exception:
        return False
