"""SQLAlchemy ORM models mirroring the dataclasses in backend.models.ticket.

Design notes
------------
- Enum-valued columns (``status``, ``priority``, ``ai_workflow_state``) reuse
  the exact ``TicketStatus`` / ``TicketPriority`` / ``AiWorkflowState`` enums
  defined in ``backend.models.ticket`` -- imported directly rather than
  redefined, so there's one source of truth. They're stored via
  ``sqlalchemy.Enum(..., native_enum=False, values_callable=...)``, which
  persists as a plain ``VARCHAR`` with a CHECK constraint instead of a native
  Postgres ``ENUM`` type. That keeps the column portable across
  SQLite/Postgres and avoids the ``ALTER TYPE ... ADD VALUE`` migration dance
  a native Postgres enum requires whenever a new status/priority is added.
  ``values_callable`` makes the stored/round-tripped string be the enum's
  ``.value`` (e.g. ``"in_progress"``) rather than SQLAlchemy's default of the
  member ``.name`` (e.g. ``"IN_PROGRESS"``), matching what
  ``Ticket.to_dict()`` already produces elsewhere in the codebase.
- ``AiDraftORM`` and ``EscalationInfoORM`` are modeled as *history* tables:
  many rows per ticket over time. The dataclasses only expose a single
  "current" draft/escalation, so the converters in ``converters.py`` treat
  the most recent row (by timestamp) as that current value. This matches the
  Phase 2 spec's explicit call to persist ``AiDraft`` history, and the same
  reasoning applies to escalations (a ticket can be escalated more than once
  over its life).
- ``TicketORM.crm_system`` + ``TicketORM.crm_ticket_id`` carry a unique
  constraint together, so a given CRM ticket can only ever map to one local
  ticket (the "ticket mapping (crm_id <-> local ID)" requirement).
- ``TicketContext`` and ``conversation_history`` are stored as JSON blobs.
  Their sub-fields (``retrieved_knowledge``, ``related_tickets``, ``tags``,
  ``custom_fields``, and the message list) are inherently semi-structured
  (list-of-dict / free-form dict shaped), so normalizing them into further
  tables would add complexity without a real query benefit here; JSON keeps
  a lossless 1:1 mapping to the dataclass.
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.models.db.base import Base
from backend.models.ticket import AiWorkflowState, TicketPriority, TicketStatus


def _enum_column(enum_cls, **kwargs):
    """A portable VARCHAR+CHECK column backed by a (str, Enum) class.

    Stores/reads the member's `.value` (not `.name`) so the DB representation
    matches the enum's own string values one-to-one.
    """
    return Column(
        Enum(
            enum_cls,
            values_callable=lambda cls: [member.value for member in cls],
            native_enum=False,
            length=32,
        ),
        **kwargs,
    )


class CustomerORM(Base):
    """Mirrors `backend.models.ticket.CustomerInfo`."""

    __tablename__ = "customers"

    id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    phone = Column(String(64), nullable=True)
    account_id = Column(String(64), nullable=True)
    crm_link = Column(String(512), nullable=True)

    tickets = relationship("TicketORM", back_populates="customer")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<CustomerORM id={self.id!r} email={self.email!r}>"


class TicketORM(Base):
    """Mirrors `backend.models.ticket.Ticket`."""

    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("crm_system", "crm_ticket_id", name="uq_ticket_crm_mapping"),
    )

    id = Column(String(64), primary_key=True)
    subject = Column(String(512), nullable=False)
    description = Column(Text, nullable=False)

    customer_id = Column(String(64), ForeignKey("customers.id"), nullable=False)

    status = _enum_column(TicketStatus, nullable=False, default=TicketStatus.NEW)
    priority = _enum_column(TicketPriority, nullable=False, default=TicketPriority.MEDIUM)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    assigned_to = Column(String(255), nullable=True)

    # CRM integration / ticket mapping (crm_id <-> local id).
    crm_ticket_id = Column(String(128), nullable=True)
    crm_system = Column(String(64), nullable=False, default="zoho")
    crm_link = Column(String(512), nullable=True)

    # AI workflow.
    ai_workflow_state = _enum_column(
        AiWorkflowState, nullable=False, default=AiWorkflowState.CLASSIFIED
    )
    ai_context = Column(JSON, nullable=False, default=dict)

    # Conversation history: list[{"role": ..., "content": ..., "timestamp": ...}]
    conversation_history = Column(JSON, nullable=False, default=list)

    customer = relationship("CustomerORM", back_populates="tickets")
    ai_drafts = relationship(
        "AiDraftORM",
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="AiDraftORM.generated_at",
    )
    escalations = relationship(
        "EscalationInfoORM",
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="EscalationInfoORM.escalated_at",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<TicketORM id={self.id!r} status={self.status!r}>"


class AiDraftORM(Base):
    """Mirrors `backend.models.ticket.AiDraft`.

    History table: every AI draft ever generated for a ticket is a row here;
    the newest (by `generated_at`) is treated as the ticket's current draft.
    """

    __tablename__ = "ai_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String(64), ForeignKey("tickets.id"), nullable=False)

    content = Column(Text, nullable=False)
    summary = Column(Text, nullable=False)
    suggested_actions = Column(JSON, nullable=False, default=list)
    confidence_score = Column(Float, nullable=False, default=0.0)
    model_used = Column(String(128), nullable=False, default="gpt-4")
    generated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    ticket = relationship("TicketORM", back_populates="ai_drafts")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<AiDraftORM id={self.id} ticket_id={self.ticket_id!r}>"


class EscalationInfoORM(Base):
    """Mirrors `backend.models.ticket.EscalationInfo`.

    History table: every escalation event for a ticket is a row here; the
    newest (by `escalated_at`) is treated as the ticket's current escalation.
    """

    __tablename__ = "escalations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String(64), ForeignKey("tickets.id"), nullable=False)

    reason = Column(Text, nullable=False)
    escalation_type = Column(String(64), nullable=False)
    escalated_to = Column(String(255), nullable=True)
    jira_issue_id = Column(String(128), nullable=True)
    jira_issue_key = Column(String(64), nullable=True)
    jira_url = Column(String(512), nullable=True)
    escalated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    ticket = relationship("TicketORM", back_populates="escalations")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<EscalationInfoORM id={self.id} ticket_id={self.ticket_id!r}>"
