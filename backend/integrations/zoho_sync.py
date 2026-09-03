"""Zoho CRM sync helpers (Phase 2 starter implementation)

Provides basic methods to fetch tickets from Zoho and sync updates back.
This is intentionally a lightweight implementation with clear TODOs and
places to harden authentication, paging, rate-limiting, and webhook handling.
"""
import os
import time
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests
from backend.models.ticket import Ticket, CustomerInfo, TicketPriority, TicketStatus

logger = logging.getLogger(__name__)

ZOHO_API_BASE = os.getenv("ZOHO_API_BASE", "https://www.zohoapis.com")
ZOHO_API_TOKEN = os.getenv("ZOHO_API_TOKEN")  # Bearer token

DEFAULT_CONFIG_PATH = "config/settings.yaml"

# Default Zoho -> internal field mapping. Different Zoho orgs customize field
# names, so this can be overridden via `integrations.zoho.field_map` in
# config/settings.yaml. These defaults preserve the previous hardcoded
# behavior for anyone not customizing their Zoho org.
DEFAULT_FIELD_MAP: Dict[str, str] = {
    "subject": "Subject",
    "description": "Description",
    "priority": "Priority",
    "status": "Status",
    "contact_name": "Contact_Name",
    "email": "Email",
    "phone": "Phone",
    "account_name": "Account_Name",
    "ticket_url": "ticket_url",
}


class ZohoSyncError(Exception):
    """Base exception for all Zoho sync failures."""


class ZohoAPIError(ZohoSyncError):
    """Raised when a call to the Zoho API fails (opt-in via raise_on_error).

    Carries the HTTP status code and response body (when available) so
    callers can distinguish transient/API failures from an empty result set.
    """

    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class ZohoConfigError(ZohoSyncError):
    """Raised when Zoho sync is invoked without valid configuration (e.g. missing token)."""


def _load_field_map(config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, str]:
    """Load `integrations.zoho.field_map` from a YAML config file.

    Falls back to DEFAULT_FIELD_MAP (merging in any partial overrides) if the
    config file, the `integrations.zoho` section, or the `field_map` section
    is missing or malformed, so behavior is unchanged for anyone not
    customizing their Zoho org.
    """
    field_map = dict(DEFAULT_FIELD_MAP)

    try:
        import yaml
    except ImportError:
        logger.debug("PyYAML not installed; using default Zoho field map")
        return field_map

    try:
        path = Path(config_path)
        if not path.is_file():
            logger.debug(f"Zoho config file not found at '{config_path}'; using default field map")
            return field_map

        with path.open("r") as f:
            config = yaml.safe_load(f) or {}

        zoho_config = ((config.get("integrations") or {}).get("zoho") or {})
        custom_map = zoho_config.get("field_map")

        if not custom_map:
            logger.debug(f"No 'integrations.zoho.field_map' section in '{config_path}'; using default field map")
            return field_map

        if not isinstance(custom_map, dict):
            logger.warning(
                f"'integrations.zoho.field_map' in '{config_path}' is not a mapping; using default field map"
            )
            return field_map

        field_map.update({k: v for k, v in custom_map.items() if isinstance(v, str) and v})

    except Exception as e:
        logger.warning(f"Failed to load Zoho field map from '{config_path}': {e}; using default field map")

    return field_map


