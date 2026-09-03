"""Zoho CRM sync helpers (Phase 2 starter implementation)

Provides basic methods to fetch tickets from Zoho and sync updates back.
This is intentionally a lightweight implementation with clear TODOs and
places to harden paging, rate-limiting, and webhook handling.

Authentication
---------------
ZohoSync authenticates using Zoho's OAuth2 refresh-token flow. Required
environment variables:

- ``ZOHO_OAUTH_CLIENT_ID``      - OAuth2 client id from the Zoho API console
- ``ZOHO_OAUTH_CLIENT_SECRET``  - OAuth2 client secret from the Zoho API console
- ``ZOHO_REFRESH_TOKEN``        - Long-lived refresh token issued for the client

Optional:

- ``ZOHO_ACCOUNTS_BASE`` - Zoho accounts/token endpoint base URL, for
  region-specific data centers (default: https://accounts.zoho.com; use
  e.g. https://accounts.zoho.eu, https://accounts.zoho.in,
  https://accounts.zoho.com.au as appropriate for your Zoho org's DC).

Access tokens are fetched on demand and cached in memory until shortly
before they expire, then transparently refreshed.

Deprecated: a static ``ZOHO_API_TOKEN`` env var is still honored as a
fallback when the OAuth2 env vars above are not set, for backward
compatibility with older deployments. It is never refreshed and logs a
deprecation warning; new deployments should use the OAuth2 flow instead.
"""
import os
import random
import time
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests
from backend.models.ticket import Ticket, CustomerInfo, TicketPriority, TicketStatus

logger = logging.getLogger(__name__)

