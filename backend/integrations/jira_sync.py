"""Jira Cloud sync helpers (Phase 2 starter implementation)

Provides basic methods to create and manage escalation issues in Jira Cloud
for tickets that have been escalated by CS/AI workflows. This is intentionally
a lightweight implementation with clear TODOs and places to harden auth,
webhook-driven status sync, and richer field mapping.

Error-handling contract (documented here so it's easy to reason about from a
single place, mirroring the "be consistent" guidance for this module):

- ``create_escalation_issue`` returns a ``Dict[str, Any]`` on success
  (``{"id", "key", "url"}``). Because there is no sensible "empty" dict to
  signal failure without the caller having to guess, failures are raised as
  exceptions instead: ``JiraConfigError`` when credentials are missing, and
  ``JiraAPIError`` (carrying ``status_code`` and ``response_body``) when the
  Jira API returns a non-2xx response after retries are exhausted.
- ``update_issue_status`` and ``link_ticket_to_jira`` return ``bool``. For
  boolean-returning methods we follow the same "safe failure" convention used
  by ``ZohoSync`` elsewhere in this package: missing config, network errors,
  and "no matching transition" all log a warning/error and return ``False``
  rather than raising, since callers of these methods are typically doing
  best-effort synchronization and a raised exception would be disproportionate
  to an expected, recoverable condition (e.g. a workflow without that status).

Auth: Jira Cloud uses HTTP Basic auth with the account email + an API token
(https://id.atlassian.com/manage-profile/security/api-tokens) - this is the
standard "API token" flow, not OAuth.
"""
import os
import time
import logging
from typing import Any, Dict, Optional

import requests

from backend.models.ticket import EscalationInfo
from backend.utils.escalation_formatter import EscalationFormatter

logger = logging.getLogger(__name__)

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "ITSS")

# Default issue type used by EscalationFormatter.format_for_jira(). The target
# Jira project must define an "Escalation" issue type (or this should be made
# configurable end-to-end, e.g. by templating it in EscalationFormatter too) -
# left as a follow-up given time constraints, since hardening every workflow
# edge case is out of scope for this Phase 2 pass.
DEFAULT_ISSUE_TYPE = "Escalation"

REQUEST_TIMEOUT = 10  # seconds
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5  # seconds; doubles each retry (0.5s, 1s, 2s)


class JiraConfigError(Exception):
    """Raised when JiraSync is asked to call the API without valid credentials."""


