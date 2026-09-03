"""Tests for AgentWorkflow's Phase 2 wiring.

These cover two things:
  1. That the workflow runs correctly end-to-end using real services when
     they're available (mocked here, since we have no real LLM/Jira
     credentials).
  2. That it degrades gracefully when LLMService/JiraSync are unavailable
     (module missing) or, as is actually exercised below with no mocking
     at all, available but unconfigured (no API keys / Jira credentials
     in this environment) -- both cases must produce the same safe
     placeholder/local-only behavior.
"""
from unittest.mock import Mock

import pytest

from backend.core.agent_workflow import AgentWorkflow, LLMService, JiraSync
from backend.models.ticket import (
    CustomerInfo,
    Ticket,
    TicketPriority,
    TicketStatus,
)


def make_ticket(ticket_id="t-1", subject="App crashes on login", description="The app crashes with an error every time I try to log in."):
    customer = CustomerInfo(id="c-1", name="Jane Doe", email="jane@example.com")
    return Ticket(id=ticket_id, subject=subject, description=description, customer=customer)


class TestRealDependencyAvailability:
    def test_llm_service_and_jira_sync_are_available(self):
        """
        LLMService (#9) and JiraSync (#7) have both merged, so
        agent_workflow.py's optional imports now resolve to the real
        classes. The rest of this file (below) still exercises the
        fallback behavior -- not because these modules are missing, but
        because neither has real credentials configured in this test
        environment, which is the more common real-world case anyway.
        """
        assert LLMService is not None
        assert JiraSync is not None


class TestWorkflowWithUnconfiguredIntegrations:
    """
    These tests use a real, unmodified AgentWorkflow() as constructed in
    *this* checkout. LLMService and JiraSync are real (not None) here, but
    neither has credentials configured in this environment (no
    OPENAI_API_KEY/ANTHROPIC_API_KEY, no JIRA_BASE_URL/EMAIL/API_TOKEN) --
    so this exercises each service's own real, unmocked graceful-fallback
    behavior end-to-end, together with the real (merged) KnowledgeRetriever
    with no MCP registry configured.
    """

    def test_full_workflow_completes_with_no_errors(self):
        workflow = AgentWorkflow()
        assert workflow.llm_service is not None
        assert workflow.jira_sync is not None

        ticket = make_ticket()
        result = workflow.execute_workflow(ticket)

        assert result["errors"] == []
        assert result["steps_executed"] == [
            "classify", "retrieve_knowledge", "generate_draft", "evaluate_confidence", "route_for_review"
        ]
        assert ticket.ai_draft is not None
        # No OPENAI_API_KEY/ANTHROPIC_API_KEY configured -> LLMService's own
        # mock-fallback response, per its documented model_used tag.
        assert ticket.ai_draft.model_used == "mock-fallback"

    def test_knowledge_retrieval_returns_real_shape_via_local_fallback(self):
        workflow = AgentWorkflow()
        ticket = make_ticket()
        classification = workflow._classify_ticket(ticket)

        knowledge = workflow._retrieve_knowledge(ticket, classification)

        assert set(knowledge.keys()) == {"relevant_docs", "similar_tickets", "faq_items"}
        # FAQ fallback content should be present and clearly labeled, since
        # KnowledgeRetriever has no MCP registry to call here.
        assert all(item.get("source") == "builtin_fallback" for item in knowledge["faq_items"])

    def test_low_confidence_escalates_ticket_locally_without_jira(self):
        workflow = AgentWorkflow()
        ticket = make_ticket()
        workflow.confidence_evaluator.evaluate_draft = Mock(
            return_value={"overall_confidence": 0.1, "component_scores": {}, "reasoning": ["Low confidence"]}
        )

        result = workflow.execute_workflow(ticket)

        assert result["errors"] == []
        assert ticket.status == TicketStatus.ESCALATED
        assert ticket.escalation_info is not None
        assert ticket.escalation_info.jira_issue_key is None
        assert ticket.escalation_info.jira_url is None
        assert "AI-Escalated" in ticket.ai_context.tags
        assert result["routing_decision"]["jira_issue_key"] is None


