"""Check GitHub credential scope without remote requests."""

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.client import IncompleteRead
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from cloudbox.common import CloudboxError
from cloudbox.github import (
    JWT_CLOCK_MARGIN_SECONDS, JWT_LIFETIME_SECONDS, RUN_PERMISSIONS,
    TOKEN_CLEANUP_MARGIN_SECONDS, TOKEN_STARTUP_MARGIN_SECONDS,
    app_jwt, prepare_github_access, token_deadline, validate_private_key,
)
from cloudbox.github_api import GitHubAPIError, GitHubClient, NoRedirects

APP_ID = 123
INSTALLATION_ID = 456
REPOSITORIES = [{"id": 42, "full_name": "example/first"}, {"id": 84, "full_name": "example/second"}]
BOT_LOGIN = "example-agent[bot]"
BOT_ID = 789
GIT_IDENTITY = {"name": BOT_LOGIN, "email": f"{BOT_ID}+{BOT_LOGIN}@users.noreply.github.com"}
TOKEN = "test-token-do-not-save"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:github-key"


class GitHubAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.private_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()

    def setUp(self):
        self.deployment = {"github_app_id": APP_ID, "github_installation_id": INSTALLATION_ID,
                           "github_repository_ids": [repo["id"] for repo in REPOSITORIES],
                           "github_private_key_secret_arn": SECRET_ARN}
        self.session = Mock()
        self.session.client.return_value.get_secret_value.return_value = {"SecretString": self.pem}
        self.response = {"token": TOKEN, "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                         "permissions": dict(RUN_PERMISSIONS), "repositories": REPOSITORIES}

    def github_response(self, method, path, data=None):
        if path == "/app":
            return {"id": APP_ID, "slug": "example-agent"}
        if path.startswith("/users/"):
            return {"id": BOT_ID, "login": BOT_LOGIN}
        return self.response

    def test_no_settings_do_not_access_aws(self):
        self.assertIsNone(prepare_github_access(self.session, {}))
        self.session.client.assert_not_called()

    def test_partial_settings_do_not_create_access(self):
        for config in ({"github_app_id": APP_ID}, {"github_app_id": 0}):
            with self.subTest(config=config), self.assertRaisesRegex(CloudboxError, "Configure the GitHub"):
                prepare_github_access(self.session, config)
        self.session.client.assert_not_called()

    def test_jwt_verifies_and_uses_bounded_lifetime(self):
        now = int(datetime.now(timezone.utc).timestamp())
        with patch("cloudbox.github.time.time", return_value=now):
            encoded = app_jwt(self.pem, APP_ID)
        claims = jwt.decode(encoded, self.private_key.public_key(), algorithms=["RS256"])
        self.assertEqual(claims, {"iat": now - JWT_CLOCK_MARGIN_SECONDS,
                                 "exp": now + JWT_LIFETIME_SECONDS, "iss": str(APP_ID)})

    def test_rejects_other_key_types(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        with self.assertRaises(CloudboxError) as raised:
            validate_private_key(pem)
        self.assertEqual(raised.exception.code, "github_private_key_invalid")

    @patch("cloudbox.github.revoke_quietly")
    @patch("cloudbox.github.GitHubClient")
    def test_exact_repository_scope_is_available_to_generic_runs(self, client, revoke):
        client.return_value.request.side_effect = self.github_response
        access = prepare_github_access(self.session, self.deployment)
        client.return_value.request.assert_any_call("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens",
            {"repository_ids": [42, 84], "permissions": RUN_PERMISSIONS})
        self.assertEqual(access.github, {"repositories": REPOSITORIES, "permissions": RUN_PERMISSIONS,
                                        "git_identity": GIT_IDENTITY})
        self.assertEqual(access.token, TOKEN)
        self.assertNotIn(TOKEN, repr(access))
        self.assertNotIn(self.pem, repr(access))
        revoke.assert_not_called()

    @patch("cloudbox.github.revoke_quietly")
    @patch("cloudbox.github.GitHubClient")
    def test_unexpected_scope_revokes_token(self, client, revoke):
        cases = [
            {"permissions": {**RUN_PERMISSIONS, "workflows": "write"}},
            {"repositories": REPOSITORIES[:1]},
            {"repositories": [*REPOSITORIES, {"id": 126, "full_name": "example/extra"}]},
            {"repositories": [{"id": True, "full_name": "example/first"}, REPOSITORIES[1]]},
        ]
        for change in cases:
            with self.subTest(change=change):
                response = {**self.response, **change}
                client.return_value.request.side_effect = lambda method, path, data=None: (
                    {"id": APP_ID, "slug": "example-agent"} if path == "/app" else response)
                with self.assertRaises(CloudboxError) as raised:
                    prepare_github_access(self.session, self.deployment)
                self.assertEqual(raised.exception.code, "github_scope_invalid")
                revoke.assert_called_with(TOKEN)

    @patch("cloudbox.github.revoke_quietly")
    @patch("cloudbox.github.GitHubClient")
    def test_missing_expiry_revokes_token(self, client, revoke):
        self.response["expires_at"] = None
        client.return_value.request.side_effect = self.github_response
        with self.assertRaises(CloudboxError) as raised:
            prepare_github_access(self.session, self.deployment)
        self.assertEqual(raised.exception.code, "github_token_invalid")
        revoke.assert_called_once_with(TOKEN)

    @patch("cloudbox.github.revoke_quietly")
    @patch("cloudbox.github.GitHubClient")
    def test_wrong_bot_identity_revokes_token(self, client, revoke):
        client.return_value.request.side_effect = lambda method, path, data=None: (
            {"id": BOT_ID, "login": "other[bot]"} if path.startswith("/users/")
            else self.github_response(method, path, data))
        with self.assertRaises(CloudboxError) as raised:
            prepare_github_access(self.session, self.deployment)
        self.assertEqual(raised.exception.code, "github_identity_invalid")
        revoke.assert_called_once_with(TOKEN)

    def test_expiry_includes_startup_and_cleanup(self):
        now = datetime.now(timezone.utc)
        timeout = 3300
        required = timeout + TOKEN_STARTUP_MARGIN_SECONDS + TOKEN_CLEANUP_MARGIN_SECONDS
        token_deadline((now + timedelta(seconds=required)).isoformat(), timeout, now=now)
        with self.assertRaises(CloudboxError) as raised:
            token_deadline((now + timedelta(seconds=required - 1)).isoformat(), timeout, now=now)
        self.assertEqual(raised.exception.code, "github_token_expires_early")


class GitHubAPITests(unittest.TestCase):
    def test_rejects_external_paths_before_request(self):
        opener = Mock()
        with self.assertRaises(GitHubAPIError):
            GitHubClient(TOKEN, opener=opener).request("GET", "//example.com/collect")
        opener.open.assert_not_called()
        self.assertIsNone(NoRedirects().redirect_request(None, None, HTTPStatus.FOUND, "", {}, "https://example.com"))

    def test_write_transport_failure_is_uncertain_and_not_retried(self):
        opener = Mock()
        opener.open.side_effect = URLError(TOKEN)
        with self.assertRaises(GitHubAPIError) as raised:
            GitHubClient(TOKEN, opener=opener).request("POST", "/repos/example/first/issues", {"body": "text"})
        self.assertTrue(raised.exception.uncertain)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_http_errors_exclude_remote_text(self):
        opener = Mock()
        opener.open.side_effect = HTTPError("https://api.github.com/app", HTTPStatus.FORBIDDEN,
                                            TOKEN, {}, io.BytesIO(TOKEN.encode()))
        with self.assertRaises(GitHubAPIError) as raised:
            GitHubClient(TOKEN, opener=opener).request("POST", "/app")
        self.assertEqual(raised.exception.status, HTTPStatus.FORBIDDEN)
        self.assertFalse(raised.exception.uncertain)
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_truncated_response_is_safe_and_uncertain(self):
        opener = Mock()
        response = MagicMock()
        response.status = HTTPStatus.CREATED
        response.read.side_effect = IncompleteRead(TOKEN.encode(), 100)
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with self.assertRaises(GitHubAPIError) as raised:
            GitHubClient(TOKEN, opener=opener).request("POST", "/app")
        self.assertTrue(raised.exception.uncertain)
        self.assertNotIn(TOKEN, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
