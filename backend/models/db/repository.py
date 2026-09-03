"""Persistence-facing repository wrapping common Ticket queries.

Intentionally free of any web-framework concerns -- this is meant as the
swap-in replacement for whatever in-memory ticket store the (separately
built) API layer currently uses. Give it a `sqlalchemy.orm.Session` (e.g.
obtained via `backend.models.db.base.get_db`) and it hands back/accepts plain
`Ticket` dataclasses; ORM/session details never leak past this module.
"""
from typing import List, Optional

from sqlalchemy.orm import Session, selectinload

from backend.models.db.converters import ai_draft_to_orm, escalation_to_orm, orm_to_ticket, ticket_to_orm
from backend.models.db.models import CustomerORM, TicketORM
from backend.models.ticket import Ticket


class TicketRepository:
    """CRUD-style access to tickets, backed by a SQLAlchemy `Session`."""

    def __init__(self, session: Session):
        self.session = session

    def _query(self):
        # selectinload (rather than joinedload) avoids a join-fanout across
        # the two one-to-many collections (ai_drafts, escalations) being
        # loaded on the same query.
        return self.session.query(TicketORM).options(
            selectinload(TicketORM.customer),
            selectinload(TicketORM.ai_drafts),
            selectinload(TicketORM.escalations),
        )

    def create(self, ticket: Ticket) -> Ticket:
        """Insert a new ticket, plus its customer/draft/escalation snapshot.

        The customer row is upserted: if a customer with this id already
        exists (from an earlier ticket) it's reused instead of re-inserted.
        """
        existing_customer = self.session.get(CustomerORM, ticket.customer.id)

        orm_ticket = ticket_to_orm(ticket)
        if existing_customer is not None:
            orm_ticket.customer = existing_customer

        self.session.add(orm_ticket)
        self.session.commit()
        self.session.refresh(orm_ticket)
        return orm_to_ticket(orm_ticket)

    def get_by_id(self, ticket_id: str) -> Optional[Ticket]:
        orm_ticket = self._query().filter(TicketORM.id == ticket_id).first()
        return orm_to_ticket(orm_ticket) if orm_ticket is not None else None

    def get_by_crm_id(self, crm_system: str, crm_id: str) -> Optional[Ticket]:
        orm_ticket = (
            self._query()
            .filter(TicketORM.crm_system == crm_system, TicketORM.crm_ticket_id == crm_id)
            .first()
        )
        return orm_to_ticket(orm_ticket) if orm_ticket is not None else None

    def update(self, ticket: Ticket) -> Ticket:
        """Update an existing ticket in place.

        Scalar fields are overwritten outright. `ai_draft`/`escalation_info`
        are history-tracked: a new row is appended only when it differs from
        the most recently stored one, so calling `update()` repeatedly with
        an unchanged draft doesn't spam the history table.

        Raises `ValueError` if no ticket with `ticket.id` exists yet (use
        `create()` for that).
        """
        orm_ticket = self._query().filter(TicketORM.id == ticket.id).first()
        if orm_ticket is None:
            raise ValueError(f"No ticket found with id={ticket.id!r} to update")

        orm_ticket.subject = ticket.subject
        orm_ticket.description = ticket.description
        orm_ticket.status = ticket.status
        orm_ticket.priority = ticket.priority
        orm_ticket.updated_at = ticket.updated_at
        orm_ticket.assigned_to = ticket.assigned_to
        orm_ticket.crm_ticket_id = ticket.crm_ticket_id
        orm_ticket.crm_system = ticket.crm_system
        orm_ticket.crm_link = ticket.crm_link
        orm_ticket.ai_workflow_state = ticket.ai_workflow_state
        orm_ticket.ai_context = {
            "classification": ticket.ai_context.classification,
            "retrieved_knowledge": list(ticket.ai_context.retrieved_knowledge),
            "related_tickets": list(ticket.ai_context.related_tickets),
            "tags": list(ticket.ai_context.tags),
            "custom_fields": dict(ticket.ai_context.custom_fields),
        }
        orm_ticket.conversation_history = list(ticket.conversation_history)

        if ticket.ai_draft is not None:
            latest = max(orm_ticket.ai_drafts, key=lambda d: d.generated_at, default=None)
            draft_changed = latest is None or (
                latest.content != ticket.ai_draft.content
                or latest.summary != ticket.ai_draft.summary
                or latest.confidence_score != ticket.ai_draft.confidence_score
                or latest.model_used != ticket.ai_draft.model_used
                or list(latest.suggested_actions or []) != list(ticket.ai_draft.suggested_actions)
            )
            if draft_changed:
                orm_ticket.ai_drafts.append(ai_draft_to_orm(ticket.ai_draft))

        if ticket.escalation_info is not None:
            latest = max(orm_ticket.escalations, key=lambda e: e.escalated_at, default=None)
            escalation_changed = latest is None or (
                latest.reason != ticket.escalation_info.reason
                or latest.escalation_type != ticket.escalation_info.escalation_type
                or latest.escalated_to != ticket.escalation_info.escalated_to
                or latest.jira_issue_id != ticket.escalation_info.jira_issue_id
            )
            if escalation_changed:
                orm_ticket.escalations.append(escalation_to_orm(ticket.escalation_info))

        self.session.commit()
        self.session.refresh(orm_ticket)
        return orm_to_ticket(orm_ticket)

    def list(self, limit: int = 50, offset: int = 0) -> List[Ticket]:
        orm_tickets = (
            self._query()
            .order_by(TicketORM.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [orm_to_ticket(t) for t in orm_tickets]
