"""Unit tests for backend.integrations.zoho_sync.ZohoSync.

These tests exercise the current, as-shipped behavior of ZohoSync against
realistic-looking Zoho CRM payloads. All HTTP calls (requests.get / requests.put)
are mocked via unittest.mock -- no real network access is made.

This is a test-only suite: it does not modify backend/integrations/zoho_sync.py.
Where the implementation's actual behavior differs from an "ideal" expectation
(e.g. a defensive-parsing edge case that isn't fully defensive), the test
documents and asserts the real behavior rather than the idealized one.
"""
from unittest.mock import patch, MagicMock

import pytest
import requests

from backend.integrations.zoho_sync import ZohoSync
from backend.models.ticket import Ticket, TicketPriority, TicketStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_response(json_data, status_code=200, raise_exc=None):
    """Build a MagicMock standing in for a requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if raise_exc is not None:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    return resp


def zoho_ticket(
    id="112233",
    Subject="Cannot login to portal",
    Description="Customer unable to authenticate since this morning.",
    Contact_Name=None,
    Email="jane.doe@example.com",
    Phone="+1-555-0100",
    Account_Name=None,
    Priority="High",
    Status="In Progress",
    ticket_url="https://desk.zoho.com/support/ticket/112233",
    **extra,
):
    """Build a realistic-looking Zoho CRM ticket record.

    Pass explicit None (or omit via a dict comprehension) to simulate a
    missing field, matching how `dict.get` behaves against the real API.
    """
    if Contact_Name is None:
        Contact_Name = {"id": "554400000000123", "name": "Jane Doe"}
    if Account_Name is None:
        Account_Name = {"id": "554400000000456"}
    record = {
        "id": id,
        "Subject": Subject,
        "Description": Description,
        "Contact_Name": Contact_Name,
        "Email": Email,
        "Phone": Phone,
        "Account_Name": Account_Name,
        "Priority": Priority,
        "Status": Status,
        "ticket_url": ticket_url,
    }
    record.update(extra)
    return record


@pytest.fixture
def no_token_sync(monkeypatch):
    """A ZohoSync guaranteed to have no token configured."""
    monkeypatch.setattr("backend.integrations.zoho_sync.ZOHO_API_TOKEN", None)
    monkeypatch.delenv("ZOHO_API_TOKEN", raising=False)
    return ZohoSync(token=None)


@pytest.fixture
def sync():
    """A ZohoSync with an explicit token, independent of the environment."""
    return ZohoSync(api_base="https://www.zohoapis.com", token="test-token-123")


# ---------------------------------------------------------------------------
# No-token behavior: no HTTP calls should ever be attempted
# ---------------------------------------------------------------------------

class TestNoToken:
    def test_fetch_tickets_returns_empty_list_without_http_call(self, no_token_sync):
        with patch("backend.integrations.zoho_sync.requests.request") as mock_get:
            result = no_token_sync.fetch_tickets()

        assert result == []
        mock_get.assert_not_called()

    def test_get_ticket_by_crm_id_returns_none_without_http_call(self, no_token_sync):
        with patch("backend.integrations.zoho_sync.requests.request") as mock_get:
            result = no_token_sync.get_ticket_by_crm_id("112233")

        assert result is None
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_tickets: happy path mapping
# ---------------------------------------------------------------------------

class TestFetchTicketsMapping:
    def test_maps_multiple_well_formed_records(self, sync):
        records = [
            zoho_ticket(
                id="112233",
                Subject="Cannot login to portal",
                Description="Customer unable to authenticate since this morning.",
                Contact_Name={"id": "554400000000123", "name": "Jane Doe"},
                Priority="High",
                Status="In Progress",
            ),
            zoho_ticket(
                id="112234",
                Subject="Billing discrepancy",
                Description="Customer was double charged on invoice #4821.",
                Contact_Name={"id": "554400000000789", "name": "John Smith"},
                Email="john.smith@example.com",
                Priority="Critical",
                Status="Escalated",
            ),
            zoho_ticket(
                id="112235",
                Subject="Feature request: dark mode",
                Description="Customer would like a dark theme option.",
                Contact_Name={"id": "554400000000999", "name": "Alex Lee"},
                Email="alex.lee@example.com",
                Priority="Low",
                Status="Resolved",
            ),
        ]
        response = make_response({"data": records})

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response) as mock_get:
            tickets = sync.fetch_tickets()

        assert mock_get.called
        assert len(tickets) == 3
        assert all(isinstance(t, Ticket) for t in tickets)

        first = tickets[0]
        assert first.id == "zoho-112233"
        assert first.subject == "Cannot login to portal"
        assert first.description == "Customer unable to authenticate since this morning."
        assert first.crm_ticket_id == "112233"
        assert first.crm_system == "zoho"
        assert first.crm_link == "https://desk.zoho.com/support/ticket/112233"
        assert first.customer.id == "554400000000123"
        assert first.customer.name == "Jane Doe"
        assert first.customer.email == "jane.doe@example.com"
        assert first.customer.phone == "+1-555-0100"
        assert first.customer.account_id == "554400000000456"
        assert first.priority == TicketPriority.HIGH
        assert first.status == TicketStatus.IN_PROGRESS

        second = tickets[1]
        assert second.id == "zoho-112234"
        assert second.crm_ticket_id == "112234"
        assert second.customer.name == "John Smith"
        assert second.priority == TicketPriority.CRITICAL
        assert second.status == TicketStatus.ESCALATED

        third = tickets[2]
        assert third.id == "zoho-112235"
        assert third.priority == TicketPriority.LOW
        assert third.status == TicketStatus.RESOLVED

    def test_fetch_tickets_passes_filter_dict_as_params(self, sync):
        response = make_response({"data": []})
        filter_dict = {"status": "open"}

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response) as mock_get:
            result = sync.fetch_tickets(filter_dict=filter_dict)

        assert result == []
        _, kwargs = mock_get.call_args
        # Pagination params (page/per_page) are layered on top of filter_dict,
        # not a replacement for it -- see test_zoho_sync_pagination.py for
        # dedicated coverage of that behavior.
        assert kwargs["params"]["status"] == filter_dict["status"]


# ---------------------------------------------------------------------------
# Priority mapping
# ---------------------------------------------------------------------------

class TestPriorityMapping:
    @pytest.mark.parametrize(
        "raw_priority, expected",
        [
            ("High", TicketPriority.HIGH),
            ("Critical", TicketPriority.CRITICAL),
            ("Low", TicketPriority.LOW),
            ("Medium", TicketPriority.MEDIUM),
            ("Unrecognized", TicketPriority.MEDIUM),
            (None, TicketPriority.MEDIUM),
        ],
    )
    def test_priority_values_map_correctly(self, sync, raw_priority, expected):
        record = zoho_ticket(Priority=raw_priority)
        response = make_response({"data": [record]})

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response):
            tickets = sync.fetch_tickets()

        assert tickets[0].priority == expected

    def test_priority_missing_key_defaults_to_medium(self, sync):
        record = zoho_ticket()
        del record["Priority"]
        response = make_response({"data": [record]})

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response):
            tickets = sync.fetch_tickets()

        assert tickets[0].priority == TicketPriority.MEDIUM


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

class TestStatusMapping:
    @pytest.mark.parametrize(
        "raw_status, expected",
        [
            ("In Progress", TicketStatus.IN_PROGRESS),
            ("Awaiting Customer", TicketStatus.AWAITING_CUSTOMER),
            ("Resolved", TicketStatus.RESOLVED),
            ("Escalated", TicketStatus.ESCALATED),
            ("Closed", TicketStatus.CLOSED),
            ("New", TicketStatus.NEW),
            ("Some Unrecognized Status", TicketStatus.NEW),
            (None, TicketStatus.NEW),
        ],
    )
    def test_status_values_map_correctly(self, sync, raw_status, expected):
        record = zoho_ticket(Status=raw_status)
        response = make_response({"data": [record]})

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response):
            tickets = sync.fetch_tickets()

        assert tickets[0].status == expected

    def test_status_missing_key_defaults_to_new(self, sync):
        record = zoho_ticket()
        del record["Status"]
        response = make_response({"data": [record]})

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response):
            tickets = sync.fetch_tickets()

        assert tickets[0].status == TicketStatus.NEW


# ---------------------------------------------------------------------------
# Defensive parsing of incomplete records
# ---------------------------------------------------------------------------

class TestDefensiveParsing:
    def test_missing_subject_description_and_contact_fall_back_to_defaults(self, sync):
        # Only an id is present -- everything else is absent from the payload,
        # the way a sparse/partial Zoho record might look.
        record = {"id": "999999"}
        response = make_response({"data": [record]})

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response):
            tickets = sync.fetch_tickets()

        assert len(tickets) == 1
        ticket = tickets[0]
        assert ticket.id == "zoho-999999"
        assert ticket.crm_ticket_id == "999999"
        assert ticket.subject == "No subject"
        assert ticket.description == ""
        assert ticket.customer.id == "unknown"
        assert ticket.customer.name == "Unknown"
        assert ticket.customer.email == ""
        assert ticket.customer.phone is None
        assert ticket.customer.account_id is None
        assert ticket.customer.crm_link is None
        assert ticket.priority == TicketPriority.MEDIUM
        assert ticket.status == TicketStatus.NEW

    def test_completely_missing_id_still_produces_a_ticket(self, sync):
        record = {"Subject": "Orphan ticket with no id"}
        response = make_response({"data": [record]})

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response):
            tickets = sync.fetch_tickets()

        assert len(tickets) == 1
        assert tickets[0].id == "zoho-"
        assert tickets[0].crm_ticket_id == ""


# ---------------------------------------------------------------------------
# fetch_tickets: network/error handling
# ---------------------------------------------------------------------------

class TestFetchTicketsErrors:
    def test_request_exception_returns_empty_list(self, sync):
        with patch(
            "backend.integrations.zoho_sync.requests.request",
            side_effect=requests.RequestException("connection failed"),
        ):
            result = sync.fetch_tickets()

        assert result == []

    def test_http_error_status_returns_empty_list(self, sync):
        response = make_response(
            {}, status_code=500, raise_exc=requests.HTTPError("500 Server Error")
        )
        with patch("backend.integrations.zoho_sync.requests.request", return_value=response):
            result = sync.fetch_tickets()

        assert result == []


# ---------------------------------------------------------------------------
# get_ticket_by_crm_id
# ---------------------------------------------------------------------------

class TestGetTicketByCrmId:
    def test_happy_path_returns_populated_ticket(self, sync):
        record = zoho_ticket(
            id="778899",
            Subject="Password reset not working",
            Description="Reset email never arrives.",
            Contact_Name={"id": "554400000001111", "name": "Priya Patel"},
            Email="priya.patel@example.com",
        )
        response = make_response({"data": [record]})

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response) as mock_get:
            ticket = sync.get_ticket_by_crm_id("778899")

        assert mock_get.called
        assert isinstance(ticket, Ticket)
        assert ticket.id == "zoho-778899"
        assert ticket.crm_ticket_id == "778899"
        assert ticket.crm_system == "zoho"
        assert ticket.subject == "Password reset not working"
        assert ticket.description == "Reset email never arrives."
        assert ticket.customer.id == "554400000001111"
        assert ticket.customer.name == "Priya Patel"
        assert ticket.customer.email == "priya.patel@example.com"
        # get_ticket_by_crm_id does not map Priority/Status from the payload;
        # the Ticket dataclass defaults apply as currently implemented.
        assert ticket.status == TicketStatus.NEW
        assert ticket.priority == TicketPriority.MEDIUM

    def test_missing_data_key_returns_none(self, sync):
        response = make_response({})  # no "data" key at all

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response):
            result = sync.get_ticket_by_crm_id("778899")

        assert result is None

    def test_empty_data_list_raises_index_error(self, sync):
        # NOTE: this documents the current, as-shipped behavior. The code does
        # `resp.json().get("data", [None])[0]`, and the [None] default only
        # applies when the "data" key is absent entirely. When "data" is
        # present but an empty list, indexing [0] raises IndexError, which is
        # not a requests.RequestException and therefore is NOT caught -- it
        # propagates out of get_ticket_by_crm_id rather than returning None.
        response = make_response({"data": []})

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response):
            with pytest.raises(IndexError):
                sync.get_ticket_by_crm_id("778899")

    def test_request_exception_returns_none(self, sync):
        with patch(
            "backend.integrations.zoho_sync.requests.request",
            side_effect=requests.RequestException("timeout"),
        ):
            result = sync.get_ticket_by_crm_id("778899")

        assert result is None


# ---------------------------------------------------------------------------
# sync_ticket_to_zoho
# ---------------------------------------------------------------------------

class TestSyncTicketToZoho:
    def _make_ticket(self, crm_ticket_id="112233"):
        from backend.models.ticket import CustomerInfo

        return Ticket(
            id="zoho-112233",
            subject="Cannot login to portal",
            description="Customer unable to authenticate since this morning.",
            customer=CustomerInfo(id="1", name="Jane Doe", email="jane.doe@example.com"),
            status=TicketStatus.RESOLVED,
            priority=TicketPriority.HIGH,
            crm_ticket_id=crm_ticket_id,
            crm_system="zoho",
        )

    def test_successful_sync_returns_true_and_sends_expected_payload(self, sync):
        ticket = self._make_ticket(crm_ticket_id="112233")
        response = make_response({"data": [{"code": "SUCCESS"}]}, status_code=200)

        with patch("backend.integrations.zoho_sync.requests.request", return_value=response) as mock_put:
            result = sync.sync_ticket_to_zoho(ticket)

        assert result is True
        assert mock_put.called
        args, kwargs = mock_put.call_args
        url = args[1] if len(args) > 1 else (args[0] if args else kwargs.get("url"))
        assert "112233" in url
        payload = kwargs["json"]
        item = payload["data"][0]
        assert item["Status"] == ticket.status.value
        assert item["Description"] == ticket.description

    def test_missing_crm_ticket_id_returns_false_without_http_call(self, sync):
        ticket = self._make_ticket(crm_ticket_id=None)

        with patch("backend.integrations.zoho_sync.requests.request") as mock_put:
            result = sync.sync_ticket_to_zoho(ticket)

        assert result is False
        mock_put.assert_not_called()

    def test_no_token_returns_false_without_http_call(self, no_token_sync):
        ticket = self._make_ticket(crm_ticket_id="112233")

        with patch("backend.integrations.zoho_sync.requests.request") as mock_put:
            result = no_token_sync.sync_ticket_to_zoho(ticket)

        assert result is False
        mock_put.assert_not_called()

    def test_request_exception_returns_false(self, sync):
        ticket = self._make_ticket(crm_ticket_id="112233")

        with patch(
            "backend.integrations.zoho_sync.requests.request",
            side_effect=requests.RequestException("connection reset"),
        ):
            result = sync.sync_ticket_to_zoho(ticket)

        assert result is False
