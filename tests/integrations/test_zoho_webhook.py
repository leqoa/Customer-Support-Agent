"""Unit tests for the Zoho webhook listener (backend/integrations/zoho_webhook.py).

Pure in-process ASGI testing via FastAPI's TestClient -- no real network calls.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.integrations import zoho_webhook

TEST_SECRET = "test-shared-secret"

VALID_PAYLOAD = {
    "id": "123456789",
    "Subject": "Cannot log in to portal",
    "Description": "Customer reports login failures since this morning.",
    "Priority": "High",
    "Status": "New",
    "Email": "customer@example.com",
    "Phone": "+1-555-0100",
    "ticket_url": "https://crm.zoho.com/tickets/123456789",
    "Contact_Name": {"id": "c-1", "name": "Jane Doe"},
    "Account_Name": {"id": "a-1", "name": "Acme Inc."},
}


@pytest.fixture()
def app_client():
    app = FastAPI()
    app.include_router(zoho_webhook.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_webhook_state(monkeypatch):
    """Ensure each test starts with a clean idempotency cache and known secret."""
    zoho_webhook._seen_record_ids.clear()
    monkeypatch.setattr(zoho_webhook, "ZOHO_WEBHOOK_SECRET", TEST_SECRET)
    yield
    zoho_webhook._seen_record_ids.clear()


def test_valid_payload_with_correct_secret_returns_202(app_client):
    resp = app_client.post(
        "/webhooks/zoho/tickets",
        json=VALID_PAYLOAD,
        headers={"X-Zoho-Webhook-Token": TEST_SECRET},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body == {"status": "accepted", "ticket_id": "zoho-123456789"}


def test_missing_token_returns_401(app_client):
    resp = app_client.post("/webhooks/zoho/tickets", json=VALID_PAYLOAD)
    assert resp.status_code == 401


def test_wrong_token_returns_401(app_client):
    resp = app_client.post(
        "/webhooks/zoho/tickets",
        json=VALID_PAYLOAD,
        headers={"X-Zoho-Webhook-Token": "not-the-secret"},
    )
    assert resp.status_code == 401


def test_server_secret_not_configured_returns_503(app_client, monkeypatch):
    monkeypatch.setattr(zoho_webhook, "ZOHO_WEBHOOK_SECRET", None)
    resp = app_client.post(
        "/webhooks/zoho/tickets",
        json=VALID_PAYLOAD,
        headers={"X-Zoho-Webhook-Token": TEST_SECRET},
    )
    assert resp.status_code == 503


def test_duplicate_record_id_within_window_is_ignored(app_client):
    headers = {"X-Zoho-Webhook-Token": TEST_SECRET}

    first = app_client.post("/webhooks/zoho/tickets", json=VALID_PAYLOAD, headers=headers)
    assert first.status_code == 202
    assert first.json()["status"] == "accepted"

    second = app_client.post("/webhooks/zoho/tickets", json=VALID_PAYLOAD, headers=headers)
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate_ignored", "ticket_id": "zoho-123456789"}


def test_malformed_payload_returns_422(app_client):
    # Contact_Name is expected to be an object (or absent); sending a plain
    # string for it should fail Pydantic validation.
    bad_payload = dict(VALID_PAYLOAD)
    bad_payload["Contact_Name"] = "this-should-be-an-object-not-a-string"

    resp = app_client.post(
        "/webhooks/zoho/tickets",
        json=bad_payload,
        headers={"X-Zoho-Webhook-Token": TEST_SECRET},
    )
    assert resp.status_code == 422


def test_query_param_token_also_accepted(app_client):
    resp = app_client.post(
        "/webhooks/zoho/tickets?token=" + TEST_SECRET,
        json=VALID_PAYLOAD,
    )
    assert resp.status_code == 202
