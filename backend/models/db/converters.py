"""Two-way conversion between backend.models.ticket dataclasses and the
SQLAlchemy ORM rows in backend.models.db.models.

These are plain functions with no Session/query logic (that lives in
`repository.py`), so they're cheap to unit test in isolation and safe to
reuse from anywhere that needs to translate between the two representations
(e.g. a future CRM sync job writing straight into the ORM layer).
"""
from typing import Optional

from backend.models.db.models import (
    AiDraftORM,
    CustomerORM,
    EscalationInfoORM,
    TicketORM,
)
from backend.models.ticket import (
    AiDraft,
    AiWorkflowState,
    CustomerInfo,
    EscalationInfo,
    Ticket,
    TicketContext,
    TicketPriority,
    TicketStatus,
)


def customer_to_orm(customer: CustomerInfo) -> CustomerORM:
    return CustomerORM(
        id=customer.id,
        name=customer.name,
        email=customer.email,
        phone=customer.phone,
        account_id=customer.account_id,
        crm_link=customer.crm_link,
    )


def orm_to_customer(orm_customer: CustomerORM) -> CustomerInfo:
    return CustomerInfo(
        id=orm_customer.id,
        name=orm_customer.name,
        email=orm_customer.email,
        phone=orm_customer.phone,
        account_id=orm_customer.account_id,
        crm_link=orm_customer.crm_link,
    )


def ai_draft_to_orm(draft: AiDraft, ticket_id: Optional[str] = None) -> AiDraftORM:
    return AiDraftORM(
        ticket_id=ticket_id,
        content=draft.content,
        summary=draft.summary,
        suggested_actions=list(draft.suggested_actions),
        confidence_score=draft.confidence_score,
        model_used=draft.model_used,
        generated_at=draft.generated_at,
    )


def orm_to_ai_draft(orm_draft: AiDraftORM) -> AiDraft:
    return AiDraft(
        content=orm_draft.content,
        summary=orm_draft.summary,
        suggested_actions=list(orm_draft.suggested_actions or []),
        confidence_score=orm_draft.confidence_score,
        model_used=orm_draft.model_used,
        generated_at=orm_draft.generated_at,
    )


def escalation_to_orm(
    escalation: EscalationInfo, ticket_id: Optional[str] = None
) -> EscalationInfoORM:
    return EscalationInfoORM(
        ticket_id=ticket_id,
        reason=escalation.reason,
        escalation_type=escalation.escalation_type,
        escalated_to=escalation.escalated_to,
        jira_issue_id=escalation.jira_issue_id,
        jira_issue_key=escalation.jira_issue_key,
        jira_url=escalation.jira_url,
        escalated_at=escalation.escalated_at,
    )


def orm_to_escalation(orm_escalation: EscalationInfoORM) -> EscalationInfo:
    return EscalationInfo(
        reason=orm_escalation.reason,
        escalation_type=orm_escalation.escalation_type,
        escalated_to=orm_escalation.escalated_to,
        jira_issue_id=orm_escalation.jira_issue_id,
        jira_issue_key=orm_escalation.jira_issue_key,
        jira_url=orm_escalation.jira_url,
        escalated_at=orm_escalation.escalated_at,
    )


def _context_to_dict(context: TicketContext) -> dict:
    return {
        "classification": context.classification,
        "retrieved_knowledge": list(context.retrieved_knowledge),
        "related_tickets": list(context.related_tickets),
        "tags": list(context.tags),
        "custom_fields": dict(context.custom_fields),
    }


def _dict_to_context(data: Optional[dict]) -> TicketContext:
    data = data or {}
    return TicketContext(
        classification=data.get("classification"),
        retrieved_knowledge=list(data.get("retrieved_knowledge") or []),
        related_tickets=list(data.get("related_tickets") or []),
        tags=list(data.get("tags") or []),
        custom_fields=dict(data.get("custom_fields") or {}),
    )


def ticket_to_orm(ticket: Ticket) -> TicketORM:
    """Build a transient `TicketORM` object graph from a `Ticket`.

    Includes the nested customer row and, when present, one AiDraft/
    Escalation history row seeded from the ticket's *current* draft/
    escalation snapshot. The returned object is not yet added to any
    Session -- see `TicketRepository.create`/`.update` for persisting it
    (which also handles reusing an existing customer row and appending to
    history rather than re-inserting on every update).
    """
    orm_ticket = TicketORM(
        id=ticket.id,
        subject=ticket.subject,
        description=ticket.description,
        customer=customer_to_orm(ticket.customer),
        status=ticket.status,
        priority=ticket.priority,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        assigned_to=ticket.assigned_to,
        crm_ticket_id=ticket.crm_ticket_id,
        crm_system=ticket.crm_system,
        crm_link=ticket.crm_link,
        ai_workflow_state=ticket.ai_workflow_state,
        ai_context=_context_to_dict(ticket.ai_context),
        conversation_history=list(ticket.conversation_history),
    )

    if ticket.ai_draft is not None:
        orm_ticket.ai_drafts.append(ai_draft_to_orm(ticket.ai_draft))

    if ticket.escalation_info is not None:
        orm_ticket.escalations.append(escalation_to_orm(ticket.escalation_info))

    return orm_ticket


def orm_to_ticket(orm_ticket: TicketORM) -> Ticket:
    """Build a `Ticket` dataclass from a `TicketORM` row (and its relationships).

    The *latest* ai_draft/escalation row (by timestamp) becomes the
    dataclass's single "current" `ai_draft`/`escalation_info` value.
    """
    latest_draft = (
        max(orm_ticket.ai_drafts, key=lambda d: d.generated_at)
        if orm_ticket.ai_drafts
        else None
    )
    latest_escalation = (
        max(orm_ticket.escalations, key=lambda e: e.escalated_at)
        if orm_ticket.escalations
        else None
    )

    return Ticket(
        id=orm_ticket.id,
        subject=orm_ticket.subject,
        description=orm_ticket.description,
        customer=orm_to_customer(orm_ticket.customer),
        status=TicketStatus(orm_ticket.status),
        priority=TicketPriority(orm_ticket.priority),
        created_at=orm_ticket.created_at,
        updated_at=orm_ticket.updated_at,
        assigned_to=orm_ticket.assigned_to,
        crm_ticket_id=orm_ticket.crm_ticket_id,
        crm_system=orm_ticket.crm_system,
        crm_link=orm_ticket.crm_link,
        ai_workflow_state=AiWorkflowState(orm_ticket.ai_workflow_state),
        ai_context=_dict_to_context(orm_ticket.ai_context),
        ai_draft=orm_to_ai_draft(latest_draft) if latest_draft is not None else None,
        escalation_info=(
            orm_to_escalation(latest_escalation) if latest_escalation is not None else None
        ),
        conversation_history=list(orm_ticket.conversation_history or []),
    )
