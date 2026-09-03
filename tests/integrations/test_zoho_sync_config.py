"""Tests for ZohoSync's configurable field mapping, typed error handling,
and operational metrics counters.

All network calls are mocked - no real Zoho credentials or requests are used.
"""
import json
from unittest.mock import patch, MagicMock

import pytest
import requests

from backend.integrations.zoho_sync import (
    ZohoSync,
    ZohoAPIError,
    ZohoConfigError,
    ZohoSyncError,
    DEFAULT_FIELD_MAP,
    _load_field_map,
)
from backend.models.ticket import Ticket, CustomerInfo, TicketStatus


@pytest.fixture(autouse=True)
def _no_real_sleeping():
    """ZohoSync now retries retryable (429/5xx) responses with real backoff
    delays (see the retry/backoff hardening). None of the failure scenarios
    in this file are testing retry timing itself (that's covered in
    test_zoho_sync_retry.py) -- they just want to observe the eventual
    failure -- so skip the actual sleeping everywhere in this file.
    """
    with patch("backend.integrations.zoho_sync.time.sleep"):
        yield


# ---------------------------------------------------------------------------
# Field map config loading
# ---------------------------------------------------------------------------

def test_field_map_loads_from_config_file(tmp_path):
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(
        """
integrations:
  zoho:
    enabled: true
    field_map:
      subject: "Ticket_Subject"
      description: "Ticket_Description"
      priority: "Priority"
      status: "Ticket_Status"
      contact_name: "Contact_Name"
      email: "Contact_Email"
      phone: "Contact_Phone"
      account_name: "Account_Name"
      ticket_url: "Ticket_URL"
"""
    )

    field_map = _load_field_map(str(config_file))

    assert field_map["subject"] == "Ticket_Subject"
    assert field_map["description"] == "Ticket_Description"
    assert field_map["status"] == "Ticket_Status"
    assert field_map["email"] == "Contact_Email"
    assert field_map["phone"] == "Contact_Phone"
    assert field_map["ticket_url"] == "Ticket_URL"


def test_field_map_falls_back_to_defaults_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.yaml"

    field_map = _load_field_map(str(missing_path))

    assert field_map == DEFAULT_FIELD_MAP


def test_field_map_falls_back_to_defaults_when_section_missing(tmp_path):
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(
        """
integrations:
  zoho:
    enabled: true
"""
    )

    field_map = _load_field_map(str(config_file))

    assert field_map == DEFAULT_FIELD_MAP


def test_field_map_partial_override_merges_with_defaults(tmp_path):
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(
        """
integrations:
  zoho:
    field_map:
      subject: "Custom_Subject"
"""
    )

    field_map = _load_field_map(str(config_file))

    assert field_map["subject"] == "Custom_Subject"
    # Everything else should still be the default
    assert field_map["description"] == DEFAULT_FIELD_MAP["description"]
    assert field_map["status"] == DEFAULT_FIELD_MAP["status"]


def test_field_map_falls_back_when_malformed_yaml(tmp_path):
    config_file = tmp_path / "settings.yaml"
    config_file.write_text("::: not valid yaml :::\n  - [unbalanced")

    field_map = _load_field_map(str(config_file))

    assert field_map == DEFAULT_FIELD_MAP


def test_zoho_sync_uses_loaded_field_map(tmp_path):
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(
        """
integrations:
  zoho:
    field_map:
      subject: "Custom_Subject_Field"
"""
    )

    client = ZohoSync(token="fake-token", config_path=str(config_file))

    assert client.field_map["subject"] == "Custom_Subject_Field"
    assert client.field_map["description"] == DEFAULT_FIELD_MAP["description"]


# ---------------------------------------------------------------------------
# Typed error handling
# ---------------------------------------------------------------------------

def _mock_failing_response(status_code=500, body="Internal Server Error"):
    response = MagicMock()
    response.status_code = status_code
    response.text = body
    error = requests.exceptions.HTTPError(f"{status_code} Server Error")
    error.response = response
    response.raise_for_status.side_effect = error
    return response


def test_fetch_tickets_default_returns_empty_list_on_api_failure(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_failing_response()):
        result = client.fetch_tickets()

    assert result == []


def test_fetch_tickets_raise_on_error_raises_zoho_api_error(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_failing_response(503, "boom")):
        with pytest.raises(ZohoAPIError) as exc_info:
            client.fetch_tickets(raise_on_error=True)

    assert exc_info.value.status_code == 503
    assert exc_info.value.response_body == "boom"
    assert isinstance(exc_info.value, ZohoSyncError)


