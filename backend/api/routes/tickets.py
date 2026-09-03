"""Ticket ingestion/retrieval/update endpoints."""
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.auth import require_api_key
from backend.api.dependencies import TicketStore, get_ticket_store
from backend.api.schemas import (
    EscalateRequest,
    TicketCreateRequest,
    TicketCreateResponse,
    TicketResponse,
    TicketUpdateRequest,
    WorkflowResultSchema,
    customer_from_schema,
    ticket_to_response,
)
from backend.core.agent_workflow import AgentWorkflow
from backend.models.ticket import EscalationInfo, Ticket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("", response_model=TicketCreateResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreateRequest,
    store: TicketStore = Depends(get_ticket_store),
    _auth: None = Depends(require_api_key),
) -> TicketCreateResponse:
    """Ingest a new ticket and run it through the AI workflow.

    NOTE: There is no async task queue yet, so ``AgentWorkflow.execute_workflow``
    runs synchronously, inline with the request. Once a real worker/queue
    exists, this should enqueue the workflow run and return immediately
    (e.g. 202 Accepted) with clients polling ``GET /workflows/{id}/status``.
    """
    ticket = Ticket(
        id=str(uuid.uuid4()),
        subject=payload.subject,
        description=payload.description,
        customer=customer_from_schema(payload.customer),
        priority=payload.priority,
        crm_ticket_id=payload.crm_ticket_id,
        crm_system=payload.crm_system,
    )

    workflow_result = AgentWorkflow().execute_workflow(ticket)

    store.save(ticket)

    return TicketCreateResponse(
        ticket=ticket_to_response(ticket),
        workflow=WorkflowResultSchema.model_validate(workflow_result),
    )


@router.get("/{ticket_id}", response_model=TicketResponse)
def get_ticket(
    ticket_id: str,
    store: TicketStore = Depends(get_ticket_store),
    _auth: None = Depends(require_api_key),
) -> TicketResponse:
    ticket = store.get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket_to_response(ticket)


@router.put("/{ticket_id}", response_model=TicketResponse)
def update_ticket(
    ticket_id: str,
    payload: TicketUpdateRequest,
    store: TicketStore = Depends(get_ticket_store),
    _auth: None = Depends(require_api_key),
) -> TicketResponse:
    """Apply agent-driven updates to a ticket (status/priority changes,
    reassignment, or appending a conversation message)."""
    ticket = store.get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    if payload.subject is not None:
        ticket.subject = payload.subject
    if payload.description is not None:
        ticket.description = payload.description
    if payload.status is not None:
        ticket.status = payload.status
    if payload.priority is not None:
        ticket.priority = payload.priority
    if payload.assigned_to is not None:
        ticket.assigned_to = payload.assigned_to
    if payload.add_message is not None:
        ticket.add_message(payload.add_message.role, payload.add_message.content)

    ticket.updated_at = datetime.utcnow()
    store.save(ticket)
    return ticket_to_response(ticket)


@router.post("/{ticket_id}/escalate", response_model=TicketResponse)
def escalate_ticket(
    ticket_id: str,
    payload: EscalateRequest,
    store: TicketStore = Depends(get_ticket_store),
    _auth: None = Depends(require_api_key),
) -> TicketResponse:
    """Manually escalate a ticket.

    Jira integration is separate parallel work, so this endpoint only
    updates local ticket state (status, ai_workflow_state, escalation_info)
    and does not talk to Jira. It is fully functional standalone today.
    """
    ticket = store.get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    escalation_info = EscalationInfo(
        reason=payload.reason,
        escalation_type=payload.escalation_type,
        escalated_to=payload.escalated_to,
    )
    ticket.mark_escalated(escalation_info)
    # TODO: call JiraSync.create_escalation_issue() once available, and
    # populate escalation_info.jira_issue_id/jira_issue_key/jira_url from
    # the result.

    store.save(ticket)
    return ticket_to_response(ticket)