class JiraAPIError(Exception):
    """Raised when the Jira API returns a non-2xx response after retries are exhausted."""

    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class JiraSync:
    """Lightweight Jira Cloud sync client.

    NOTE: This is a Phase 2 scaffold. Real deployments should:
    - Cache/validate the project's available issue types and workflow statuses
    - Listen for Jira webhooks to sync status changes back into tickets
      instead of (or in addition to) polling `update_issue_status`
    - Support custom field mapping per Jira project
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        project_key: Optional[str] = None,
    ):
        self.base_url = (base_url or JIRA_BASE_URL or "").rstrip("/")
        self.email = email or JIRA_EMAIL
        self.api_token = api_token or JIRA_API_TOKEN
        self.project_key = project_key or JIRA_PROJECT_KEY

        if not (self.base_url and self.email and self.api_token):
            logger.warning(
                "Jira credentials are not fully configured (need JIRA_BASE_URL, "
                "JIRA_EMAIL, JIRA_API_TOKEN). JiraSync will not call external APIs."
            )

    def _is_configured(self) -> bool:
        return bool(self.base_url and self.email and self.api_token)

    def _auth(self) -> requests.auth.HTTPBasicAuth:
        return requests.auth.HTTPBasicAuth(self.email, self.api_token)

    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json", "Accept": "application/json"}

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """Issue an HTTP request with simple manual exponential backoff.

        Retries up to MAX_RETRIES attempts total, only on 5xx responses or
        transient network exceptions. 4xx responses (bad auth, bad request,
        not found) are returned immediately without retrying since retrying
        won't change the outcome.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.request(method, url, auth=self._auth(), headers=self._headers(), timeout=REQUEST_TIMEOUT, **kwargs)
                if resp.status_code >= 500 and attempt < MAX_RETRIES:
                    logger.warning(f"Jira API {method} {url} returned {resp.status_code}; retrying (attempt {attempt}/{MAX_RETRIES})")
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue
                return resp
            except requests.RequestException as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    logger.warning(f"Jira API {method} {url} raised {e}; retrying (attempt {attempt}/{MAX_RETRIES})")
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue
                raise
        # Should be unreachable, but keep mypy/type-checkers happy.
        if last_exc:
            raise last_exc
        raise JiraAPIError(f"Exhausted retries calling {method} {url}")

    def create_escalation_issue(self, escalation_summary: Dict[str, Any]) -> Dict[str, Any]:
        """Create a Jira issue from a raw escalation summary.

        Formats `escalation_summary` via `EscalationFormatter.format_for_jira`
        internally (callers pass the raw summary dict, not a pre-built payload)
        and POSTs it to `{base_url}/rest/api/3/issue`.

        Returns:
            Dict with "id", "key", and "url" (a browsable link) on success.

        Raises:
            JiraConfigError: if credentials/base_url are not configured.
            JiraAPIError: if Jira returns a non-2xx response after retries.
        """
        if not self._is_configured():
            raise JiraConfigError(
                "Cannot create Jira issue: JiraSync is missing base_url/email/api_token "
                "(set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)."
            )

        payload = EscalationFormatter.format_for_jira(escalation_summary, jira_project_key=self.project_key)
        url = f"{self.base_url}/rest/api/3/issue"

        try:
            resp = self._request_with_retry("POST", url, json=payload)
        except requests.RequestException as e:
            raise JiraAPIError(f"Network error creating Jira issue: {e}")

        if resp.status_code not in (200, 201):
            logger.error(f"Failed to create Jira issue: {resp.status_code} {resp.text}")
            raise JiraAPIError(
                f"Jira issue creation failed with status {resp.status_code}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        data = resp.json()
        issue_id = data.get("id")
        issue_key = data.get("key")
        result = {
            "id": issue_id,
            "key": issue_key,
            "url": f"{self.base_url}/browse/{issue_key}" if issue_key else None,
        }
        logger.info(f"Created Jira escalation issue {issue_key} (id={issue_id})")
        return result

    def update_issue_status(self, jira_key: str, status: str) -> bool:
        """Transition a Jira issue to the given status by name (case-insensitive).

        Jira Cloud doesn't allow setting status via a direct field PATCH; the
        allowed transitions (and their ids) are workflow-specific, so this:
          1. GETs `{base_url}/rest/api/3/issue/{jira_key}/transitions`
          2. Finds the transition whose `to.name` matches `status`
          3. POSTs `{"transition": {"id": <id>}}` to the same endpoint

        Returns:
            True on success. False (with a logged warning) if credentials are
            missing, the API call fails, or no transition to the requested
            status exists in this issue's workflow - the latter is treated as
            an expected, recoverable case rather than an error.
        """
        if not self._is_configured():
            logger.warning("Cannot update Jira issue status: JiraSync is not configured.")
            return False

        transitions_url = f"{self.base_url}/rest/api/3/issue/{jira_key}/transitions"
        try:
            resp = self._request_with_retry("GET", transitions_url)
        except requests.RequestException as e:
            logger.error(f"Error fetching Jira transitions for {jira_key}: {e}")
            return False

        if resp.status_code != 200:
            logger.error(f"Failed to fetch Jira transitions for {jira_key}: {resp.status_code} {resp.text}")
            return False

        transitions = resp.json().get("transitions", [])
        match = next(
            (t for t in transitions if t.get("to", {}).get("name", "").lower() == status.lower()),
            None,
        )
        if not match:
            available = [t.get("to", {}).get("name") for t in transitions]
            logger.warning(f"No transition to status '{status}' found for {jira_key}. Available: {available}")
            return False

        try:
            resp = self._request_with_retry("POST", transitions_url, json={"transition": {"id": match["id"]}})
        except requests.RequestException as e:
            logger.error(f"Error posting Jira transition for {jira_key}: {e}")
            return False

        if resp.status_code not in (200, 204):
            logger.error(f"Failed to transition Jira issue {jira_key}: {resp.status_code} {resp.text}")
            return False

        logger.info(f"Transitioned Jira issue {jira_key} to '{status}'")
        return True

    def link_ticket_to_jira(self, ticket, jira_key: str) -> bool:
        """Record Jira linkage on a local Ticket object (no HTTP call).

        Sets `jira_issue_id`/`jira_issue_key`/`jira_url` on `ticket.escalation_info`
        if it already exists, or creates a minimal `EscalationInfo` if not. This
        is purely in-memory bookkeeping; callers are expected to persist the
        ticket via whatever storage layer they use.

        Returns:
            True on success, False if `ticket` is falsy or `jira_key` is empty.
        """
        if not ticket or not jira_key:
            logger.warning("Cannot link ticket to Jira: missing ticket or jira_key.")
            return False

        jira_url = f"{self.base_url}/browse/{jira_key}" if self.base_url else None

        if getattr(ticket, "escalation_info", None):
            ticket.escalation_info.jira_issue_key = jira_key
            ticket.escalation_info.jira_issue_id = jira_key
            ticket.escalation_info.jira_url = jira_url
        else:
            ticket.escalation_info = EscalationInfo(
                reason="Escalated to Jira",
                escalation_type="general",
                jira_issue_id=jira_key,
                jira_issue_key=jira_key,
                jira_url=jira_url,
            )

        logger.info(f"Linked ticket {getattr(ticket, 'id', '?')} to Jira issue {jira_key}")
        return True
