"""Pydantic (v2) request/response schemas for the REST API layer.

The domain models in ``backend/models/ticket.py`` (``Ticket``, ``CustomerInfo``,
``AiDraft``, ``EscalationInfo``, ...) are plain ``dataclasses`` -- they predate
this API layer and are shared with the core workflow engine, so we don't want
to turn them into Pydantic models directly. Instead this module defines
Pydantic request/response schemas for the HTTP boundary and small adapter
functions that convert between the two representations. Response schemas are
built from ``Ticket.to_dict()`` (which already exists on the dataclass) so we
get a single source of truth for serialization plus a proper OpenAPI schema
for free.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.models.ticket import (
    AiDraft,
    CustomerInfo,
    EscalationInfo,
    Ticket,
    TicketPriority,
    TicketStatus,
)


# ---------------------------------------------------------------------------
# Shared / nested schemas
# ---------------------------------------------------------------------------

class CustomerInfoSchema(BaseModel):
    """Customer information supplied when creating a ticket."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Customer/contact identifier (e.g. CRM contact id)")
    name: str
    email: str
    phone: Optional[str] = None
    account_id: Optional[str] = None
    crm_link: Optional[str] = None


class AiDraftSchema(BaseModel):
    """Read-only view of an AI-generated draft response."""

    # protected_namespaces=() silences Pydantic's "model_used looks like a
    # protected `model_` field" warning -- it's a genuine field name here,
    # mirroring AiDraft.model_used in backend/models/ticket.py.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    content: str
    summary: str
    suggested_actions: List[str]
    confidence_score: float
    model_used: str
    generated_at: datetime


class EscalationInfoSchema(BaseModel):
    """Read-only view of escalation tracking info."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    escalation_type: str
    escalated_to: Optional[str] = None
    jira_issue_id: Optional[str] = None
    jira_issue_key: Optional[str] = None
    jira_url: Optional[str] = None
    escalated_at: datetime


# ---------------------------------------------------------------------------
# Ticket schemas
# ---------------------------------------------------------------------------

class TicketCreateRequest(BaseModel):
    """Body for ``POST /tickets``."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    customer: CustomerInfoSchema
    priority: TicketPriority = TicketPriority.MEDIUM
    crm_ticket_id: Optional[str] = None
    crm_system: str = "zoho"


class TicketUpdateRequest(BaseModel):
    """Body for ``PUT /tickets/{id}``.

    All fields are optional -- only the supplied fields are applied. This
    covers the "agent actions" use case (reassigning, changing status /
    priority, appending a note to the conversation history) without needing
    separate endpoints for each mutation.
    """

    model_config = ConfigDict(extra="forbid")

    subject: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TicketStatus] = None
    priority: Optional[TicketPriority] = None
    assigned_to: Optional[str] = None
    add_message: Optional["ConversationMessageSchema"] = Field(
        default=None,
        description="Optional message to append to the conversation history "
        "as part of this update (e.g. an agent note).",
    )


class ConversationMessageSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(..., description="e.g. 'agent', 'customer', 'system'")
    content: str


TicketUpdateRequest.model_rebuild()


class TicketResponse(BaseModel):
    """Full ticket representation returned by the API.

    Built via ``ticket_to_response()`` from ``Ticket.to_dict()`` rather than
    constructed field-by-field, so it always mirrors the dataclass.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    subject: str
    description: str
    customer: CustomerInfoSchema
    status: TicketStatus
    priority: TicketPriority
    created_at: datetime
    updated_at: datetime
    assigned_to: Optional[str] = None
    crm_ticket_id: Optional[str] = None
    crm_system: str
    ai_workflow_state: str
    ai_context: Dict[str, Any]
    ai_draft: Optional[AiDraftSchema] = None
    escalation_info: Optional[EscalationInfoSchema] = None
    conversation_history: List[Dict[str, str]] = Field(default_factory=list)


class WorkflowResultSchema(BaseModel):
    """Summary of a synchronous ``AgentWorkflow.execute_workflow()`` run."""

    model_config = ConfigDict(extra="ignore")

    ticket_id: str
    steps_executed: List[str]
    final_state: Optional[str] = None
    routing_decision: Optional[Dict[str, Any]] = None
    errors: List[str] = Field(default_factory=list)


class TicketCreateResponse(BaseModel):
    """Response for ``POST /tickets``: the stored ticket plus the workflow
    run that was executed synchronously against it."""

    model_config = ConfigDict(extra="ignore")

    ticket: TicketResponse
    workflow: WorkflowResultSchema


# ---------------------------------------------------------------------------
# Escalation schemas
# ---------------------------------------------------------------------------

class EscalateRequest(BaseModel):
    """Body for ``POST /tickets/{id}/escalate`` (manual escalation)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1)
    escalation_type: str = Field(
        ..., description="e.g. 'technical', 'billing', 'vip'"
    )
    escalated_to: Optional[str] = None


# ---------------------------------------------------------------------------
# Workflow status schemas
# ---------------------------------------------------------------------------

class WorkflowStatusResponse(BaseModel):
    """Response for ``GET /workflows/{id}/status``.

    NOTE: there is no async job queue yet, so workflows execute synchronously
    inline with ``POST /tickets`` / ``PUT /tickets/{id}``. The "workflow id"
    is therefore just the ticket id for now, and this endpoint reflects
    ``ticket.ai_workflow_state`` at read time. Once a real worker/queue
    exists, this should be backed by an actual workflow-run record.
    """

    model_config = ConfigDict(extra="ignore")

    workflow_id: str
    ticket_id: str
    ticket_status: TicketStatus
    ai_workflow_state: str


# ---------------------------------------------------------------------------
# Draft review (agent approval / rejection) schemas
# ---------------------------------------------------------------------------

class DraftActionRequest(BaseModel):
    """Body for ``POST /drafts/{id}/approve`` and ``POST /drafts/{id}/reject``.

    ``{id}`` is the ticket id that owns the draft (a ticket has at most one
    "current" AI draft today, so drafts don't have their own id space yet).
    """

    model_config = ConfigDict(extra="forbid")

    reviewer: Optional[str] = Field(
        default=None, description="Identifier of the agent taking the action"
    )
    notes: Optional[str] = Field(
        default=None, description="Optional reviewer notes / rejection reason"
    )


class DraftActionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticket_id: str
    action: str
    ai_workflow_state: str
    ticket_status: TicketStatus
    draft: Optional[AiDraftSchema] = None


# ---------------------------------------------------------------------------
# Adapters: dataclass -> Pydantic
# ---------------------------------------------------------------------------

def ticket_to_response(ticket: Ticket) -> TicketResponse:
    """Convert a ``Ticket`` dataclass into its API response schema.

    Goes through ``Ticket.to_dict()`` so serialization stays in one place
    (the dataclass) and this layer just validates/wraps it for OpenAPI.
    """
    return TicketResponse.model_validate(ticket.to_dict())


def draft_to_schema(draft: Optional[AiDraft]) -> Optional[AiDraftSchema]:
    if draft is None:
        return None
    return AiDraftSchema.model_validate(draft.__dict__)


def customer_from_schema(schema: CustomerInfoSchema) -> CustomerInfo:
    return CustomerInfo(**schema.model_dump())
