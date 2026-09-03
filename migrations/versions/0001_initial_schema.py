"""initial schema: customers, tickets, ai_drafts, escalations

Hand-written to match backend/models/db/models.py exactly (see that module's
docstring for the enum/JSON/history-table design rationale). Not produced via
`alembic revision --autogenerate`, but structurally identical to what that
would generate from the current ORM models.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enum string values, kept in sync with backend/models/ticket.py.
# Migrations are intentionally decoupled from importing application code (so
# a later change to the enums in ticket.py doesn't retroactively change the
# meaning of this historical migration) -- if a new status/priority/workflow
# state value is ever added, write a follow-up migration that updates
# `_TICKET_STATUS`/etc. and the CHECK constraint here, mirroring the ORM.
_TICKET_STATUS = ("new", "in_progress", "awaiting_customer", "resolved", "escalated", "closed")
_TICKET_PRIORITY = ("low", "medium", "high", "critical")
_AI_WORKFLOW_STATE = (
    "classified",
    "knowledge_retrieved",
    "draft_generated",
    "confidence_evaluated",
    "awaiting_review",
    "reviewed",
    "escalated",
)


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("account_id", sa.String(length=64), nullable=True),
        sa.Column("crm_link", sa.String(length=512), nullable=True),
    )

    op.create_table(
        "tickets",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "customer_id",
            sa.String(length=64),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*_TICKET_STATUS, name="ticket_status", native_enum=False, length=32),
            nullable=False,
            server_default="new",
        ),
        sa.Column(
            "priority",
            sa.Enum(*_TICKET_PRIORITY, name="ticket_priority", native_enum=False, length=32),
            nullable=False,
            server_default="medium",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("assigned_to", sa.String(length=255), nullable=True),
        sa.Column("crm_ticket_id", sa.String(length=128), nullable=True),
        sa.Column("crm_system", sa.String(length=64), nullable=False, server_default="zoho"),
        sa.Column("crm_link", sa.String(length=512), nullable=True),
        sa.Column(
            "ai_workflow_state",
            sa.Enum(
                *_AI_WORKFLOW_STATE, name="ai_workflow_state", native_enum=False, length=32
            ),
            nullable=False,
            server_default="classified",
        ),
        sa.Column("ai_context", sa.JSON(), nullable=False),
        sa.Column("conversation_history", sa.JSON(), nullable=False),
        sa.UniqueConstraint("crm_system", "crm_ticket_id", name="uq_ticket_crm_mapping"),
    )
    op.create_index("ix_tickets_customer_id", "tickets", ["customer_id"])

    op.create_table(
        "ai_drafts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ticket_id", sa.String(length=64), sa.ForeignKey("tickets.id"), nullable=False
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("suggested_actions", sa.JSON(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("model_used", sa.String(length=128), nullable=False, server_default="gpt-4"),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ai_drafts_ticket_id", "ai_drafts", ["ticket_id"])

    op.create_table(
        "escalations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ticket_id", sa.String(length=64), sa.ForeignKey("tickets.id"), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("escalation_type", sa.String(length=64), nullable=False),
        sa.Column("escalated_to", sa.String(length=255), nullable=True),
        sa.Column("jira_issue_id", sa.String(length=128), nullable=True),
        sa.Column("jira_issue_key", sa.String(length=64), nullable=True),
        sa.Column("jira_url", sa.String(length=512), nullable=True),
        sa.Column("escalated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_escalations_ticket_id", "escalations", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_escalations_ticket_id", table_name="escalations")
    op.drop_table("escalations")

    op.drop_index("ix_ai_drafts_ticket_id", table_name="ai_drafts")
    op.drop_table("ai_drafts")

    op.drop_index("ix_tickets_customer_id", table_name="tickets")
    op.drop_table("tickets")

    op.drop_table("customers")