class TestClassificationWithMockedLLM:
    def test_uses_llm_classification_when_available(self):
        workflow = AgentWorkflow()
        workflow.llm_service = Mock()
        workflow.llm_service.classify_with_llm.return_value = {
            "category": "billing", "confidence": 0.92, "reasoning": "mentions invoice", "source": "llm"
        }

        ticket = make_ticket(subject="Invoice question", description="Why was I charged twice?")
        extracted = workflow._classify_ticket(ticket)

        assert extracted["classification"]["category"] == "billing"
        assert extracted["classification"]["confidence"] == 0.92
        assert extracted["classification"]["source"] == "llm"
        assert ticket.ai_context.classification == "billing"

    def test_falls_back_to_keyword_classifier_when_llm_raises(self):
        workflow = AgentWorkflow()
        workflow.llm_service = Mock()
        workflow.llm_service.classify_with_llm.side_effect = RuntimeError("LLM API down")

        ticket = make_ticket(subject="Cannot log in", description="Password reset isn't working, account access issue")
        extracted = workflow._classify_ticket(ticket)

        # Falls back to the keyword-based result; account-related keywords
        # should win here regardless of the LLM failure.
        assert extracted["classification"]["category"] == "account"
        assert ticket.ai_context.classification == "account"


class TestDraftGenerationWithMockedLLM:
    def test_uses_llm_draft_when_available(self):
        from backend.models.ticket import AiDraft

        workflow = AgentWorkflow()
        fake_draft = AiDraft(content="Real LLM draft", summary="summary", suggested_actions=["a"], model_used="claude-sonnet-5")
        workflow.llm_service = Mock()
        workflow.llm_service.generate_draft.return_value = fake_draft

        ticket = make_ticket()
        draft = workflow._generate_draft(ticket, {"relevant_docs": [], "similar_tickets": [], "faq_items": []})

        assert draft is fake_draft
        assert ticket.ai_draft is fake_draft
        workflow.llm_service.generate_draft.assert_called_once()

    def test_falls_back_to_placeholder_when_llm_raises(self):
        workflow = AgentWorkflow()
        workflow.llm_service = Mock()
        workflow.llm_service.generate_draft.side_effect = RuntimeError("LLM API down")

        ticket = make_ticket()
        draft = workflow._generate_draft(ticket, {"relevant_docs": [], "similar_tickets": [], "faq_items": []})

        assert draft.model_used == "gpt-4"  # the placeholder's tag
        assert draft.content == "Draft response based on ticket analysis and knowledge base."


class TestEscalationWithMockedJira:
    def test_successful_jira_creation_populates_escalation_info(self):
        workflow = AgentWorkflow()
        workflow.jira_sync = Mock()
        workflow.jira_sync.create_escalation_issue.return_value = {
            "id": "10001", "key": "ITSS-42", "url": "https://example.atlassian.net/browse/ITSS-42"
        }

        ticket = make_ticket()
        confidence_eval = {"overall_confidence": 0.2, "reasoning": ["Low confidence - escalation recommended"]}
        routing_decision = {"action": "escalate", "reason": "Low confidence - requires human expertise", "escalation_required": True}

        workflow._create_escalation(ticket, confidence_eval, routing_decision)

        assert ticket.status == TicketStatus.ESCALATED
        assert ticket.escalation_info.jira_issue_key == "ITSS-42"
        assert ticket.escalation_info.jira_url == "https://example.atlassian.net/browse/ITSS-42"
        assert routing_decision["jira_issue_key"] == "ITSS-42"
        workflow.jira_sync.create_escalation_issue.assert_called_once()

    def test_jira_failure_still_escalates_ticket_locally(self):
        workflow = AgentWorkflow()
        workflow.jira_sync = Mock()
        workflow.jira_sync.create_escalation_issue.side_effect = RuntimeError("Jira API unreachable")

        ticket = make_ticket()
        confidence_eval = {"overall_confidence": 0.2, "reasoning": ["Low confidence"]}
        routing_decision = {"action": "escalate", "reason": "Low confidence - requires human expertise", "escalation_required": True}

        # Must not raise -- a Jira outage can't be allowed to prevent the
        # ticket from being marked escalated locally.
        workflow._create_escalation(ticket, confidence_eval, routing_decision)

        assert ticket.status == TicketStatus.ESCALATED
        assert ticket.escalation_info.jira_issue_key is None
        assert routing_decision["jira_issue_key"] is None
