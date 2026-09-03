"""Workflow status endpoint.

NOTE: There is no async job queue yet -- ``AgentWorkflow.execute_workflow``
runs synchronously inside the ``POST /tickets`` / ``PUT /tickets/{id}``
request handlers. So there is no independent "workflow run" record to look
up: for now, ``workflow_id == ticket_id``, and this endpoint just reflects
the current ``ticket.ai_workflow_state`` for that ticket. Once a real
worker/queue exists, this should be backed by an actual workflow-run id and
status record.
"""
from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.auth import require_api_key
from backend.api.dependencies import TicketStore, get_ticket_store
from backend.api.schemas import WorkflowStatusResponse

router = APIRouter(prefix="/workflows", tags=["workflows"])


@router.get("/{workflow_id}/status", response_model=WorkflowStatusResponse)
def get_workflow_status(
    workflow_id: str,
    store: TicketStore = Depends(get_ticket_store),
    _auth: None = Depends(require_api_key),
) -> WorkflowStatusResponse:
    # workflow_id == ticket_id today; see module docstring.
    ticket = store.get(workflow_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    return WorkflowStatusResponse(
        workflow_id=workflow_id,
        ticket_id=ticket.id,
        ticket_status=ticket.status,
        ai_workflow_state=ticket.ai_workflow_state.value,
    )
