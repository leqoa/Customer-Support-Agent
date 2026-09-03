"""Unit tests for backend.core.llm_service.LLMService

All HTTP calls are mocked - no real network requests and no real API keys
are used anywhere in this file. Every live-call code path (OpenAI, Anthropic,
retries, failures) is exercised purely through mocked `requests.post` and
mocked `time.sleep`.
"""
import requests
from unittest.mock import patch, MagicMock

import pytest

from backend.core.llm_service import LLMService
from backend.models.ticket import Ticket, CustomerInfo, AiDraft


# --- Helpers ----------------------------------------------------------------

def make_ticket() -> Ticket:
    customer = CustomerInfo(id="cust-1", name="Alice", email="alice@example.com")
    return Ticket(
        id="T-100",
        subject="Cannot log in to my account",
        description="I get an 'invalid credentials' error every time I try to log in.",
        customer=customer,
    )


def openai_response(text: str, prompt_tokens: int = 12, completion_tokens: int = 34) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
    return resp


def anthropic_response(text: str, input_tokens: int = 15, output_tokens: int = 40) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    return resp


DRAFT_TEXT = (
    "SUMMARY: Customer cannot log in due to invalid credentials.\n"
    "ACTIONS:\n"
    "- Ask the customer to reset their password\n"
    "- Verify their account is not locked\n"
    "RESPONSE:\n"
    "Hi Alice, I'm sorry you're having trouble logging in. Please try resetting "
    "your password using the link on the login page, and let us know if the "
    "issue persists."
)

CLASSIFICATION_TEXT_WELL_FORMED = (
    "CATEGORY: account\n"
    "CONFIDENCE: 0.87\n"
    "REASONING: The ticket mentions login and credential errors, which are account issues."
)

CLASSIFICATION_TEXT_MALFORMED = "I think this is probably about login stuff, not sure though."


# --- generate_draft ----------------------------------------------------------

