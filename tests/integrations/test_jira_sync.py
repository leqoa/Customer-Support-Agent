"""Unit tests for JiraSync (backend/integrations/jira_sync.py).

All HTTP calls are mocked - no real Jira instance or credentials are used.
"""
import pytest
from unittest.mock import patch, MagicMock

from backend.integrations.jira_sync import JiraSync, JiraConfigError
from backend.models.ticket import Ticket, CustomerInfo, EscalationInfo


def make_escalation_summary():
    return {
        "escalation_id": "ESC-123-20260101000000",
        "timestamp": "2026-01-01T00:00:00",
        "ticket_id": "123",
        "priority": "high",
        "problem_statement": {
            "customer": "Jane Doe",
            "subject": "Payments failing",
            "description": "Customer cannot complete checkout.",
        },
        "customer_support_investigation": {
            "steps_taken": "Verified account, cleared cache, still failing.",
            "findings": [],
            "workarounds_attempted": [],
        },
        "requested_action": {
            "action": "Investigate payment gateway timeout.",
            "urgency": "high",
            "target_team": None,
        },
        "supporting_information": {},
        "handoff_checklist": [],
    }


def make_ticket(with_escalation_info=False):
    customer = CustomerInfo(id="c1", name="Jane Doe", email="jane@example.com")
    ticket = Ticket(id="t1", subject="Payments failing", description="desc", customer=customer)
    if with_escalation_info:
        ticket.escalation_info = EscalationInfo(reason="prior reason", escalation_type="technical")
    return ticket


@pytest.fixture
def configured_sync():
    return JiraSync(base_url="https://example.atlassian.net", email="bot@example.com", api_token="tok123")


class TestConstructorAndConfig:
    def test_missing_credentials_logs_warning_and_is_unconfigured(self, monkeypatch):
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        monkeypatch.delenv("JIRA_EMAIL", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

        sync = JiraSync()
        assert sync._is_configured() is False

    def test_create_escalation_issue_raises_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        monkeypatch.delenv("JIRA_EMAIL", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

        sync = JiraSync()
        with pytest.raises(JiraConfigError):
            sync.create_escalation_issue(make_escalation_summary())

    def test_update_issue_status_returns_false_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        monkeypatch.delenv("JIRA_EMAIL", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

        sync = JiraSync()
        assert sync.update_issue_status("ITSS-1", "Done") is False


class TestCreateEscalationIssue:
    @patch("backend.integrations.jira_sync.requests.request")
    def test_success_returns_expected_shape(self, mock_request, configured_sync):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": "10001", "key": "ITSS-42", "self": "https://example.atlassian.net/rest/api/3/issue/10001"}
        mock_request.return_value = mock_resp

        result = configured_sync.create_escalation_issue(make_escalation_summary())

        assert result == {
            "id": "10001",
            "key": "ITSS-42",
            "url": "https://example.atlassian.net/browse/ITSS-42",
        }
        # Verify it POSTed to the issue creation endpoint with a Jira-shaped payload.
        args, kwargs = mock_request.call_args
        assert args[0] == "POST"
        assert args[1] == "https://example.atlassian.net/rest/api/3/issue"
        assert kwargs["json"]["fields"]["project"]["key"] == "ITSS"
        assert kwargs["json"]["fields"]["issuetype"]["name"] == "Escalation"

    @patch("backend.integrations.jira_sync.requests.request")
    def test_non_2xx_raises_jira_api_error(self, mock_request, configured_sync):
        from backend.integrations.jira_sync import JiraAPIError

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"errorMessages": ["bad request"]}'
        mock_request.return_value = mock_resp

        with pytest.raises(JiraAPIError) as excinfo:
            configured_sync.create_escalation_issue(make_escalation_summary())

        assert excinfo.value.status_code == 400

    @patch("backend.integrations.jira_sync.time.sleep", return_value=None)
    @patch("backend.integrations.jira_sync.requests.request")
    def test_retries_on_5xx_then_succeeds(self, mock_request, _mock_sleep, configured_sync):
        fail_resp = MagicMock(status_code=503, text="upstream error")
        ok_resp = MagicMock(status_code=201)
        ok_resp.json.return_value = {"id": "1", "key": "ITSS-1"}
        mock_request.side_effect = [fail_resp, ok_resp]

        result = configured_sync.create_escalation_issue(make_escalation_summary())

        assert result["key"] == "ITSS-1"
        assert mock_request.call_count == 2


class TestUpdateIssueStatus:
    @patch("backend.integrations.jira_sync.requests.request")
    def test_finds_and_uses_matching_transition(self, mock_request, configured_sync):
        transitions_resp = MagicMock(status_code=200)
        transitions_resp.json.return_value = {
            "transitions": [
                {"id": "11", "to": {"name": "In Progress"}},
                {"id": "31", "to": {"name": "Done"}},
            ]
        }
        post_resp = MagicMock(status_code=204)
        mock_request.side_effect = [transitions_resp, post_resp]

        result = configured_sync.update_issue_status("ITSS-42", "done")  # case-insensitive match

        assert result is True
        # Second call should be the POST with the matched transition id (31).
        second_call_args, second_call_kwargs = mock_request.call_args_list[1]
        assert second_call_args[0] == "POST"
        assert second_call_kwargs["json"] == {"transition": {"id": "31"}}

    @patch("backend.integrations.jira_sync.requests.request")
    def test_returns_false_when_no_matching_transition(self, mock_request, configured_sync):
        transitions_resp = MagicMock(status_code=200)
        transitions_resp.json.return_value = {
            "transitions": [
                {"id": "11", "to": {"name": "In Progress"}},
            ]
        }
        mock_request.return_value = transitions_resp

        result = configured_sync.update_issue_status("ITSS-42", "Resolved")

        assert result is False
        # Only the GET for transitions should have happened - no POST attempted.
        assert mock_request.call_count == 1


class TestLinkTicketToJira:
    def test_creates_minimal_escalation_info_when_absent(self, configured_sync):
        ticket = make_ticket(with_escalation_info=False)

        result = configured_sync.link_ticket_to_jira(ticket, "ITSS-99")

        assert result is True
        assert ticket.escalation_info is not None
        assert ticket.escalation_info.jira_issue_key == "ITSS-99"
        assert ticket.escalation_info.jira_url == "https://example.atlassian.net/browse/ITSS-99"

    def test_updates_existing_escalation_info(self, configured_sync):
        ticket = make_ticket(with_escalation_info=True)
        original_reason = ticket.escalation_info.reason

        result = configured_sync.link_ticket_to_jira(ticket, "ITSS-100")

        assert result is True
        assert ticket.escalation_info.reason == original_reason  # untouched
        assert ticket.escalation_info.jira_issue_key == "ITSS-100"
        assert ticket.escalation_info.jira_url == "https://example.atlassian.net/browse/ITSS-100"

    def test_returns_false_without_jira_key(self, configured_sync):
        ticket = make_ticket()
        assert configured_sync.link_ticket_to_jira(ticket, "") is False
