"""Agent workflow orchestration"""
import logging
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum

from backend.models.ticket import (
    Ticket, AiWorkflowState, AiDraft, EscalationInfo
)
from backend.core.ticket_processor import TicketProcessor
from backend.core.confidence_evaluator import ConfidenceEvaluator
from backend.core.knowledge_retriever import KnowledgeRetriever
from backend.utils.escalation_formatter import EscalationFormatter

# The following three integrations are separate, parallel Phase 2 PRs and
# may not exist in this checkout yet, in any order. Import them optionally
# so AgentWorkflow keeps working (with its original placeholder behavior)
# regardless of which of these have landed -- and upgrades automatically,
# with no further code changes here, the moment each one merges.
try:
    from backend.core.llm_service import LLMService
except ImportError:
    LLMService = None

try:
    from backend.integrations.jira_sync import JiraSync
except ImportError:
    JiraSync = None

try:
    from backend.integrations.mcp_layer import MCPRegistry
except ImportError:
    MCPRegistry = None

logger = logging.getLogger(__name__)


class WorkflowStep(str, Enum):
    """Workflow execution steps"""
    CLASSIFY = "classify"
    RETRIEVE_KNOWLEDGE = "retrieve_knowledge"
    GENERATE_DRAFT = "generate_draft"
    EVALUATE_CONFIDENCE = "evaluate_confidence"
    ROUTE_FOR_REVIEW = "route_for_review"
    AWAIT_HUMAN_REVIEW = "await_human_review"


def _build_mcp_registry() -> Optional[Any]:
    """
    Build an MCPRegistry with plugins loaded from config, if the MCP plugin
    layer (backend/integrations/mcp_layer.py) is available in this checkout.
    Returns None otherwise, or if config loading fails for any reason --
    KnowledgeRetriever treats a None registry as "use local fallback only".
    """
    if MCPRegistry is None:
        return None
    try:
        registry = MCPRegistry()
        registry.load_plugins_from_config()
        return registry
    except Exception as e:  # noqa: BLE001 - never let plugin config issues block startup
        logger.warning(f"Could not initialize MCP registry, knowledge retrieval will use local fallback only: {e}")
        return None


