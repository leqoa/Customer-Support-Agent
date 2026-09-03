"""Zoho webhook listener (Phase 2)

Alternative/complement to polling via ``ZohoSync.fetch_tickets()``.

Zoho lets you configure an outbound webhook (e.g. a CRM workflow rule) that
POSTs a JSON payload to a URL whenever a ticket-like record is created or
updated. This module exposes a small, self-contained ``APIRouter`` that can
be mounted onto any FastAPI app once an API layer exists (see the TODO
below) -- it does not assume ``backend/api/`` or a database layer exist yet.

Authentication
--------------
Zoho does not sign webhook payloads out of the box, but you can configure a
shared secret as part of the webhook URL/headers in the workflow rule. This
implementation expects that shared secret to be sent by the caller in the
``X-Zoho-Webhook-Token`` request header (a header is simpler to configure
than a query param inside most Zoho workflow-rule UIs and avoids leaking the
secret into request logs/URLs), and compares it against the
``ZOHO_WEBHOOK_SECRET`` environment variable using a constant-time
comparison.

- If ``ZOHO_WEBHOOK_SECRET`` is not configured server-side, the endpoint
  treats webhooks as disabled and returns 503 (never silently accepts
  unauthenticated payloads).
- If the header is missing or does not match, the endpoint returns 401.

Idempotency
-----------
Webhook senders (Zoho included) commonly retry/duplicate deliveries. A
simple in-memory "poor-man's LRU" (``collections.OrderedDict`` capped at
``_SEEN_IDS_MAXLEN`` entries) tracks recently-seen Zoho record ids so a
duplicate delivery within the window is acknowledged without reprocessing.
This is process-local and resets on restart -- fine for Phase 2, but a real
dedup store (e.g. Redis or the eventual DB layer) should replace it once
available.
"""
import hmac
import logging
import os
from collections import OrderedDict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from backend.models.ticket import CustomerInfo, Ticket, TicketPriority, TicketStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/zoho", tags=["zoho-webhook"])

ZOHO_WEBHOOK_SECRET = os.getenv("ZOHO_WEBHOOK_SECRET")

# Poor-man's LRU of recently-seen Zoho record ids, for idempotency.
_SEEN_IDS_MAXLEN = 500
_seen_record_ids: "OrderedDict[str, bool]" = OrderedDict()


def _mark_seen_and_check_duplicate(record_id: str) -> bool:
    """Return True if record_id was already seen (i.e. this is a duplicate).

    As a side effect, marks record_id as seen and evicts the oldest entry
    once the tracked set exceeds _SEEN_IDS_MAXLEN.
    """
    if record_id in _seen_record_ids:
        # Refresh recency.
        _seen_record_ids.move_to_end(record_id)
        return True

    _seen_record_ids[record_id] = True
    if len(_seen_record_ids) > _SEEN_IDS_MAXLEN:
        _seen_record_ids.popitem(last=False)
    return False


class ZohoContactRef(BaseModel):
    """Zoho "lookup" field shape, e.g. Contact_Name / Account_Name."""

    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    name: Optional[str] = None


class ZohoTicketWebhookPayload(BaseModel):
    """Permissive model for a Zoho ticket create/update webhook payload.

    Mirrors the field shape ``ZohoSync.fetch_tickets()`` expects from the
    Zoho CRM Tickets API (Subject/Description/Priority/Status/Contact_Name/
    etc.), since the same org-configured record is likely to show up via
    either channel. Zoho's exact payload shape varies per org/workflow-rule
    configuration, so every field is optional and unrecognized fields are
    allowed rather than rejected.
    """

    model_config = ConfigDict(extra="allow")

    id: Optional[str] = Field(default=None, description="Zoho record id")
    ticket_id: Optional[str] = None
    Subject: Optional[str] = None
    subject: Optional[str] = None
    Description: Optional[str] = None
    description: Optional[str] = None
    Priority: Optional[str] = None
    Status: Optional[str] = None
    Email: Optional[str] = None
    Phone: Optional[str] = None
    ticket_url: Optional[str] = None
    Contact_Name: Optional[ZohoContactRef] = None
    Account_Name: Optional[ZohoContactRef] = None

    def record_id(self) -> Optional[str]:
        return self.id or self.ticket_id


