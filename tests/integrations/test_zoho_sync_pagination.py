"""Unit tests for ZohoSync.fetch_tickets() pagination.

All Zoho API calls are mocked -- no real network access or credentials
are used or required.
"""
from unittest.mock import patch, MagicMock

from backend.integrations.zoho_sync import ZohoSync, MAX_PAGES


def _make_response(items, more_records):
    """Build a fake requests.Response-like object for a single Zoho page."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "data": items,
        "info": {"more_records": more_records, "count": len(items)},
    }
    return resp


def _zoho_item(ticket_id, subject="Some subject"):
    return {
        "id": str(ticket_id),
        "Subject": subject,
        "Description": f"Description for {ticket_id}",
        "Contact_Name": {"id": f"contact-{ticket_id}", "name": f"Customer {ticket_id}"},
        "Email": f"customer{ticket_id}@example.com",
        "Priority": "High",
        "Status": "New",
        "ticket_url": f"https://crm.zoho.com/tickets/{ticket_id}",
    }


class TestFetchTicketsPagination:
    def _client(self):
        return ZohoSync(api_base="https://fake.zohoapis.com", token="fake-token")

    @patch("backend.integrations.zoho_sync.requests.get")
    def test_paginates_across_multiple_pages_and_concatenates_results(self, mock_get):
        page1 = _make_response([_zoho_item(1), _zoho_item(2)], more_records=True)
        page2 = _make_response([_zoho_item(3), _zoho_item(4)], more_records=True)
        page3 = _make_response([_zoho_item(5)], more_records=False)
        mock_get.side_effect = [page1, page2, page3]

        client = self._client()
        tickets = client.fetch_tickets()

        # All records from all pages were merged, in order, and correctly
        # converted into Ticket objects (reusing the per-item conversion).
        assert [t.crm_ticket_id for t in tickets] == ["1", "2", "3", "4", "5"]
        assert all(t.id == f"zoho-{t.crm_ticket_id}" for t in tickets)
        assert tickets[0].customer.name == "Customer 1"
        assert tickets[0].customer.email == "customer1@example.com"
        assert tickets[0].priority.value == "high"
        assert tickets[0].status.value == "new"

        # Exactly 3 requests were made (one per page).
        assert mock_get.call_count == 3

    @patch("backend.integrations.zoho_sync.requests.get")
    def test_stops_calling_api_once_more_records_is_false(self, mock_get):
        page1 = _make_response([_zoho_item(1)], more_records=False)
        mock_get.side_effect = [page1]

        client = self._client()
        tickets = client.fetch_tickets()

        assert len(tickets) == 1
        # Only a single page was fetched -- pagination stopped immediately
        # since more_records was false on the first (and only) response.
        mock_get.assert_called_once()

    @patch("backend.integrations.zoho_sync.requests.get")
    def test_missing_info_object_is_treated_as_no_more_records(self, mock_get):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": [_zoho_item(1)]}  # no "info" key at all
        mock_get.side_effect = [resp]

        client = self._client()
        tickets = client.fetch_tickets()

        assert len(tickets) == 1
        mock_get.assert_called_once()

    @patch("backend.integrations.zoho_sync.requests.get")
    def test_pagination_params_layered_on_top_of_filter_dict(self, mock_get):
        page1 = _make_response([_zoho_item(1)], more_records=False)
        mock_get.side_effect = [page1]

        client = self._client()
        client.fetch_tickets(filter_dict={"Status": "New"}, page_size=50)

        _, kwargs = mock_get.call_args
        params = kwargs["params"]
        # Caller-provided filters are preserved...
        assert params["Status"] == "New"
        # ...and pagination params are layered on top rather than replacing them.
        assert params["page"] == 1
        assert params["per_page"] == 50

    @patch("backend.integrations.zoho_sync.requests.get")
    def test_max_records_caps_results_and_stops_early(self, mock_get):
        page1 = _make_response([_zoho_item(1), _zoho_item(2)], more_records=True)
        page2 = _make_response([_zoho_item(3), _zoho_item(4)], more_records=True)
        mock_get.side_effect = [page1, page2]

        client = self._client()
        tickets = client.fetch_tickets(max_records=3)

        assert len(tickets) == 3
        assert [t.crm_ticket_id for t in tickets] == ["1", "2", "3"]
        # Stopped mid-second-page; no third page request was ever made.
        assert mock_get.call_count == 2

    @patch("backend.integrations.zoho_sync.requests.get")
    def test_max_pages_safety_cap_prevents_infinite_loop(self, mock_get):
        # Every page claims there are more records -- without a hard cap
        # this would loop forever.
        def infinite_pages(*args, **kwargs):
            return _make_response([_zoho_item(1)], more_records=True)

        mock_get.side_effect = infinite_pages

        client = self._client()
        tickets = client.fetch_tickets()

        assert mock_get.call_count == MAX_PAGES
        assert len(tickets) == MAX_PAGES

    @patch("backend.integrations.zoho_sync.requests.get")
    def test_no_token_returns_empty_list_without_calling_api(self, mock_get):
        client = ZohoSync(api_base="https://fake.zohoapis.com", token=None)
        tickets = client.fetch_tickets()

        assert tickets == []
        mock_get.assert_not_called()
