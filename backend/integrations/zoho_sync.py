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
import time
import logging
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


class ZohoSync:
    """Lightweight Zoho sync client.

    NOTE: This is a Phase 2 scaffold. Real deployments should still:
    - Implement full paging, rate-limit backoff, and webhook listener
    - Verify response schemas and map custom fields
    """

    # Refresh this many seconds before the token's reported expiry to
    # avoid using a token that expires mid-request.
    TOKEN_SAFETY_MARGIN_SECONDS = 60

    def __init__(
        self,
        api_base: str = ZOHO_API_BASE,
        token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        accounts_base: Optional[str] = None,
    ):
        self.api_base = api_base
        self.accounts_base = accounts_base or ZOHO_ACCOUNTS_BASE

        self.client_id = client_id or ZOHO_OAUTH_CLIENT_ID
        self.client_secret = client_secret or ZOHO_OAUTH_CLIENT_SECRET
        self.refresh_token = refresh_token or ZOHO_REFRESH_TOKEN
        self._oauth_configured = bool(self.client_id and self.client_secret and self.refresh_token)

        # Deprecated static-token fallback (never logged, never refreshed).
        self._static_token = token or ZOHO_API_TOKEN

        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0

        if self._oauth_configured:
            logger.debug("ZohoSync configured with OAuth2 refresh-token authentication.")
        elif self._static_token:
            logger.warning(
                "ZohoSync is using the deprecated static ZOHO_API_TOKEN for authentication. "
                "This token is never refreshed and will eventually expire or be revoked. "
                "Configure ZOHO_OAUTH_CLIENT_ID, ZOHO_OAUTH_CLIENT_SECRET, and "
                "ZOHO_REFRESH_TOKEN to use the supported OAuth2 refresh-token flow."
            )
        else:
            logger.warning(
                "Zoho credentials are not configured (missing OAuth2 env vars and "
                "ZOHO_API_TOKEN). ZohoSync will not call external APIs."
            )

    def _has_credentials(self) -> bool:
        """Whether any usable form of Zoho credentials has been configured."""
        return bool(self._oauth_configured or self._static_token)

    def _refresh_access_token(self) -> str:
        """Exchange the refresh token for a new access token and cache it.

        Raises:
            ZohoAuthError: if OAuth2 is not configured, the request fails,
                or the response does not contain a usable access token.
        """
        if not self._oauth_configured:
            raise ZohoAuthError(
                "Cannot refresh Zoho access token: OAuth2 credentials "
                "(ZOHO_OAUTH_CLIENT_ID, ZOHO_OAUTH_CLIENT_SECRET, ZOHO_REFRESH_TOKEN) "
                "are not configured."
            )

        url = f"{self.accounts_base}/oauth/v2/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }

        try:
            resp = requests.post(url, data=data, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Failed to refresh Zoho access token: %s", e)
            raise ZohoAuthError(f"Failed to refresh Zoho access token: {e}") from e

        try:
            payload = resp.json()
        except ValueError as e:
            raise ZohoAuthError("Zoho token endpoint returned a non-JSON response") from e

        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")

        if not access_token:
            error_desc = payload.get("error") or "no access_token in response"
            logger.error("Zoho token refresh failed: %s", error_desc)
            raise ZohoAuthError(f"Zoho token refresh failed: {error_desc}")

        if not isinstance(expires_in, (int, float)):
            # Zoho's documented default access token lifetime is 1 hour.
            expires_in = 3600

        self._access_token = access_token
        self._token_expiry = time.time() + expires_in - self.TOKEN_SAFETY_MARGIN_SECONDS
        logger.info("Refreshed Zoho OAuth2 access token (expires_in=%ss)", expires_in)
        return self._access_token

    def _get_valid_token(self) -> str:
        """Return a currently-valid access token, refreshing it if needed.

        Raises:
            ZohoAuthError: if no credentials are configured or refreshing fails.
        """
        if self._oauth_configured:
            if self._access_token and time.time() < self._token_expiry:
                return self._access_token
            return self._refresh_access_token()

        if self._static_token:
            return self._static_token

        raise ZohoAuthError(
            "Zoho credentials are not configured. Set ZOHO_OAUTH_CLIENT_ID, "
            "ZOHO_OAUTH_CLIENT_SECRET, and ZOHO_REFRESH_TOKEN (or the deprecated "
            "ZOHO_API_TOKEN)."
        )

    def _headers(self) -> Dict[str, str]:
        token = self._get_valid_token()
        return {"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"}

    def fetch_tickets(self, filter_dict: Optional[Dict[str, Any]] = None) -> List[Ticket]:
        """Fetch tickets from Zoho and convert into internal Ticket objects.

        filter_dict is mapped into query params for Zoho; this implementation keeps it simple.
        """
        if not self._has_credentials():
            logger.debug("Zoho credentials missing; returning empty ticket list (mock mode)")
            return []

        params = filter_dict or {}
        url = f"{self.api_base}/crm/v2/Tickets"
        try:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            tickets = []
            # Zoho CRM structure varies per org; this code assumes common fields
            for item in data.get("data", []):
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

                ticket = Ticket(
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
                tickets.append(ticket)

            return tickets

        except ZohoAuthError as e:
            logger.error(f"Zoho authentication error while fetching tickets: {e}")
            return []
        except requests.RequestException as e:
            logger.error(f"Error fetching tickets from Zoho: {e}")
            return []

    def get_ticket_by_crm_id(self, crm_id: str) -> Optional[Ticket]:
        """Fetch a single ticket by its Zoho CRM id and convert to Ticket.

        This is a thin wrapper and will return None on errors.
        """
        if not self._has_credentials():
            return None
        url = f"{self.api_base}/crm/v2/Tickets/{crm_id}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=10)
            resp.raise_for_status()
            item = resp.json().get("data", [None])[0]
            if not item:
                return None
            # reuse logic from fetch_tickets - minimal mapping
            crm_id = str(item.get("id"))
            subject = item.get("Subject") or "No subject"
            desc = item.get("Description") or ""
            customer = CustomerInfo(id=str(item.get("Contact_Name", {}).get("id") if isinstance(item.get("Contact_Name"), dict) else "unknown"),
                                    name=item.get("Contact_Name", {}).get("name") if isinstance(item.get("Contact_Name"), dict) else item.get("Contact_Name"),
                                    email=item.get("Email") or "")
            ticket = Ticket(id=f"zoho-{crm_id}", subject=subject, description=desc, customer=customer, crm_ticket_id=crm_id, crm_system="zoho")
            return ticket
        except ZohoAuthError as e:
            logger.error(f"Zoho authentication error while fetching ticket {crm_id}: {e}")
            return None
        except requests.RequestException as e:
            logger.error(f"Error fetching Zoho ticket {crm_id}: {e}")
            return None

    def sync_ticket_to_zoho(self, ticket: Ticket) -> bool:
        """Sync local ticket updates to Zoho.

        For Phase 2 we implement a minimal update of status and comments.
        Returns True on success.
        """
        if not self._has_credentials() or not ticket.crm_ticket_id:
            logger.warning("Cannot sync to Zoho: missing credentials or crm_ticket_id")
            return False

        url = f"{self.api_base}/crm/v2/Tickets/{ticket.crm_ticket_id}"
        payload = {
            "data": [
                {
                    # Map fields; Zoho field names may vary per setup
                    "Status": ticket.status.value,
                    "Description": ticket.description
                }
            ]
        }
        try:
            resp = requests.put(url, headers=self._headers(), json=payload, timeout=10)
            resp.raise_for_status()
            logger.info(f"Synced ticket {ticket.id} to Zoho (crm_id={ticket.crm_ticket_id})")
            return True
        except ZohoAuthError as e:
            logger.error(f"Zoho authentication error while syncing ticket: {e}")
            return False
        except requests.RequestException as e:
            logger.error(f"Failed to sync ticket to Zoho: {e}")
            return False
