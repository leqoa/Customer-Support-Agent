"""Unit tests for ZohoSync's OAuth2 refresh-token authentication.

All Zoho HTTP calls are mocked via unittest.mock.patch -- these tests never
make real network requests and never require real Zoho credentials.
"""
import time
from unittest.mock import patch, MagicMock

import pytest
import requests

from backend.integrations.zoho_sync import ZohoSync, ZohoAuthError


def _oauth_client(**overrides):
    """Build a ZohoSync configured for the OAuth2 flow, bypassing env vars."""
    kwargs = dict(
        client_id="test-client-id",
        client_secret="test-client-secret",
        refresh_token="test-refresh-token",
        accounts_base="https://accounts.zoho.com",
    )
    kwargs.update(overrides)
    return ZohoSync(**kwargs)


def _mock_token_response(access_token="access-token-1", expires_in=3600, status_ok=True):
    resp = MagicMock()
    resp.json.return_value = {"access_token": access_token, "expires_in": expires_in}
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.HTTPError("400 Bad Request")
    return resp


class TestTokenFetchAndCache:
    @patch("backend.integrations.zoho_sync.requests.post")
    def test_token_is_fetched_and_cached(self, mock_post):
        mock_post.return_value = _mock_token_response(access_token="fresh-token")
        client = _oauth_client()

        token = client._get_valid_token()

        assert token == "fresh-token"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.args[0] == "https://accounts.zoho.com/oauth/v2/token"
        assert call_args.kwargs["data"] == {
            "grant_type": "refresh_token",
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "refresh_token": "test-refresh-token",
        }
        # Expiry cached with a safety margin subtracted.
        assert client._token_expiry <= time.time() + 3600 - client.TOKEN_SAFETY_MARGIN_SECONDS + 1

    @patch("backend.integrations.zoho_sync.requests.post")
    def test_headers_use_cached_token(self, mock_post):
        mock_post.return_value = _mock_token_response(access_token="fresh-token")
        client = _oauth_client()

        headers = client._headers()

        assert headers["Authorization"] == "Zoho-oauthtoken fresh-token"
        mock_post.assert_called_once()


class TestTokenReuse:
    @patch("backend.integrations.zoho_sync.requests.post")
    def test_cached_token_is_reused_when_not_expired(self, mock_post):
        mock_post.return_value = _mock_token_response(access_token="fresh-token")
        client = _oauth_client()

        first = client._get_valid_token()
        second = client._get_valid_token()
        third = client._get_valid_token()

        assert first == second == third == "fresh-token"
        mock_post.assert_called_once()  # only one network call across all 3 gets


class TestTokenRefreshOnExpiry:
    @patch("backend.integrations.zoho_sync.requests.post")
    def test_token_is_refreshed_when_expired(self, mock_post):
        mock_post.return_value = _mock_token_response(access_token="token-1")
        client = _oauth_client()

        first = client._get_valid_token()
        assert first == "token-1"
        assert mock_post.call_count == 1

        # Force expiry into the past.
        client._token_expiry = time.time() - 1

        mock_post.return_value = _mock_token_response(access_token="token-2")
        second = client._get_valid_token()

        assert second == "token-2"
        assert mock_post.call_count == 2


class TestRefreshFailures:
    @patch("backend.integrations.zoho_sync.requests.post")
    def test_network_failure_raises_zoho_auth_error(self, mock_post):
        mock_post.side_effect = requests.ConnectionError("boom")
        client = _oauth_client()

        with pytest.raises(ZohoAuthError):
            client._get_valid_token()

    @patch("backend.integrations.zoho_sync.requests.post")
    def test_http_error_status_raises_zoho_auth_error(self, mock_post):
        mock_post.return_value = _mock_token_response(status_ok=False)
        client = _oauth_client()

        with pytest.raises(ZohoAuthError):
            client._get_valid_token()

    @patch("backend.integrations.zoho_sync.requests.post")
    def test_missing_access_token_in_response_raises_zoho_auth_error(self, mock_post):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"error": "invalid_code"}
        mock_post.return_value = resp
        client = _oauth_client()

        with pytest.raises(ZohoAuthError):
            client._get_valid_token()

    def test_no_credentials_raises_zoho_auth_error(self):
        client = ZohoSync(client_id=None, client_secret=None, refresh_token=None, token=None)

        with pytest.raises(ZohoAuthError):
            client._get_valid_token()


class TestStaticTokenBackwardCompatibility:
    def test_falls_back_to_static_token_without_oauth_config(self, caplog):
        with caplog.at_level("WARNING"):
            client = ZohoSync(token="legacy-static-token")

        assert client._get_valid_token() == "legacy-static-token"
        assert any("deprecated" in message.lower() for message in caplog.messages)

    @patch("backend.integrations.zoho_sync.requests.post")
    def test_oauth_config_takes_priority_over_static_token(self, mock_post):
        mock_post.return_value = _mock_token_response(access_token="oauth-token")
        client = _oauth_client(token="legacy-static-token")

        assert client._get_valid_token() == "oauth-token"
        mock_post.assert_called_once()
