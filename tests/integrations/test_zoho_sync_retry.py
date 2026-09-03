"""Unit tests for the retry/backoff behavior added to ZohoSync HTTP calls.

All tests mock `requests.request` and `time.sleep` so no real network calls
or real waiting ever happens.
"""
from unittest.mock import patch, MagicMock

import pytest
import requests

from backend.integrations.zoho_sync import ZohoSync
from backend.models.ticket import Ticket, CustomerInfo, TicketStatus


def _make_response(status_code, json_data=None, headers=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_data or {}

    def raise_for_status():
        if status_code >= 400:
            raise requests.exceptions.HTTPError(f"{status_code} error", response=resp)

    resp.raise_for_status.side_effect = raise_for_status
    return resp


def _make_ticket():
    customer = CustomerInfo(id="c1", name="Jane Doe", email="jane@example.com")
    return Ticket(
        id="local-1",
        subject="Test subject",
        description="Test description",
        customer=customer,
        status=TicketStatus.IN_PROGRESS,
        crm_ticket_id="zoho-crm-1",
    )


@pytest.fixture
def sync_client():
    return ZohoSync(api_base="https://example.invalid", token="test-token", max_retry_attempts=4, retry_base_delay=0.5, retry_jitter=0.25)


@patch("backend.integrations.zoho_sync.random.uniform", return_value=0.0)
@patch("backend.integrations.zoho_sync.time.sleep")
@patch("backend.integrations.zoho_sync.requests.request")
def test_retries_on_429_then_succeeds(mock_request, mock_sleep, mock_uniform, sync_client):
    """Two 429s followed by a 200 should succeed on the third attempt."""
    resp_429_a = _make_response(429)
    resp_429_b = _make_response(429)
    resp_200 = _make_response(200, json_data={"data": [{"id": "1"}]})
    mock_request.side_effect = [resp_429_a, resp_429_b, resp_200]

    result = sync_client._request_with_retry("GET", "https://example.invalid/crm/v2/Tickets")

    assert result is resp_200
    assert mock_request.call_count == 3

    # Two sleeps happened (before attempt 2 and before attempt 3), with
    # strictly increasing exponential backoff delays.
    assert mock_sleep.call_count == 2
    delays = [call.args[0] for call in mock_sleep.call_args_list]
    assert delays[0] < delays[1]
    assert delays[0] == pytest.approx(0.5)  # base_delay * 2**0 + 0 jitter
    assert delays[1] == pytest.approx(1.0)  # base_delay * 2**1 + 0 jitter


@patch("backend.integrations.zoho_sync.random.uniform", return_value=0.0)
@patch("backend.integrations.zoho_sync.time.sleep")
@patch("backend.integrations.zoho_sync.requests.request")
def test_gives_up_after_max_attempts_on_persistent_500(mock_request, mock_sleep, mock_uniform, sync_client):
    """A persistent 500 should exhaust retries and preserve documented failure values."""
    resp_500 = _make_response(500)
    mock_request.return_value = resp_500

    ticket = _make_ticket()
    result = sync_client.sync_ticket_to_zoho(ticket)

    assert result is False
    assert mock_request.call_count == sync_client.max_retry_attempts
    # One sleep between each attempt except after the final one.
    assert mock_sleep.call_count == sync_client.max_retry_attempts - 1


@patch("backend.integrations.zoho_sync.random.uniform", return_value=0.0)
@patch("backend.integrations.zoho_sync.time.sleep")
@patch("backend.integrations.zoho_sync.requests.request")
def test_gives_up_after_max_attempts_fetch_tickets_returns_empty_list(mock_request, mock_sleep, mock_uniform, sync_client):
    resp_500 = _make_response(500)
    mock_request.return_value = resp_500

    result = sync_client.fetch_tickets()

    assert result == []
    assert mock_request.call_count == sync_client.max_retry_attempts


@patch("backend.integrations.zoho_sync.random.uniform", return_value=0.0)
@patch("backend.integrations.zoho_sync.time.sleep")
@patch("backend.integrations.zoho_sync.requests.request")
def test_gives_up_after_max_attempts_get_ticket_by_crm_id_returns_none(mock_request, mock_sleep, mock_uniform, sync_client):
    resp_500 = _make_response(500)
    mock_request.return_value = resp_500

    result = sync_client.get_ticket_by_crm_id("crm-123")

    assert result is None
    assert mock_request.call_count == sync_client.max_retry_attempts


@patch("backend.integrations.zoho_sync.time.sleep")
@patch("backend.integrations.zoho_sync.requests.request")
def test_retry_after_header_is_honored(mock_request, mock_sleep, sync_client):
    """A Retry-After header on a 429 response should be used verbatim instead
    of the computed exponential backoff delay."""
    resp_429 = _make_response(429, headers={"Retry-After": "7"})
    resp_200 = _make_response(200, json_data={"data": [{"id": "1"}]})
    mock_request.side_effect = [resp_429, resp_200]

    result = sync_client._request_with_retry("GET", "https://example.invalid/crm/v2/Tickets")

    assert result is resp_200
    mock_sleep.assert_called_once_with(7.0)


@patch("backend.integrations.zoho_sync.time.sleep")
@patch("backend.integrations.zoho_sync.requests.request")
def test_connection_error_is_retried_and_eventually_succeeds(mock_request, mock_sleep, sync_client):
    resp_200 = _make_response(200, json_data={"data": [{"id": "1"}]})
    mock_request.side_effect = [
        requests.exceptions.ConnectionError("boom"),
        requests.exceptions.Timeout("timed out"),
        resp_200,
    ]

    result = sync_client._request_with_retry("GET", "https://example.invalid/crm/v2/Tickets")

    assert result is resp_200
    assert mock_request.call_count == 3
    assert mock_sleep.call_count == 2


def test_no_retry_on_non_retryable_status(sync_client):
    """A plain 404 should not be retried at all."""
    resp_404 = _make_response(404)
    with patch("backend.integrations.zoho_sync.requests.request", return_value=resp_404) as mock_request, \
         patch("backend.integrations.zoho_sync.time.sleep") as mock_sleep:
        result = sync_client._request_with_retry("GET", "https://example.invalid/crm/v2/Tickets/missing")

    assert result is resp_404
    assert mock_request.call_count == 1
    mock_sleep.assert_not_called()
