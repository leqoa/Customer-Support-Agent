"""Unit tests for the SQLAlchemy ORM layer and dataclass<->ORM converters.

Only an in-memory SQLite database is used here -- no external Postgres
server is touched. See the PR description for what to double-check against a
real Postgres instance before relying on this in production.
"""
import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.db.base import Base
from backend.models.db.converters import orm_to_ticket, ticket_to_orm
from backend.models.db.models import TicketORM
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


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture()
def session(engine):
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    yield db
    db.close()


def _sample_ticket(ticket_id="ticket-1", customer_id="cust-1", crm_ticket_id="ZOHO-999") -> Ticket:
    customer = CustomerInfo(
        id=customer_id,
        name="Ada Lovelace",
        email="ada@example.com",
        phone="555-0100",
        account_id="acct-1",
        crm_link="https://crm.example.com/customers/cust-1",
    )
    context = TicketContext(
        classification="billing",
        retrieved_knowledge=[{"doc_id": "kb-1", "title": "Refund policy"}],
        related_tickets=["ticket-0"],
        tags=["vip"],
        custom_fields={"region": "us-east"},
    )
    draft = AiDraft(
        content="Here's how to resolve your refund issue...",
        summary="Billing issue resolved via refund",
        suggested_actions=["issue_refund", "close_ticket"],
        confidence_score=0.92,
        model_used="gpt-4",
        generated_at=datetime.datetime(2026, 1, 1, 12, 0, 0),
    )
    escalation = EscalationInfo(
        reason="Refund exceeds agent authority",
        escalation_type="billing",
        escalated_to="finance-team",
        jira_issue_id="10001",
        jira_issue_key="ITSS-1",
        jira_url="https://jira.example.com/ITSS-1",
        escalated_at=datetime.datetime(2026, 1, 1, 12, 5, 0),
    )
    return Ticket(
        id=ticket_id,
        subject="Refund not processed",
        description="Customer says the refund never arrived.",
        customer=customer,
        status=TicketStatus.ESCALATED,
        priority=TicketPriority.HIGH,
        created_at=datetime.datetime(2026, 1, 1, 11, 0, 0),
        updated_at=datetime.datetime(2026, 1, 1, 12, 5, 0),
        assigned_to="agent-42",
        crm_ticket_id=crm_ticket_id,
        crm_system="zoho",
        crm_link="https://crm.example.com/tickets/ZOHO-999",
        ai_workflow_state=AiWorkflowState.ESCALATED,
        ai_context=context,
        ai_draft=draft,
        escalation_info=escalation,
        conversation_history=[
            {
                "role": "customer",
                "content": "Where's my refund?",
                "timestamp": "2026-01-01T11:00:00",
            }
        ],
    )


def test_roundtrip_without_db():
    """ticket_to_orm -> orm_to_ticket preserves data with no DB involved."""
    ticket = _sample_ticket()

    orm_ticket = ticket_to_orm(ticket)
    result = orm_to_ticket(orm_ticket)

    assert result == ticket


def test_roundtrip_through_persisted_row(session):
    """Same round trip, but through an actual insert + fetch."""
    ticket = _sample_ticket()

    session.add(ticket_to_orm(ticket))
    session.commit()

    fetched = session.query(TicketORM).filter_by(id="ticket-1").one()
    result = orm_to_ticket(fetched)

    assert result == ticket


def test_roundtrip_with_no_optional_fields(session):
    """A minimal ticket (no draft, no escalation) round-trips too."""
    ticket = Ticket(
        id="ticket-min",
        subject="Simple question",
        description="How do I reset my password?",
        customer=CustomerInfo(id="cust-min", name="Bob", email="bob@example.com"),
    )

    session.add(ticket_to_orm(ticket))
    session.commit()

    fetched = session.query(TicketORM).filter_by(id="ticket-min").one()
    result = orm_to_ticket(fetched)

    assert result == ticket
    assert result.ai_draft is None
    assert result.escalation_info is None


def test_ai_draft_history_keeps_all_rows_latest_wins(session):
    """Multiple AiDraft rows for one ticket: orm_to_ticket surfaces the newest."""
    ticket = _sample_ticket()
    orm_ticket = ticket_to_orm(ticket)
    session.add(orm_ticket)
    session.commit()

    from backend.models.db.converters import ai_draft_to_orm

    newer_draft = AiDraft(
        content="Escalating further, refund approved.",
        summary="Refund approved",
        suggested_actions=["close_ticket"],
        confidence_score=0.97,
        generated_at=datetime.datetime(2026, 1, 2, 9, 0, 0),
    )
    orm_ticket.ai_drafts.append(ai_draft_to_orm(newer_draft, ticket_id=orm_ticket.id))
    session.commit()

    fetched = session.query(TicketORM).filter_by(id="ticket-1").one()
    assert len(fetched.ai_drafts) == 2

    result = orm_to_ticket(fetched)
    assert result.ai_draft.content == "Escalating further, refund approved."
    assert result.ai_draft.confidence_score == 0.97


def test_unique_constraint_on_crm_mapping(session):
    t1 = _sample_ticket(ticket_id="ticket-1", customer_id="cust-1", crm_ticket_id="CRM-DUP")
    t2 = _sample_ticket(ticket_id="ticket-2", customer_id="cust-2", crm_ticket_id="CRM-DUP")

    session.add(ticket_to_orm(t1))
    session.commit()

    session.add(ticket_to_orm(t2))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_crm_ticket_id_allowed_under_different_crm_system(session):
    """The unique constraint is on the (crm_system, crm_ticket_id) pair."""
    t1 = _sample_ticket(ticket_id="ticket-1", customer_id="cust-1", crm_ticket_id="SAME-ID")
    t2 = _sample_ticket(ticket_id="ticket-2", customer_id="cust-2", crm_ticket_id="SAME-ID")
    t2.crm_system = "zendesk"

    session.add(ticket_to_orm(t1))
    session.commit()
    session.add(ticket_to_orm(t2))
    session.commit()  # should not raise

    assert session.query(TicketORM).count() == 2
