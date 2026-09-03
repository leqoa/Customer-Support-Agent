"""Confidence evaluation for AI-generated drafts"""
import logging
from typing import Dict, Any, Optional
from backend.models.ticket import AiDraft

logger = logging.getLogger(__name__)


class ConfidenceEvaluator:
    """
    Evaluates confidence scores for AI-generated responses
    Considers:
    - Draft quality metrics
    - Classification confidence
    - Knowledge source reliability
    - Complexity level
    """
    
    def __init__(self):
        """Initialize confidence evaluator"""
        self.weights = {
            "draft_quality": 0.4,
            "classification_confidence": 0.3,
            "knowledge_completeness": 0.2,
            "response_relevance": 0.1
        }
    
    def evaluate_draft(self, draft: AiDraft, classification: Optional[str] = None) -> Dict[str, Any]:
        """
        Evaluate confidence of AI draft response
        
        Args:
            draft: AI-generated draft
            classification: Ticket classification for context
        
        Returns:
            Confidence evaluation with component scores
        """
        evaluation = {
            "overall_confidence": 0.0,
            "component_scores": {},
            "reasoning": []
        }
        
        # Component 1: Draft Quality
        draft_quality = self._evaluate_draft_quality(draft)
        evaluation["component_scores"]["draft_quality"] = draft_quality
        
        # Component 2: Classification Confidence (simulated)
        classification_conf = self._evaluate_classification_confidence(classification)
        evaluation["component_scores"]["classification_confidence"] = classification_conf
        
        # Component 3: Knowledge Completeness (simulated)
        knowledge_comp = self._evaluate_knowledge_completeness(draft)
        evaluation["component_scores"]["knowledge_completeness"] = knowledge_comp
        
        # Component 4: Response Relevance (simulated)
        relevance = self._evaluate_response_relevance(draft)
        evaluation["component_scores"]["response_relevance"] = relevance
        
        # Calculate weighted overall confidence
        overall = sum(
            evaluation["component_scores"][comp] * self.weights[comp]
            for comp in self.weights.keys()
        )
        evaluation["overall_confidence"] = round(overall, 3)
        
        # Add reasoning
        evaluation["reasoning"] = self._generate_reasoning(evaluation)
        
        return evaluation
    
    def _evaluate_draft_quality(self, draft: AiDraft) -> float:
        """
        Evaluate quality of draft response
        Checks: length, completeness, professionalism
        """
        quality_score = 0.5  # Base score
        
        # Check content length
        if len(draft.content) > 100:
            quality_score += 0.2
        
        # Check structure (has summary and actions)
        if draft.summary and len(draft.summary) > 0:
            quality_score += 0.15
        
        if draft.suggested_actions and len(draft.suggested_actions) > 0:
            quality_score += 0.15
        
        return min(quality_score, 1.0)
    
    def _evaluate_classification_confidence(self, classification: Optional[str]) -> float:
        """
        Evaluate confidence in ticket classification
        """
        if classification is None:
            return 0.4
        
        # Different categories have different baseline confidences
        category_confidence = {
            "technical": 0.75,
            "billing": 0.8,
            "account": 0.85,
            "general": 0.6,
            "feature_request": 0.7,
            "bug_report": 0.8
        }
        
        return category_confidence.get(classification, 0.6)
    
    def _evaluate_knowledge_completeness(self, draft: AiDraft) -> float:
        """
        Evaluate completeness of knowledge sources used
        """
        # Check if suggested actions are provided
        if draft.suggested_actions and len(draft.suggested_actions) >= 2:
            return 0.8
        elif draft.suggested_actions and len(draft.suggested_actions) >= 1:
            return 0.6
        else:
            return 0.4
    
    def _evaluate_response_relevance(self, draft: AiDraft) -> float:
        """
        Evaluate relevance of response to ticket
        """
        # Check if summary and content are aligned
        relevance_score = 0.5
        
        if draft.summary in draft.content or draft.content.lower().startswith(draft.summary.lower()):
            relevance_score += 0.3
        
        if len(draft.suggested_actions) > 0:
            relevance_score += 0.2
        
        return min(relevance_score, 1.0)
    
    def _generate_reasoning(self, evaluation: Dict[str, Any]) -> list:
        """
        Generate human-readable reasoning for confidence score
        """
        reasoning = []
        overall = evaluation["overall_confidence"]
        
        if overall >= 0.8:
            reasoning.append("✓ High confidence - ready for agent review")
        elif overall >= 0.6:
            reasoning.append("~ Medium confidence - recommend human review before sending")
        else:
            reasoning.append("✗ Low confidence - escalation recommended")
        
        # Add component-specific insights
        components = evaluation["component_scores"]
        if components.get("draft_quality", 0) < 0.5:
            reasoning.append("- Draft quality needs improvement")
        if components.get("classification_confidence", 0) < 0.6:
            reasoning.append("- Ticket classification is uncertain")
        if components.get("knowledge_completeness", 0) < 0.5:
            reasoning.append("- Insufficient knowledge/context for definitive answer")
        
        return reasoning