ZOHO_API_BASE = os.getenv("ZOHO_API_BASE", "https://www.zohoapis.com")
ZOHO_ACCOUNTS_BASE = os.getenv("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.com")
ZOHO_OAUTH_CLIENT_ID = os.getenv("ZOHO_OAUTH_CLIENT_ID")
ZOHO_OAUTH_CLIENT_SECRET = os.getenv("ZOHO_OAUTH_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
ZOHO_API_TOKEN = os.getenv("ZOHO_API_TOKEN")  # Deprecated static bearer token


class ZohoAuthError(Exception):
    """Raised when a Zoho access token cannot be obtained or refreshed."""

# Zoho CRM v2 allows a maximum of 200 records per page.
ZOHO_MAX_PAGE_SIZE = 200

# Hard safety cap on the number of pages fetched in a single fetch_tickets()
# call, so a pathological/misbehaving API response (e.g. more_records stuck
# at true) can't cause an unbounded loop.
MAX_PAGES = 50


class ZohoSync:
    """Lightweight Zoho sync client.

    NOTE: This is a Phase 2 scaffold. Real deployments should:
    - Use OAuth2 refresh tokens and automatic refresh
    - Implement rate-limit backoff and webhook listener
    - Verify response schemas and map custom fields
    """

    def __init__(
        self,
        api_base: str = ZOHO_API_BASE,
        token: Optional[str] = None,
        max_retry_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
        retry_jitter: float = DEFAULT_RETRY_JITTER,
    ):
        self.api_base = api_base
        self.token = token or ZOHO_API_TOKEN
        self.max_retry_attempts = max_retry_attempts
        self.retry_base_delay = retry_base_delay
        self.retry_jitter = retry_jitter
        if not self.token:
            logger.warning("Zoho API token is not configured. ZohoSync will not call external APIs.")

    def _headers(self) -> Dict[str, str]:
        token = self._get_valid_token()
        return {"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"}

    @staticmethod
    def _item_to_ticket(item: Dict[str, Any]) -> Ticket:
        """Convert a single Zoho CRM record into an internal Ticket object.

        Extracted from fetch_tickets() so the same defensive mapping logic
        can be reused across paginated pages (and elsewhere) without
        duplication.
        """
        # Defensive extraction
        crm_id = str(item.get("id") or item.get("ticket_id") or "")
        subject = item.get("Subject") or item.get("subject") or "No subject"
        desc = item.get("Description") or item.get("description") or ""
        contact_name = item.get("Contact_Name", {}).get("name") if isinstance(item.get("Contact_Name"), dict) else item.get("Contact_Name")
        contact_id = (item.get("Contact_Name", {}).get("id") if isinstance(item.get("Contact_Name"), dict) else None)

        customer = CustomerInfo(
            id=str(contact_id or "unknown"),
            name=contact_name or "Unknown",
            email=item.get("Email") or "",
            phone=item.get("Phone"),
            account_id=item.get("Account_Name", {}).get("id") if isinstance(item.get("Account_Name"), dict) else None,
            crm_link=item.get("ticket_url")
        )

        priority_raw = (item.get("Priority") or "Medium").lower()
        priority = TicketPriority.MEDIUM
        if "high" in priority_raw:
            priority = TicketPriority.HIGH
        if "critical" in priority_raw:
            priority = TicketPriority.CRITICAL
        if "low" in priority_raw:
            priority = TicketPriority.LOW

        status_raw = (item.get("Status") or "new").lower().replace(" ", "_")
        status = TicketStatus.NEW
        for s in TicketStatus:
            if s.value == status_raw:
                status = s

        return Ticket(
            id=f"zoho-{crm_id}",
            subject=subject,
            description=desc,
            customer=customer,
            status=status,
            priority=priority,
            crm_ticket_id=crm_id,
            crm_system="zoho",
            crm_link=item.get("ticket_url")
        )

    def fetch_tickets(
        self,
        filter_dict: Optional[Dict[str, Any]] = None,
        page_size: int = ZOHO_MAX_PAGE_SIZE,
        max_records: Optional[int] = None,
    ) -> List[Ticket]:
        """Fetch tickets from Zoho and convert into internal Ticket objects.

        filter_dict is mapped into query params for Zoho; this implementation keeps it simple.

        Results are paginated automatically: pages are fetched using Zoho's
        `page`/`per_page` query params until the response's
        `info.more_records` is falsy, an optional `max_records` cap is
        reached, or the internal MAX_PAGES safety limit is hit.

        Args:
            filter_dict: caller-supplied query params; pagination params are
                layered on top and never override existing keys.
            page_size: records requested per page (Zoho's max is 200).
            max_records: optional cap on the total number of records
                returned; None means unlimited (subject to MAX_PAGES).
        """
        if not self.token:
            logger.debug("Zoho token missing; returning empty ticket list (mock mode)")
            if raise_on_error:
                raise ZohoConfigError("Zoho API token is not configured")
            return []

        base_params = dict(filter_dict or {})
        url = f"{self.api_base}/crm/v2/Tickets"
        tickets: List[Ticket] = []

        page = 1
        pages_fetched = 0
        try:
            while True:
                params = dict(base_params)
                params["page"] = page
                params["per_page"] = page_size

                resp = requests.get(url, headers=self._headers(), params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                pages_fetched += 1

                # Zoho CRM structure varies per org; this code assumes common fields
                for item in data.get("data", []):
                    tickets.append(self._item_to_ticket(item))
                    if max_records is not None and len(tickets) >= max_records:
                        return tickets[:max_records]

                info = data.get("info") or {}
                more_records = bool(info.get("more_records"))
                if not more_records:
                    break

                if pages_fetched >= MAX_PAGES:
                    logger.warning(
                        f"Reached MAX_PAGES ({MAX_PAGES}) while fetching Zoho tickets; "
                        "stopping pagination early. Remaining records were not fetched."
                    )
                    break

                page += 1

            elapsed = time.perf_counter() - start
            self.metrics["tickets_fetched"] += len(tickets)
            logger.info(f"Fetched {len(tickets)} ticket(s) from Zoho in {elapsed:.3f}s")
            return tickets

        except ZohoAuthError as e:
            logger.error(f"Zoho authentication error while fetching tickets: {e}")
            return []
        except requests.RequestException as e:
            logger.error(f"Error fetching tickets from Zoho: {e}")
            return tickets

    def get_ticket_by_crm_id(self, crm_id: str, raise_on_error: bool = False) -> Optional[Ticket]:
        """Fetch a single ticket by its Zoho CRM id and convert to Ticket.

        By default (raise_on_error=False) this returns None on any failure,
        preserving prior behavior. Pass raise_on_error=True to raise
        ZohoConfigError (missing token) or ZohoAPIError (failed API call)
        instead.
        """
        fm = self.field_map
        if not self.token:
            if raise_on_error:
                raise ZohoConfigError("Zoho API token is not configured")
            return None
        url = f"{self.api_base}/crm/v2/Tickets/{crm_id}"
        self.metrics["requests_made"] += 1
        try:
            resp = self._request_with_retry("GET", url, headers=self._headers(), timeout=10)
            resp.raise_for_status()
            item = resp.json().get("data", [None])[0]
            if not item:
                return None
            # reuse logic from fetch_tickets - minimal mapping
            crm_id = str(item.get("id"))
            subject = item.get(fm["subject"]) or "No subject"
            desc = item.get(fm["description"]) or ""
            contact_field = item.get(fm["contact_name"])
            customer = CustomerInfo(
                id=str(contact_field.get("id") if isinstance(contact_field, dict) else "unknown"),
                name=contact_field.get("name") if isinstance(contact_field, dict) else contact_field,
                email=item.get(fm["email"]) or ""
            )
            ticket = Ticket(id=f"zoho-{crm_id}", subject=subject, description=desc, customer=customer, crm_ticket_id=crm_id, crm_system="zoho")
            return ticket
        except ZohoAuthError as e:
            logger.error(f"Zoho authentication error while fetching ticket {crm_id}: {e}")
            return None
        except requests.RequestException as e:
            self.metrics["requests_failed"] += 1
            logger.error(f"Error fetching Zoho ticket {crm_id}: {e}")
            if raise_on_error:
                status_code, body = self._error_details(e)
                raise ZohoAPIError(
                    f"Error fetching Zoho ticket {crm_id}: {e}", status_code=status_code, response_body=body
                ) from e
            return None

    def sync_ticket_to_zoho(self, ticket: Ticket, raise_on_error: bool = False) -> bool:
        """Sync local ticket updates to Zoho.

        For Phase 2 we implement a minimal update of status and comments.
        Returns True on success.

        By default (raise_on_error=False) this returns False on any failure,
        preserving prior behavior. Pass raise_on_error=True to raise
        ZohoConfigError (missing token/crm_ticket_id) or ZohoAPIError (failed
        API call) instead.
        """
        fm = self.field_map
        if not self.token or not ticket.crm_ticket_id:
            logger.warning("Cannot sync to Zoho: missing token or crm_ticket_id")
            if raise_on_error:
                raise ZohoConfigError("Cannot sync to Zoho: missing token or crm_ticket_id")
            return False

        url = f"{self.api_base}/crm/v2/Tickets/{ticket.crm_ticket_id}"
        payload = {
            "data": [
                {
                    # Map fields; Zoho field names may vary per setup
                    fm["status"]: ticket.status.value,
                    fm["description"]: ticket.description
                }
            ]
        }
        self.metrics["requests_made"] += 1
        try:
            resp = self._request_with_retry("PUT", url, headers=self._headers(), json=payload, timeout=10)
            resp.raise_for_status()
            logger.info(f"Synced ticket {ticket.id} to Zoho (crm_id={ticket.crm_ticket_id})")
            return True
        except ZohoAuthError as e:
            logger.error(f"Zoho authentication error while syncing ticket: {e}")
            return False
        except requests.RequestException as e:
            self.metrics["requests_failed"] += 1
            logger.error(f"Failed to sync ticket to Zoho: {e}")
            if raise_on_error:
                status_code, body = self._error_details(e)
                raise ZohoAPIError(
                    f"Failed to sync ticket to Zoho: {e}", status_code=status_code, response_body=body
                ) from e
            return False