class TestGenerateDraft:
    def test_openai_happy_path_returns_well_formed_draft(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        service = LLMService(provider="openai")
        ticket = make_ticket()

        with patch("backend.core.llm_service.requests.post", return_value=openai_response(DRAFT_TEXT)) as mock_post:
            draft = service.generate_draft(ticket, context={"classification": "account"}, knowledge={})

        assert mock_post.called
        assert isinstance(draft, AiDraft)
        assert draft.model_used == "gpt-4"
        assert "reset your password" in draft.content.lower() or "password" in draft.content.lower()
        assert draft.summary
        assert len(draft.suggested_actions) == 2
        assert "reset their password" in draft.suggested_actions[0].lower()

    def test_anthropic_happy_path_returns_well_formed_draft(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        service = LLMService(provider="anthropic")
        ticket = make_ticket()

        with patch("backend.core.llm_service.requests.post", return_value=anthropic_response(DRAFT_TEXT)) as mock_post:
            draft = service.generate_draft(ticket, context={"classification": "account"}, knowledge={})

        assert mock_post.called
        assert isinstance(draft, AiDraft)
        assert draft.model_used == "claude-sonnet-5"
        assert draft.summary
        assert len(draft.suggested_actions) == 2

    def test_missing_api_key_falls_back_to_mock(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        service = LLMService(provider="openai")
        ticket = make_ticket()

        with patch("backend.core.llm_service.requests.post") as mock_post:
            draft = service.generate_draft(ticket, context={}, knowledge={})

        mock_post.assert_not_called()
        assert isinstance(draft, AiDraft)
        assert draft.model_used == "mock-fallback"
        assert draft.content
        assert draft.suggested_actions

    def test_exhausted_retries_fall_back_to_mock(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        service = LLMService(provider="openai")
        ticket = make_ticket()

        with patch("backend.core.llm_service.requests.post", side_effect=requests.exceptions.ConnectionError("down")), \
             patch("backend.core.llm_service.time.sleep") as mock_sleep:
            draft = service.generate_draft(ticket, context={}, knowledge={})

        assert draft.model_used == "mock-fallback"
        assert mock_sleep.called  # backoff was attempted between retries


# --- classify_with_llm --------------------------------------------------------

class TestClassifyWithLlm:
    def test_parses_well_formed_response(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        service = LLMService(provider="openai")

        with patch(
            "backend.core.llm_service.requests.post",
            return_value=openai_response(CLASSIFICATION_TEXT_WELL_FORMED),
        ):
            result = service.classify_with_llm("Cannot log in", "Invalid credentials error")

        assert result["source"] == "llm"
        assert result["category"] == "account"
        assert result["confidence"] == pytest.approx(0.87)
        assert "login" in result["reasoning"].lower() or "credential" in result["reasoning"].lower()

    def test_falls_back_to_keyword_classifier_on_malformed_response(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        service = LLMService(provider="openai")

        with patch(
            "backend.core.llm_service.requests.post",
            return_value=openai_response(CLASSIFICATION_TEXT_MALFORMED),
        ):
            result = service.classify_with_llm(
                "Login broken", "I get an error and cannot access my account, password reset needed"
            )

        assert result["source"] == "keyword_fallback"
        assert result["fallback_reason"] == "unparseable_llm_response"
        assert "category" in result
        assert 0.0 <= result["confidence"] <= 1.0

    def test_falls_back_to_keyword_classifier_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        service = LLMService(provider="openai")

        with patch("backend.core.llm_service.requests.post") as mock_post:
            result = service.classify_with_llm("Billing issue", "I was charged twice for my subscription")

        mock_post.assert_not_called()
        assert result["source"] == "keyword_fallback"
        assert result["category"] == "billing"


# --- generate_escalation_summary ---------------------------------------------

class TestGenerateEscalationSummary:
    def test_happy_path_returns_llm_text(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        service = LLMService(provider="openai")
        ticket = make_ticket()

        summary_text = "Customer cannot log in; password reset was suggested but issue persists. Escalating for account team review."
        with patch("backend.core.llm_service.requests.post", return_value=openai_response(summary_text)):
            result = service.generate_escalation_summary(ticket)

        assert result == summary_text

    def test_missing_api_key_falls_back_to_mock_summary(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        service = LLMService(provider="openai")
        ticket = make_ticket()

        with patch("backend.core.llm_service.requests.post") as mock_post:
            result = service.generate_escalation_summary(ticket)

        mock_post.assert_not_called()
        assert "MOCK-FALLBACK" in result
        assert ticket.id in result


# --- retry behavior -----------------------------------------------------------

class TestRetryBehavior:
    def test_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        service = LLMService(provider="openai")

        success_resp = openai_response("SUMMARY: ok\nACTIONS:\n- a\nRESPONSE:\nAll good.")
        with patch(
            "backend.core.llm_service.requests.post",
            side_effect=[requests.exceptions.ConnectionError("transient"), success_resp],
        ) as mock_post, patch("backend.core.llm_service.time.sleep") as mock_sleep:
            text = service._call_llm("some prompt")

        assert mock_post.call_count == 2
        assert mock_sleep.call_count == 1
        assert "All good." in text

    def test_all_retries_fail_raises_llm_call_error(self, monkeypatch):
        from backend.core.llm_service import LLMCallError

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        service = LLMService(provider="openai")

        with patch(
            "backend.core.llm_service.requests.post",
            side_effect=requests.exceptions.ConnectionError("down"),
        ) as mock_post, patch("backend.core.llm_service.time.sleep") as mock_sleep:
            with pytest.raises(LLMCallError):
                service._call_llm("some prompt")

        assert mock_post.call_count == 3  # MAX_RETRIES
        assert mock_sleep.call_count == 2  # backoff between attempts 1-2 and 2-3


# --- usage tracking ------------------------------------------------------------

class TestUsageTracking:
    def test_usage_accumulates_across_calls(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        service = LLMService(provider="openai")

        assert service.usage == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

        resp1 = openai_response("first response", prompt_tokens=10, completion_tokens=20)
        resp2 = openai_response("second response", prompt_tokens=5, completion_tokens=15)

        with patch("backend.core.llm_service.requests.post", side_effect=[resp1, resp2]):
            service._call_llm("prompt one")
            service._call_llm("prompt two")

        assert service.usage["prompt_tokens"] == 15
        assert service.usage["completion_tokens"] == 35
        assert service.usage["estimated_cost_usd"] > 0.0

    def test_usage_falls_back_to_approximation_when_missing(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
        service = LLMService(provider="openai")

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"choices": [{"message": {"content": "hello world"}}]}  # no usage field

        with patch("backend.core.llm_service.requests.post", return_value=resp):
            service._call_llm("a prompt of some length")

        assert service.usage["prompt_tokens"] > 0
        assert service.usage["completion_tokens"] > 0


# --- constructor / provider selection ------------------------------------------

class TestProviderSelection:
    def test_defaults_to_openai_when_unset(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        service = LLMService()
        assert service.provider == "openai"
        assert service.model == "gpt-4"

    def test_reads_provider_from_env_var(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        service = LLMService()
        assert service.provider == "anthropic"
        assert service.model == "claude-sonnet-5"

    def test_explicit_args_override_env(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        service = LLMService(provider="openai", model="gpt-4-turbo")
        assert service.provider == "openai"
        assert service.model == "gpt-4-turbo"

    def test_unsupported_provider_falls_back_to_openai(self):
        service = LLMService(provider="not-a-real-provider")
        assert service.provider == "openai"
