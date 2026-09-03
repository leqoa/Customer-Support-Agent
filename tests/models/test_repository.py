"""Tests for TicketRepository against an in-memory SQLite database.

No external database server is required or used; see the PR description for
Postgres-specific considerations to verify separately.
"""
import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.db.base import Base
from backend.models.db.models import AiDraftORM, EscalationInfoORM
from backend.models.db.repository import TicketRepository
from backend.models.ticket import (
    AiDraft,
    AiWorkflowState,
    CustomerInfo,
    EscalationInfo,
    Ticket,
    TicketPriority,
    TicketStatus,
)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    yield db
    db.close()


@pytest.fixture()
def repo(session):
    return TicketRepository(session)


def _ticket(ticket_id="t-1", customer_id="c-1", crm_ticket_id="CRM-1", crm_system="zoho"):
    return Ticket(
        id=ticket_id,
        subject="Can't log in",
        description="User reports login failures since this morning.",
        customer=CustomerInfo(id=customer_id, name="Grace Hopper", email="grace@example.com"),
        status=TicketStatus.NEW,
        priority=TicketPriority.MEDIUM,
        crm_ticket_id=crm_ticket_id,
        crm_system=crm_system,
    )


def test_create_and_get_by_id(repo):
    created = repo.create(_ticket())

    fetched = repo.get_by_id("t-1")
    assert fetched is not None
    assert fetched.id == "t-1"
    assert fetched.subject == "Can't log in"
    assert fetched.customer.email == "grace@example.com"
    assert created == fetched


def test_get_by_id_missing_returns_none(repo):
    assert repo.get_by_id("does-not-exist") is None


def test_create_reuses_existing_customer_row(repo, session):
    from backend.models.db.models import CustomerORM

    repo.create(_ticket(ticket_id="t-1", customer_id="shared-customer"))
    repo.create(_ticket(ticket_id="t-2", customer_id="shared-customer", crm_ticket_id="CRM-2"))

    assert session.query(CustomerORM).filter_by(id="shared-customer").count() == 1


def test_get_by_crm_id(repo):
    repo.create(_ticket(ticket_id="t-1", crm_ticket_id="CRM-1", crm_system="zoho"))

    found = repo.get_by_crm_id("zoho", "CRM-1")
    assert found is not None
    assert found.id == "t-1"

    assert repo.get_by_crm_id("zendesk", "CRM-1") is None
    assert repo.get_by_crm_id("zoho", "nope") is None


def test_update_scalar_fields(repo):
    ticket = repo.create(_ticket())

    ticket.status = TicketStatus.IN_PROGRESS
    ticket.priority = TicketPriority.HIGH
    ticket.assigned_to = "agent-7"
    updated = repo.update(ticket)

    assert updated.status == TicketStatus.IN_PROGRESS
    assert updated.priority == TicketPriority.HIGH
    assert updated.assigned_to == "agent-7"

    refetched = repo.get_by_id("t-1")
    assert refetched.status == TicketStatus.IN_PROGRESS


def test_update_missing_ticket_raises(repo):
    with pytest.raises(ValueError):
        repo.update(_ticket(ticket_id="ghost"))


def test_update_appends_draft_history_only_when_changed(repo):
    ticket = repo.create(_ticket())

    ticket.ai_workflow_state = AiWorkflowState.DRAFT_GENERATED
    ticket.ai_draft = AiDraft(
        content="Try resetting your password.",
        summary="Password reset suggested",
        suggested_actions=["send_reset_link"],
        confidence_score=0.8,
    )
    updated = repo.update(ticket)
    assert updated.ai_draft.content == "Try resetting your password."
    assert repo.session.query(AiDraftORM).filter_by(ticket_id="t-1").count() == 1

    # Re-submitting the same (unchanged) draft must not create a duplicate
    # history row.
    repo.update(updated)
    assert repo.session.query(AiDraftORM).filter_by(ticket_id="t-1").count() == 1

    # A genuinely different draft appends a second history row.
    updated.ai_draft = AiDraft(
        content="Escalating to tier 2.",
        summary="Tier 2 escalation",
        suggested_actions=["escalate"],
        confidence_score=0.4,
    )
    repo.update(updated)
    assert repo.session.query(AiDraftORM).filter_by(ticket_id="t-1").count() == 2

    # And the ticket's current draft is the latest one.
    final = repo.get_by_id("t-1")
    assert final.ai_draft.content == "Escalating to tier 2."


def test_update_appends_escalation_history(repo):
    ticket = repo.create(_ticket())
    ticket.escalation_info = EscalationInfo(reason="VIP customer", escalation_type="vip")
    repo.update(ticket)

    assert repo.session.query(EscalationInfoORM).filter_by(ticket_id="t-1").count() == 1

    fetched = repo.get_by_id("t-1")
    assert fetched.escalation_info.reason == "VIP customer"


def test_list_returns_tickets_ordered_newest_first(repo):
    t1 = _ticket(ticket_id="t-1", customer_id="c-1", crm_ticket_id="CRM-1")
    t1.created_at = datetime.datetime(2026, 1, 1)
    t2 = _ticket(ticket_id="t-2", customer_id="c-2", crm_ticket_id="CRM-2")
    t2.created_at = datetime.datetime(2026, 1, 2)

    repo.create(t1)
    repo.create(t2)

    results = repo.list()
    assert [t.id for t in results] == ["t-2", "t-1"]


def test_list_respects_limit_and_offset(repo):
    for i in range(5):
        t = _ticket(ticket_id=f"t-{i}", customer_id=f"c-{i}", crm_ticket_id=f"CRM-{i}")
        t.created_at = datetime.datetime(2026, 1, 1) + datetime.timedelta(minutes=i)
        repo.create(t)

    page = repo.list(limit=2, offset=1)
    assert [t.id for t in page] == ["t-3", "t-2"]


def test_unique_constraint_on_crm_mapping_via_repository(repo):
    repo.create(_ticket(ticket_id="t-1", customer_id="c-1", crm_ticket_id="CRM-DUP", crm_system="zoho"))

    with pytest.raises(IntegrityError):
        repo.create(
            _ticket(ticket_id="t-2", customer_id="c-2", crm_ticket_id="CRM-DUP", crm_system="zoho")
        )