class ZohoSync:
    """Lightweight Zoho sync client.

    NOTE: This is a Phase 2 scaffold. Real deployments should:
    - Use OAuth2 refresh tokens and automatic refresh
    - Implement full paging, rate-limit backoff, and webhook listener
    - Verify response schemas and map custom fields
    """

    def __init__(
        self,
        api_base: str = ZOHO_API_BASE,
        token: Optional[str] = None,
        config_path: str = DEFAULT_CONFIG_PATH,
    ):
        self.api_base = api_base
        self.token = token or ZOHO_API_TOKEN
        self.field_map = _load_field_map(config_path)
        # Lightweight operational counters. No external metrics dependency;
        # these are plain instance attributes something like a future
        # /metrics endpoint could read.
        self.metrics: Dict[str, int] = {
            "requests_made": 0,
            "requests_failed": 0,
            "tickets_fetched": 0,
        }
        if not self.token:
            logger.warning("Zoho API token is not configured. ZohoSync will not call external APIs.")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Zoho-oauthtoken {self.token}", "Content-Type": "application/json"}

    @staticmethod
    def _error_details(e: requests.RequestException) -> tuple:
        response = getattr(e, "response", None)
        status_code = getattr(response, "status_code", None)
        body = getattr(response, "text", None)
        return status_code, body

    def fetch_tickets(
        self,
        filter_dict: Optional[Dict[str, Any]] = None,
        raise_on_error: bool = False,
    ) -> List[Ticket]:
        """Fetch tickets from Zoho and convert into internal Ticket objects.

        filter_dict is mapped into query params for Zoho; this implementation keeps it simple.

        By default (raise_on_error=False), any failure (missing token or API
        error) is logged and results in an empty list, preserving prior
        behavior. Pass raise_on_error=True to instead raise ZohoConfigError
        (missing/invalid config) or ZohoAPIError (failed API call) so callers
        can distinguish "no records" from "the call failed".
        """
        if not self.token:
            logger.debug("Zoho token missing; returning empty ticket list (mock mode)")
            if raise_on_error:
                raise ZohoConfigError("Zoho API token is not configured")
            return []

        fm = self.field_map
        params = filter_dict or {}
        url = f"{self.api_base}/crm/v2/Tickets"
        start = time.perf_counter()
        self.metrics["requests_made"] += 1
        try:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            tickets = []
            # Zoho CRM structure varies per org; this code assumes common fields
            for item in data.get("data", []):
                # Defensive extraction
                crm_id = str(item.get("id") or item.get("ticket_id") or "")
                subject = item.get(fm["subject"]) or item.get("subject") or "No subject"
                desc = item.get(fm["description"]) or item.get("description") or ""
                contact_field = item.get(fm["contact_name"])
                contact_name = contact_field.get("name") if isinstance(contact_field, dict) else contact_field
                contact_id = contact_field.get("id") if isinstance(contact_field, dict) else None
                account_field = item.get(fm["account_name"])

                customer = CustomerInfo(
                    id=str(contact_id or "unknown"),
                    name=contact_name or "Unknown",
                    email=item.get(fm["email"]) or "",
                    phone=item.get(fm["phone"]),
                    account_id=account_field.get("id") if isinstance(account_field, dict) else None,
                    crm_link=item.get(fm["ticket_url"])
                )

                priority_raw = (item.get(fm["priority"]) or "Medium").lower()
                priority = TicketPriority.MEDIUM
                if "high" in priority_raw:
                    priority = TicketPriority.HIGH
                if "critical" in priority_raw:
                    priority = TicketPriority.CRITICAL
                if "low" in priority_raw:
                    priority = TicketPriority.LOW

                status_raw = (item.get(fm["status"]) or "new").lower().replace(" ", "_")
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
                    crm_link=item.get(fm["ticket_url"])
                )
                tickets.append(ticket)

            elapsed = time.perf_counter() - start
            self.metrics["tickets_fetched"] += len(tickets)
            logger.info(f"Fetched {len(tickets)} ticket(s) from Zoho in {elapsed:.3f}s")
            return tickets

        except requests.RequestException as e:
            self.metrics["requests_failed"] += 1
            elapsed = time.perf_counter() - start
            logger.error(f"Error fetching tickets from Zoho: {e} (after {elapsed:.3f}s)")
            if raise_on_error:
                status_code, body = self._error_details(e)
                raise ZohoAPIError(
                    f"Error fetching tickets from Zoho: {e}", status_code=status_code, response_body=body
                ) from e
            return []

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
            resp = requests.get(url, headers=self._headers(), timeout=10)
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
            resp = requests.put(url, headers=self._headers(), json=payload, timeout=10)
            resp.raise_for_status()
            logger.info(f"Synced ticket {ticket.id} to Zoho (crm_id={ticket.crm_ticket_id})")
            return True
        except requests.RequestException as e:
            self.metrics["requests_failed"] += 1
            logger.error(f"Failed to sync ticket to Zoho: {e}")
            if raise_on_error:
                status_code, body = self._error_details(e)
                raise ZohoAPIError(
                    f"Failed to sync ticket to Zoho: {e}", status_code=status_code, response_body=body
                ) from e
            return False