class AgentWorkflow:
    """
    Orchestrates the ticket handling workflow:
    1. Classify ticket
    2. Retrieve relevant knowledge
    3. Generate AI draft response
    4. Evaluate confidence
    5. Route for human review or escalation

    Real classification/draft-generation/escalation are provided by
    LLMService, KnowledgeRetriever, and JiraSync respectively. LLMService
    and JiraSync are optional at import time (see module-level try/except
    imports above) since they're separate, independently-landing Phase 2
    PRs -- every step below degrades to its original Phase 1 placeholder
    behavior when its backing service isn't available yet, and every
    downstream-service call is caught locally so a failure in one
    integration (e.g. Jira being down) never prevents the rest of the
    workflow -- including the ticket actually getting marked escalated --
    from completing.
    """

    def __init__(self):
        """Initialize agent workflow"""
        self.ticket_processor = TicketProcessor()
        self.confidence_evaluator = ConfidenceEvaluator()
        self.knowledge_retriever = KnowledgeRetriever(mcp_registry=_build_mcp_registry())

        self.llm_service = None
        if LLMService is not None:
            try:
                self.llm_service = LLMService()
            except Exception as e:  # noqa: BLE001 - a bad LLM config shouldn't block startup
                logger.warning(f"Failed to initialize LLMService, draft generation will use the placeholder response: {e}")
        else:
            logger.info(
                "LLMService not available yet (backend.core.llm_service not found) - "
                "classification/draft generation will use Phase 1 placeholders until that module is merged."
            )

        self.jira_sync = None
        if JiraSync is not None:
            try:
                self.jira_sync = JiraSync()
            except Exception as e:  # noqa: BLE001 - a bad Jira config shouldn't block startup
                logger.warning(f"Failed to initialize JiraSync, escalations will be recorded locally only: {e}")
        else:
            logger.info(
                "JiraSync not available yet (backend.integrations.jira_sync not found) - "
                "escalations will be recorded locally without creating a Jira issue until that module is merged."
            )

    def execute_workflow(self, ticket: Ticket) -> Dict[str, Any]:
        """
        Execute complete workflow for a ticket

        Args:
            ticket: Ticket to process

        Returns:
            Workflow execution result
        """
        logger.info(f"Starting workflow for ticket {ticket.id}")

        workflow_result = {
            "ticket_id": ticket.id,
            "steps_executed": [],
            "final_state": None,
            "errors": []
        }

        try:
            # Step 1: Classify
            classification = self._classify_ticket(ticket)
            workflow_result["steps_executed"].append(WorkflowStep.CLASSIFY.value)

            # Step 2: Retrieve knowledge
            knowledge = self._retrieve_knowledge(ticket, classification)
            workflow_result["steps_executed"].append(WorkflowStep.RETRIEVE_KNOWLEDGE.value)

            # Step 3: Generate draft
            draft = self._generate_draft(ticket, knowledge)
            workflow_result["steps_executed"].append(WorkflowStep.GENERATE_DRAFT.value)

            # Step 4: Evaluate confidence
            confidence_eval = self._evaluate_confidence(ticket, draft)
            workflow_result["steps_executed"].append(WorkflowStep.EVALUATE_CONFIDENCE.value)

            # Step 5: Route for review or escalation
            routing_decision = self._route_for_review(ticket, confidence_eval)
            workflow_result["steps_executed"].append(WorkflowStep.ROUTE_FOR_REVIEW.value)

            workflow_result["final_state"] = ticket.ai_workflow_state.value
            workflow_result["routing_decision"] = routing_decision

        except Exception as e:
            logger.error(f"Error in workflow for ticket {ticket.id}: {str(e)}")
            workflow_result["errors"].append(str(e))

        return workflow_result

    def _classify_ticket(self, ticket: Ticket) -> Dict[str, Any]:
        """
        Classify ticket and update context.

        Always computes the Phase 1 keyword-based classification first
        (it's cheap, deterministic, and provides priority signals / word
        counts the rest of the pipeline also relies on), then -- if
        LLMService is available -- asks the LLM to classify and, on
        success, replaces just the classification portion with its
        result. Any LLM failure logs a warning and keeps the keyword
        result, so a flaky/unconfigured LLM never blocks classification.
        """
        extracted = self.ticket_processor.extract_ticket_info(
            ticket.subject,
            ticket.description
        )

        if self.llm_service is not None:
            try:
                llm_result = self.llm_service.classify_with_llm(ticket.subject, ticket.description)
                extracted["classification"] = {
                    "category": llm_result.get("category", extracted["classification"]["category"]),
                    "confidence": llm_result.get("confidence", extracted["classification"]["confidence"]),
                    "scores": extracted["classification"].get("scores", {}),
                    "source": llm_result.get("source", "llm"),
                }
            except Exception as e:  # noqa: BLE001 - keep the keyword-based fallback on any LLM failure
                logger.warning(f"LLM classification failed for ticket {ticket.id}, keeping keyword-based result: {e}")

        ticket.ai_context.classification = extracted["classification"]["category"]
        ticket.update_workflow_state(AiWorkflowState.CLASSIFIED)
        logger.info(f"Classified ticket {ticket.id} as {extracted['classification']['category']}")
        return extracted

    def _retrieve_knowledge(self, ticket: Ticket, classification: Dict) -> Dict[str, Any]:
        """Retrieve relevant knowledge/documentation via KnowledgeRetriever."""
        category = classification["classification"]["category"]
        query = f"{ticket.subject} {ticket.description}"

        try:
            relevant_docs = self.knowledge_retriever.search_knowledge_base(query)
        except Exception as e:  # noqa: BLE001 - a KB hiccup shouldn't block the workflow
            logger.warning(f"Knowledge base search failed for ticket {ticket.id}: {e}")
            relevant_docs = []

        try:
            # No ticket store/DB exists yet to source real candidates from
            # (separate Phase 2 work); KnowledgeRetriever already returns
            # [] when no candidates are supplied.
            similar_tickets = self.knowledge_retriever.get_similar_tickets(ticket, candidate_tickets=None)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Similar-ticket lookup failed for ticket {ticket.id}: {e}")
            similar_tickets = []

        try:
            faq_items = self.knowledge_retriever.retrieve_for_classification(category)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"FAQ retrieval failed for ticket {ticket.id}: {e}")
            faq_items = []

        knowledge = {
            "relevant_docs": relevant_docs,
            "similar_tickets": similar_tickets,
            "faq_items": faq_items
        }
        ticket.ai_context.retrieved_knowledge.append(knowledge)
        ticket.update_workflow_state(AiWorkflowState.KNOWLEDGE_RETRIEVED)
        logger.info(f"Retrieved knowledge for ticket {ticket.id}")
        return knowledge

    def _generate_draft(self, ticket: Ticket, knowledge: Dict) -> AiDraft:
        """Generate AI draft response via LLMService, falling back to the Phase 1 placeholder."""
        draft = None
        if self.llm_service is not None:
            try:
                context = {
                    "classification": ticket.ai_context.classification,
                    "tags": ticket.ai_context.tags,
                    "customer_name": ticket.customer.name,
                }
                draft = self.llm_service.generate_draft(ticket, context, knowledge)
            except Exception as e:  # noqa: BLE001 - fall back to the placeholder on any LLM failure
                logger.error(f"LLM draft generation failed for ticket {ticket.id}, using placeholder draft: {e}")

        if draft is None:
            draft = self._placeholder_draft()

        ticket.ai_draft = draft
        ticket.update_workflow_state(AiWorkflowState.DRAFT_GENERATED)
        logger.info(f"Generated draft for ticket {ticket.id}")
        return draft

    @staticmethod
    def _placeholder_draft() -> AiDraft:
        """The original Phase 1 placeholder draft, used whenever LLMService is unavailable/fails."""
        return AiDraft(
            content="Draft response based on ticket analysis and knowledge base.",
            summary="Addressing customer's technical issue.",
            suggested_actions=["Troubleshooting Step 1", "Troubleshooting Step 2"],
            model_used="gpt-4"
        )

    def _evaluate_confidence(self, ticket: Ticket, draft: AiDraft) -> Dict[str, Any]:
        """Evaluate confidence score of AI draft"""
        evaluation = self.confidence_evaluator.evaluate_draft(
            draft,
            ticket.ai_context.classification
        )
        draft.confidence_score = evaluation["overall_confidence"]
        ticket.update_workflow_state(AiWorkflowState.CONFIDENCE_EVALUATED)
        logger.info(f"Evaluated confidence for ticket {ticket.id}: {evaluation['overall_confidence']:.2f}")
        return evaluation

    def _route_for_review(self, ticket: Ticket, confidence_eval: Dict) -> Dict[str, Any]:
        """
        Route ticket based on confidence and complexity
        Returns: {"action": "review_by_human" | "escalate" | "auto_respond"}
        """
        confidence = confidence_eval["overall_confidence"]

        routing_decision = {
            "action": None,
            "reason": None,
            "escalation_required": False
        }

        if confidence < 0.5:
            routing_decision["action"] = "escalate"
            routing_decision["reason"] = "Low confidence - requires human expertise"
            routing_decision["escalation_required"] = True
            self._create_escalation(ticket, confidence_eval, routing_decision)
        elif confidence < 0.75:
            routing_decision["action"] = "review_by_human"
            routing_decision["reason"] = "Medium confidence - needs human review"
            ticket.update_workflow_state(AiWorkflowState.AWAITING_REVIEW)
        else:
            routing_decision["action"] = "ready_for_agent"
            routing_decision["reason"] = "High confidence - ready for agent approval"
            ticket.update_workflow_state(AiWorkflowState.AWAITING_REVIEW)

        return routing_decision

    def _create_escalation(self, ticket: Ticket, confidence_eval: Dict, routing_decision: Dict) -> None:
        """
        Build a structured escalation summary, try to open a linked Jira
        issue (if JiraSync is available and succeeds), and mark the ticket
        escalated locally either way -- a Jira outage or missing
        configuration should never prevent the ticket itself from being
        correctly marked ESCALATED.
        """
        summary = EscalationFormatter.format_escalation_summary(
            ticket_id=ticket.id,
            customer_name=ticket.customer.name,
            subject=ticket.subject,
            original_issue=ticket.description,
            cs_investigation="Automated AI triage: " + "; ".join(confidence_eval.get("reasoning", [])),
            requested_action=routing_decision.get("reason", "Escalation required"),
            priority=ticket.priority.value,
        )

        jira_result = None
        if self.jira_sync is not None:
            try:
                jira_result = self.jira_sync.create_escalation_issue(summary)
            except Exception as e:  # noqa: BLE001 - local escalation must still succeed
                logger.error(f"Failed to create Jira escalation issue for ticket {ticket.id}: {e}")

        escalation_info = EscalationInfo(
            reason=routing_decision.get("reason", "Low confidence"),
            escalation_type=ticket.ai_context.classification or "general",
            jira_issue_id=jira_result.get("id") if jira_result else None,
            jira_issue_key=jira_result.get("key") if jira_result else None,
            jira_url=jira_result.get("url") if jira_result else None,
        )
        ticket.mark_escalated(escalation_info)

        routing_decision["escalation_summary"] = summary
        routing_decision["jira_issue_key"] = escalation_info.jira_issue_key
        logger.info(
            f"Escalated ticket {ticket.id}"
            + (f" -> Jira {escalation_info.jira_issue_key}" if escalation_info.jira_issue_key else " (no Jira issue created)")
        )