def _extract_contact_ref(value: Optional[ZohoContactRef]) -> Dict[str, Optional[str]]:
    if value is None:
        return {"id": None, "name": None}
    return {"id": value.id, "name": value.name}


def zoho_webhook_payload_to_ticket(payload: ZohoTicketWebhookPayload) -> Ticket:
    """Convert a validated webhook payload into an internal Ticket.

    Mirrors the mapping logic in ZohoSync.fetch_tickets() for consistency
    between the polling and webhook ingestion paths.
    """
    crm_id = str(payload.record_id() or "")
    subject = payload.Subject or payload.subject or "No subject"
    desc = payload.Description or payload.description or ""

    contact = _extract_contact_ref(payload.Contact_Name)
    account = _extract_contact_ref(payload.Account_Name)

    customer = CustomerInfo(
        id=str(contact["id"] or "unknown"),
        name=contact["name"] or "Unknown",
        email=payload.Email or "",
        phone=payload.Phone,
        account_id=account["id"],
        crm_link=payload.ticket_url,
    )

    priority_raw = (payload.Priority or "Medium").lower()
    priority = TicketPriority.MEDIUM
    if "high" in priority_raw:
        priority = TicketPriority.HIGH
    if "critical" in priority_raw:
        priority = TicketPriority.CRITICAL
    if "low" in priority_raw:
        priority = TicketPriority.LOW

    status_raw = (payload.Status or "new").lower().replace(" ", "_")
    ticket_status = TicketStatus.NEW
    for s in TicketStatus:
        if s.value == status_raw:
            ticket_status = s

    return Ticket(
        id=f"zoho-{crm_id}",
        subject=subject,
        description=desc,
        customer=customer,
        status=ticket_status,
        priority=priority,
        crm_ticket_id=crm_id,
        crm_system="zoho",
        crm_link=payload.ticket_url,
    )


def _is_valid_token(provided: Optional[str]) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided, ZOHO_WEBHOOK_SECRET)


@router.post("/tickets", status_code=status.HTTP_202_ACCEPTED)
def receive_ticket_webhook(
    payload: ZohoTicketWebhookPayload,
    response: Response,
    x_zoho_webhook_token: Optional[str] = Header(default=None, alias="X-Zoho-Webhook-Token"),
    token: Optional[str] = Query(default=None, description="Fallback shared-secret param, in case a header can't be configured"),
) -> Dict[str, Any]:
    """Receive a Zoho ticket create/update webhook event.

    Auth: requires ZOHO_WEBHOOK_SECRET to match either the
    X-Zoho-Webhook-Token header or a `token` query param.
    """
    if not ZOHO_WEBHOOK_SECRET:
        logger.error("Zoho webhook received but ZOHO_WEBHOOK_SECRET is not configured; webhooks are disabled")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Zoho webhook listener is not configured (ZOHO_WEBHOOK_SECRET unset)",
        )

    provided_token = x_zoho_webhook_token or token
    if not _is_valid_token(provided_token):
        logger.warning("Rejected Zoho webhook request: missing or invalid webhook token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing webhook token")

    record_id = payload.record_id() or ""
    if record_id and _mark_seen_and_check_duplicate(record_id):
        logger.info(f"Ignoring duplicate Zoho webhook delivery for record id={record_id}")
        response.status_code = status.HTTP_200_OK
        return {"status": "duplicate_ignored", "ticket_id": f"zoho-{record_id}"}

    ticket = zoho_webhook_payload_to_ticket(payload)

    logger.info(f"Received Zoho ticket webhook: id={ticket.id} subject={ticket.subject!r}")

    # TODO: Once an async task queue / API layer exists, enqueue `ticket`
    # for AgentWorkflow.execute_workflow() instead of just logging receipt.
    # For now there is no persistence/queue layer, so we only ack receipt.

    return {"status": "accepted", "ticket_id": ticket.id}
