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

Field mapping
-------------
Zoho field names (e.g. "Subject", "Contact_Name") vary per org. Override
any of them via `integrations.zoho.field_map` in config/settings.yaml;
anything not overridden falls back to DEFAULT_FIELD_MAP below.
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

DEFAULT_CONFIG_PATH = "config/settings.yaml"

# Zoho CRM v2 allows a maximum of 200 records per page.
ZOHO_MAX_PAGE_SIZE = 200

# Hard safety cap on the number of pages fetched in a single fetch_tickets()
# call, so a pathological/misbehaving API response (e.g. more_records stuck
# at true) can't cause an unbounded loop.
MAX_PAGES = 50

# Retry/backoff defaults for transient Zoho API failures (rate limits, 5xx,
# connection hiccups). Kept as module-level constants so they're easy to
# tune without hunting through the retry logic itself.
DEFAULT_MAX_RETRY_ATTEMPTS = 4
DEFAULT_RETRY_BASE_DELAY = 0.5  # seconds
DEFAULT_RETRY_JITTER = 0.25  # seconds, max additional random delay
RETRYABLE_EXCEPTIONS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)

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


class ZohoAuthError(Exception):
    """Raised when a Zoho access token cannot be obtained or refreshed."""


class ZohoSyncError(Exception):
    """Base exception for all other Zoho sync failures."""


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
    """Raised when Zoho sync is invoked without valid configuration (e.g. missing credentials)."""


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

    NOTE: This is a Phase 2 scaffold. Real deployments should still:
    - Implement webhook listener support (see backend/integrations/zoho_webhook.py)
    - Verify response schemas against your org's actual Zoho customizations
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
        config_path: str = DEFAULT_CONFIG_PATH,
        max_retry_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
        retry_jitter: float = DEFAULT_RETRY_JITTER,
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

        self.field_map = _load_field_map(config_path)
        # Lightweight operational counters. No external metrics dependency;
        # these are plain instance attributes something like a future
        # /metrics endpoint could read.
        self.metrics: Dict[str, int] = {
            "requests_made": 0,
            "requests_failed": 0,
            "tickets_fetched": 0,
        }

        self.max_retry_attempts = max_retry_attempts
        self.retry_base_delay = retry_base_delay
        self.retry_jitter = retry_jitter

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

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Retry/backoff
    # ------------------------------------------------------------------

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """Perform an HTTP request with retry/backoff for transient failures.

        Retries on HTTP 429, any 5xx status code, and connection/timeout
        errors. Uses exponential backoff with jitter between attempts, and
        honors a `Retry-After` header on 429 responses when present.

        Raises the last encountered exception (requests.RequestException) or
        returns the final (non-retryable, or exhausted) requests.Response.
        """
        last_exception: Optional[Exception] = None
        last_response: Optional[requests.Response] = None

        for attempt in range(1, self.max_retry_attempts + 1):
            try:
                response = requests.request(method, url, **kwargs)
            except RETRYABLE_EXCEPTIONS as exc:
                last_exception = exc
                if attempt >= self.max_retry_attempts:
                    logger.error(
                        f"Zoho request {method} {url} failed after {attempt} attempt(s): {exc}"
                    )
                    raise
                delay = self._compute_backoff_delay(attempt)
                logger.warning(
                    f"Zoho request {method} {url} attempt {attempt}/{self.max_retry_attempts} "
                    f"raised {exc!r}; retrying in {delay:.2f}s"
                )
                time.sleep(delay)
                continue

            is_retryable_status = response.status_code == 429 or response.status_code >= 500
            if not is_retryable_status:
                return response

            last_response = response
            if attempt >= self.max_retry_attempts:
                logger.error(
                    f"Zoho request {method} {url} failed after {attempt} attempt(s); "
                    f"final status {response.status_code}"
                )
                return response

            delay = self._compute_backoff_delay(attempt)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        pass

            logger.warning(
                f"Zoho request {method} {url} attempt {attempt}/{self.max_retry_attempts} "
                f"got status {response.status_code}; retrying in {delay:.2f}s"
            )
            time.sleep(delay)

        # Should be unreachable, but keep a defensive fallback.
        if last_exception is not None:
            raise last_exception
        return last_response

    def _compute_backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter for the given attempt number (1-indexed)."""
        return self.retry_base_delay * (2 ** (attempt - 1)) + random.uniform(0, self.retry_jitter)

    @staticmethod
    def _error_details(e: requests.RequestException) -> tuple:
        response = getattr(e, "response", None)
        status_code = getattr(response, "status_code", None)
        body = getattr(response, "text", None)
        return status_code, body

    # ------------------------------------------------------------------
    # Field-mapped record conversion
    # ------------------------------------------------------------------

    def _item_to_ticket(self, item: Dict[str, Any]) -> Ticket:
        """Convert a single Zoho CRM record into an internal Ticket object.

        Uses `self.field_map` so custom Zoho field names (configured via
        `integrations.zoho.field_map` in config/settings.yaml) are honored.
        Extracted as its own method so the same defensive mapping logic can
        be reused across paginated pages (and elsewhere) without duplication.
        """
        fm = self.field_map

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

        return Ticket(
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_tickets(
        self,
        filter_dict: Optional[Dict[str, Any]] = None,
        page_size: int = ZOHO_MAX_PAGE_SIZE,
        max_records: Optional[int] = None,
        raise_on_error: bool = False,
    ) -> List[Ticket]:
        """Fetch tickets from Zoho and convert into internal Ticket objects.

        filter_dict is mapped into query params for Zoho; this implementation keeps it simple.

        Results are paginated automatically: pages are fetched using Zoho's
        `page`/`per_page` query params until the response's
        `info.more_records` is falsy, an optional `max_records` cap is
        reached, or the internal MAX_PAGES safety limit is hit.

        By default (raise_on_error=False), any failure (missing credentials
        or API error) is logged and results in an empty (or partial, if some
        pages already succeeded) list, preserving prior behavior. Pass
        raise_on_error=True to instead raise ZohoConfigError (missing/invalid
        config) or ZohoAPIError (failed API call) so callers can distinguish
        "no records" from "the call failed".

        Args:
            filter_dict: caller-supplied query params; pagination params are
                layered on top and never override existing keys.
            page_size: records requested per page (Zoho's max is 200).
            max_records: optional cap on the total number of records
                returned; None means unlimited (subject to MAX_PAGES).
        """
        if not self._has_credentials():
            logger.debug("Zoho credentials missing; returning empty ticket list (mock mode)")
            if raise_on_error:
                raise ZohoConfigError("Zoho credentials are not configured")
            return []

        base_params = dict(filter_dict or {})
        url = f"{self.api_base}/crm/v2/Tickets"
        tickets: List[Ticket] = []

        page = 1
        pages_fetched = 0
        start = time.perf_counter()
        try:
            while True:
                params = dict(base_params)
                params["page"] = page
                params["per_page"] = page_size

                self.metrics["requests_made"] += 1
                resp = self._request_with_retry("GET", url, headers=self._headers(), params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                pages_fetched += 1

                # Zoho CRM structure varies per org; this code assumes common fields
                for item in data.get("data", []):
                    tickets.append(self._item_to_ticket(item))
                    if max_records is not None and len(tickets) >= max_records:
                        self.metrics["tickets_fetched"] += len(tickets[:max_records])
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
            if raise_on_error:
                raise
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
            return tickets

    def get_ticket_by_crm_id(self, crm_id: str, raise_on_error: bool = False) -> Optional[Ticket]:
        """Fetch a single ticket by its Zoho CRM id and convert to Ticket.

        By default (raise_on_error=False) this returns None on any failure,
        preserving prior behavior. Pass raise_on_error=True to raise
        ZohoConfigError (missing credentials), ZohoAuthError (token refresh
        failed), or ZohoAPIError (failed API call) instead.
        """
        if not self._has_credentials():
            if raise_on_error:
                raise ZohoConfigError("Zoho credentials are not configured")
            return None

        fm = self.field_map
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
            if raise_on_error:
                raise
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
        ZohoConfigError (missing credentials/crm_ticket_id), ZohoAuthError
        (token refresh failed), or ZohoAPIError (failed API call) instead.
        """
        fm = self.field_map
        if not self._has_credentials() or not ticket.crm_ticket_id:
            logger.warning("Cannot sync to Zoho: missing credentials or crm_ticket_id")
            if raise_on_error:
                raise ZohoConfigError("Cannot sync to Zoho: missing credentials or crm_ticket_id")
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
            if raise_on_error:
                raise
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
