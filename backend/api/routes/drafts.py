"""Agent approval/rejection of AI-generated draft responses.

A ticket has at most one "current" AI draft today (``Ticket.ai_draft``), so
drafts are addressed by their owning ticket id rather than a separate draft
id space -- ``{id}`` below is a ticket id.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.auth import require_api_key
from backend.api.dependencies import TicketStore, get_ticket_store
from backend.api.schemas import DraftActionRequest, DraftActionResponse, draft_to_schema
from backend.models.ticket import AiWorkflowState, TicketStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drafts", tags=["drafts"])


def _get_ticket_with_draft(ticket_id: str, store: TicketStore):
    ticket = store.get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    if ticket.ai_draft is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ticket has no AI draft to review",
        )
    return ticket


@router.post("/{ticket_id}/approve", response_model=DraftActionResponse)
def approve_draft(
    ticket_id: str,
    payload: DraftActionRequest,
    store: TicketStore = Depends(get_ticket_store),
    _auth: None = Depends(require_api_key),
) -> DraftActionResponse:
    """Agent approves the AI draft: mark it reviewed and the ticket resolved."""
    ticket = _get_ticket_with_draft(ticket_id, store)

    ticket.update_workflow_state(AiWorkflowState.REVIEWED)
    ticket.status = TicketStatus.RESOLVED
    note = f"Draft approved by {payload.reviewer or 'agent'}."
    if payload.notes:
        note += f" Notes: {payload.notes}"
    ticket.add_message("agent", note)

    store.save(ticket)

    return DraftActionResponse(
        ticket_id=ticket.id,
        action="approved",
        ai_workflow_state=ticket.ai_workflow_state.value,
        ticket_status=ticket.status,
        draft=draft_to_schema(ticket.ai_draft),
    )


@router.post("/{ticket_id}/reject", response_model=DraftActionResponse)
def reject_draft(
    ticket_id: str,
    payload: DraftActionRequest,
    store: TicketStore = Depends(get_ticket_store),
    _auth: None = Depends(require_api_key),
) -> DraftActionResponse:
    """Agent rejects the AI draft: send the ticket back to in-progress for
    reprocessing/manual handling rather than closing it out."""
    ticket = _get_ticket_with_draft(ticket_id, store)

    ticket.update_workflow_state(AiWorkflowState.REVIEWED)
    ticket.status = TicketStatus.IN_PROGRESS
    note = f"Draft rejected by {payload.reviewer or 'agent'}."
    if payload.notes:
        note += f" Reason: {payload.notes}"
    ticket.add_message("agent", note)

    store.save(ticket)

    return DraftActionResponse(
        ticket_id=ticket.id,
        action="rejected",
        ai_workflow_state=ticket.ai_workflow_state.value,
        ticket_status=ticket.status,
        draft=draft_to_schema(ticket.ai_draft),
    )