def test_fetch_tickets_no_token_default_returns_empty_list(tmp_path):
    client = ZohoSync(token=None, config_path=str(tmp_path / "missing.yaml"))
    client.token = None  # ensure no env var leaked in

    result = client.fetch_tickets()

    assert result == []


def test_fetch_tickets_no_token_raise_on_error_raises_config_error(tmp_path):
    client = ZohoSync(token=None, config_path=str(tmp_path / "missing.yaml"))
    client.token = None

    with pytest.raises(ZohoConfigError):
        client.fetch_tickets(raise_on_error=True)


def test_get_ticket_by_crm_id_default_returns_none_on_failure(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_failing_response()):
        result = client.get_ticket_by_crm_id("123")

    assert result is None


def test_get_ticket_by_crm_id_raise_on_error_raises_api_error(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_failing_response(404, "not found")):
        with pytest.raises(ZohoAPIError):
            client.get_ticket_by_crm_id("123", raise_on_error=True)


def test_sync_ticket_to_zoho_default_returns_false_on_failure(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))
    ticket = Ticket(
        id="local-1",
        subject="Test",
        description="Test description",
        customer=CustomerInfo(id="c1", name="Jane", email="jane@example.com"),
        status=TicketStatus.NEW,
        crm_ticket_id="zcrm-1",
    )

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_failing_response()):
        result = client.sync_ticket_to_zoho(ticket)

    assert result is False


def test_sync_ticket_to_zoho_raise_on_error_raises_api_error(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))
    ticket = Ticket(
        id="local-1",
        subject="Test",
        description="Test description",
        customer=CustomerInfo(id="c1", name="Jane", email="jane@example.com"),
        status=TicketStatus.NEW,
        crm_ticket_id="zcrm-1",
    )

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_failing_response(500, "err")):
        with pytest.raises(ZohoAPIError):
            client.sync_ticket_to_zoho(ticket, raise_on_error=True)


def test_sync_ticket_to_zoho_missing_crm_id_raise_on_error_raises_config_error(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))
    ticket = Ticket(
        id="local-1",
        subject="Test",
        description="Test description",
        customer=CustomerInfo(id="c1", name="Jane", email="jane@example.com"),
        crm_ticket_id=None,
    )

    with pytest.raises(ZohoConfigError):
        client.sync_ticket_to_zoho(ticket, raise_on_error=True)


# ---------------------------------------------------------------------------
# Metrics counters
# ---------------------------------------------------------------------------

def _mock_success_response(payload):
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.side_effect = None
    response.json.return_value = payload
    return response


def test_metrics_start_at_zero(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))

    assert client.metrics == {"requests_made": 0, "requests_failed": 0, "tickets_fetched": 0}


def test_metrics_increment_on_successful_fetch(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))
    payload = {
        "data": [
            {"id": "1", "Subject": "Issue A", "Description": "desc", "Email": "a@example.com"},
            {"id": "2", "Subject": "Issue B", "Description": "desc", "Email": "b@example.com"},
        ]
    }

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_success_response(payload)):
        tickets = client.fetch_tickets()

    assert len(tickets) == 2
    assert client.metrics["requests_made"] == 1
    assert client.metrics["requests_failed"] == 0
    assert client.metrics["tickets_fetched"] == 2


def test_metrics_increment_across_multiple_calls_including_failures(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))
    payload = {"data": [{"id": "1", "Subject": "Issue A"}]}

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_success_response(payload)):
        client.fetch_tickets()

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_failing_response()):
        client.fetch_tickets()

    assert client.metrics["requests_made"] == 2
    assert client.metrics["requests_failed"] == 1
    assert client.metrics["tickets_fetched"] == 1


def test_metrics_track_failed_sync_calls(tmp_path):
    client = ZohoSync(token="fake-token", config_path=str(tmp_path / "missing.yaml"))
    ticket = Ticket(
        id="local-1",
        subject="Test",
        description="Test description",
        customer=CustomerInfo(id="c1", name="Jane", email="jane@example.com"),
        crm_ticket_id="zcrm-1",
    )

    with patch("backend.integrations.zoho_sync.requests.request", return_value=_mock_failing_response()):
        client.sync_ticket_to_zoho(ticket)

    assert client.metrics["requests_made"] == 1
    assert client.metrics["requests_failed"] == 1
