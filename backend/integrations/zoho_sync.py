"""Zoho CRM sync helpers (Phase 2 starter implementation)

Provides basic methods to fetch tickets from Zoho and sync updates back.
This is intentionally a lightweight implementation with clear TODOs and
places to harden authentication, paging, rate-limiting, and webhook handling.
"""
import os
import logging
from typing import List, Dict, Any, Optional
import requests
from backend.models.ticket import Ticket, CustomerInfo, TicketPriority, TicketStatus

logger = logging.getLogger(__name__)

ZOHO_API_BASE = os.getenv("ZOHO_API_BASE", "https://www.zohoapis.com")
ZOHO_API_TOKEN = os.getenv("ZOHO_API_TOKEN")  # Bearer token


class ZohoSync:
    """Lightweight Zoho sync client.

    NOTE: This is a Phase 2 scaffold. Real deployments should:
    - Use OAuth2 refresh tokens and automatic refresh
    - Implement full paging, rate-limit backoff, and webhook listener
    - Verify response schemas and map custom fields
    """

    def __init__(self, api_base: str = ZOHO_API_BASE, token: Optional[str] = None):
        self.api_base = api_base
        self.token = token or ZOHO_API_TOKEN
        if not self.token:
            logger.warning("Zoho API token is not configured. ZohoSync will not call external APIs.")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Zoho-oauthtoken {self.token}", "Content-Type": "application/json"}

    def fetch_tickets(self, filter_dict: Optional[Dict[str, Any]] = None) -> List[Ticket]:
        """Fetch tickets from Zoho and convert into internal Ticket objects.

        filter_dict is mapped into query params for Zoho; this implementation keeps it simple.
        """
        if not self.token:
            logger.debug("Zoho token missing; returning empty ticket list (mock mode)")
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

        except requests.RequestException as e:
            logger.error(f"Error fetching tickets from Zoho: {e}")
            return []

    def get_ticket_by_crm_id(self, crm_id: str) -> Optional[Ticket]:
        """Fetch a single ticket by its Zoho CRM id and convert to Ticket.

        This is a thin wrapper and will return None on errors.
        """
        if not self.token:
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
        except requests.RequestException as e:
            logger.error(f"Error fetching Zoho ticket {crm_id}: {e}")
            return None

    def sync_ticket_to_zoho(self, ticket: Ticket) -> bool:
        """Sync local ticket updates to Zoho.

        For Phase 2 we implement a minimal update of status and comments.
        Returns True on success.
        """
        if not self.token or not ticket.crm_ticket_id:
            logger.warning("Cannot sync to Zoho: missing token or crm_ticket_id")
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
        except requests.RequestException as e:
            logger.error(f"Failed to sync ticket to Zoho: {e}")
            return False
