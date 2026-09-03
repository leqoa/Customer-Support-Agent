"""In-process ASGI tests for the REST API layer, via FastAPI's TestClient.

No real network calls are made, and these tests do not depend on any other
parallel PR's code (Zoho/Jira integrations, a real DB layer, etc.) -- the
ticket store is overridden per-test with a fresh in-memory ``TicketStore``.
"""
import pytest
from fastapi.testclient import TestClient

from backend.api.dependencies import TicketStore, get_ticket_store
from backend.api.main import app
from backend.models.ticket import CustomerInfo, Ticket


@pytest.fixture()
def store() -> TicketStore:
    return TicketStore()


@pytest.fixture()
def client(store: TicketStore):
    app.dependency_overrides[get_ticket_store] = lambda: store
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _ticket_payload(**overrides):
    payload = {
        "subject": "Cannot log in",
        "description": "I get an error when I try to log in to my account.",
        "customer": {
            "id": "cust-1",
            "name": "Jane Doe",
            "email": "jane@example.com",
        },
    }
    payload.update(overrides)
    return payload


def _create_ticket(client) -> dict:
    resp = client.post("/tickets", json=_ticket_payload())
    assert resp.status_code == 201
    return resp.json()["ticket"]


# ---------------------------------------------------------------------------
# POST /tickets
# ---------------------------------------------------------------------------

def test_create_ticket_runs_workflow_and_returns_ticket(client):
    resp = client.post("/tickets", json=_ticket_payload())
    assert resp.status_code == 201
    body = resp.json()

    ticket = body["ticket"]
    assert ticket["subject"] == "Cannot log in"
    assert ticket["status"] == "new"
    assert ticket["customer"]["email"] == "jane@example.com"
    # workflow ran synchronously and produced a draft
    assert ticket["ai_draft"] is not None
    assert ticket["ai_workflow_state"] == "awaiting_review"

    workflow = body["workflow"]
    assert workflow["ticket_id"] == ticket["id"]
    assert workflow["steps_executed"] == [
        "classify",
        "retrieve_knowledge",
        "generate_draft",
        "evaluate_confidence",
        "route_for_review",
    ]
    assert workflow["errors"] == []


def test_create_ticket_rejects_invalid_payload(client):
    resp = client.post("/tickets", json={"subject": "no description or customer"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /tickets/{id}
# ---------------------------------------------------------------------------

def test_get_ticket_success(client):
    created = _create_ticket(client)
    resp = client.get(f"/tickets/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_ticket_not_found(client):
    resp = client.get("/tickets/does-not-exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /tickets/{id}
# ---------------------------------------------------------------------------

def test_update_ticket_success(client):
    created = _create_ticket(client)
    resp = client.put(
        f"/tickets/{created['id']}",
        json={
            "status": "in_progress",
            "assigned_to": "agent-42",
            "add_message": {"role": "agent", "content": "Looking into this."},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["assigned_to"] == "agent-42"
    assert body["conversation_history"][-1]["content"] == "Looking into this."


def test_update_ticket_not_found(client):
    resp = client.put("/tickets/does-not-exist", json={"status": "in_progress"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /tickets/{id}/escalate
# ---------------------------------------------------------------------------

def test_escalate_ticket_success(client):
    created = _create_ticket(client)
    resp = client.post(
        f"/tickets/{created['id']}/escalate",
        json={"reason": "Customer is VIP and angry", "escalation_type": "vip"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "escalated"
    assert body["ai_workflow_state"] == "escalated"
    assert body["escalation_info"]["reason"] == "Customer is VIP and angry"
    assert body["escalation_info"]["escalation_type"] == "vip"
    # Jira integration is separate parallel work -- no jira fields populated
    assert body["escalation_info"]["jira_issue_id"] is None


def test_escalate_ticket_not_found(client):
    resp = client.post(
        "/tickets/does-not-exist/escalate",
        json={"reason": "x", "escalation_type": "technical"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /workflows/{id}/status
# ---------------------------------------------------------------------------

def test_workflow_status_success(client):
    created = _create_ticket(client)
    resp = client.get(f"/workflows/{created['id']}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow_id"] == created["id"]
    assert body["ticket_id"] == created["id"]
    assert body["ai_workflow_state"] == "awaiting_review"


def test_workflow_status_not_found(client):
    resp = client.get("/workflows/does-not-exist/status")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /drafts/{id}/approve, /drafts/{id}/reject
# ---------------------------------------------------------------------------

def test_approve_draft_success(client):
    created = _create_ticket(client)
    resp = client.post(
        f"/drafts/{created['id']}/approve",
        json={"reviewer": "agent-7", "notes": "Looks good"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "approved"
    assert body["ai_workflow_state"] == "reviewed"
    assert body["ticket_status"] == "resolved"
    assert body["draft"] is not None


def test_reject_draft_success(client):
    created = _create_ticket(client)
    resp = client.post(
        f"/drafts/{created['id']}/reject",
        json={"reviewer": "agent-7", "notes": "Missing key troubleshooting step"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "rejected"
    assert body["ai_workflow_state"] == "reviewed"
    assert body["ticket_status"] == "in_progress"


def test_approve_draft_ticket_not_found(client):
    resp = client.post("/drafts/does-not-exist/approve", json={})
    assert resp.status_code == 404


def test_approve_draft_no_draft_yet(client, store: TicketStore):
    # A ticket that was never run through the workflow has no ai_draft.
    bare_ticket = Ticket(
        id="bare-1",
        subject="No workflow run",
        description="Inserted directly into the store.",
        customer=CustomerInfo(id="c1", name="Bob", email="bob@example.com"),
    )
    bare_ticket.ai_draft = None
    store.save(bare_ticket)

    resp = client.post("/drafts/bare-1/approve", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# API key auth (only enforced once API_KEY is set server-side)
# ---------------------------------------------------------------------------

def test_requests_allowed_when_api_key_not_configured(client, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    resp = client.post("/tickets", json=_ticket_payload())
    assert resp.status_code == 201


def test_requests_rejected_without_key_when_api_key_configured(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "super-secret")
    resp = client.post("/tickets", json=_ticket_payload())
    assert resp.status_code == 401


def test_requests_allowed_with_correct_key_when_api_key_configured(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "super-secret")
    resp = client.post(
        "/tickets",
        json=_ticket_payload(),
        headers={"X-API-Key": "super-secret"},
    )
    assert resp.status_code == 201
