"""Ticket processing and classification engine"""
import logging
from typing import Optional, List, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class ClassificationCategory(str, Enum):
    """Ticket classification categories"""
    TECHNICAL = "technical"
    BILLING = "billing"
    ACCOUNT = "account"
    GENERAL = "general"
    FEATURE_REQUEST = "feature_request"
    BUG_REPORT = "bug_report"


class TicketProcessor:
    """
    Processes incoming tickets:
    - Classifies ticket type
    - Extracts key information
    - Determines routing and priority
    """
    
    def __init__(self):
        """Initialize ticket processor"""
        self.keywords_map = self._build_keywords_map()
    
    def _build_keywords_map(self) -> Dict[ClassificationCategory, List[str]]:
        """Build keyword map for classification"""
        return {
            ClassificationCategory.TECHNICAL: [
                "error", "bug", "crash", "not working", "issue", 
                "problem", "broken", "failed", "doesn't work", "exception"
            ],
            ClassificationCategory.BILLING: [
                "charge", "invoice", "payment", "refund", "bill", 
                "subscription", "cost", "price", "pricing", "money"
            ],
            ClassificationCategory.ACCOUNT: [
                "login", "password", "reset", "access", "account", 
                "user", "permission", "credential", "2fa", "mfa"
            ],
            ClassificationCategory.FEATURE_REQUEST: [
                "feature", "enhancement", "add", "implement", "support",
                "would like", "request", "please add", "suggestion"
            ],
            ClassificationCategory.BUG_REPORT: [
                "bug", "defect", "issue", "problem", "malfunction",
                "unexpected behavior", "should not", "shouldn't"
            ]
        }
    
    def classify_ticket(self, subject: str, description: str) -> Dict[str, Any]:
        """
        Classify ticket based on content
        
        Args:
            subject: Ticket subject line
            description: Ticket description
        
        Returns:
            Classification result with category and confidence
        """
        combined_text = f"{subject} {description}".lower()
        
        scores = {}
        for category, keywords in self.keywords_map.items():
            score = sum(1 for kw in keywords if kw in combined_text)
            scores[category] = score
        
        best_category = max(scores, key=scores.get) if scores else ClassificationCategory.GENERAL
        confidence = scores.get(best_category, 0) / max(len(self.keywords_map[best_category]), 1)
        confidence = min(confidence, 1.0)
        
        return {
            "category": best_category.value,
            "confidence": confidence,
            "scores": {k.value: v for k, v in scores.items()}
        }
    
    def extract_priority_signals(self, subject: str, description: str) -> Dict[str, Any]:
        """
        Extract priority signals from ticket content
        
        Args:
            subject: Ticket subject
            description: Ticket description
        
        Returns:
            Priority signals analysis
        """
        combined_text = f"{subject} {description}".lower()
        
        signals = {
            "urgent_keywords": 0,
            "blocking_keywords": 0,
            "business_impact": 0
        }
        
        urgent_words = ["urgent", "asap", "critical", "emergency", "immediately"]
        blocking_words = ["blocked", "cannot", "unable", "production", "down"]
        impact_words = ["business", "revenue", "customer", "all users", "everyone"]
        
        signals["urgent_keywords"] = sum(1 for w in urgent_words if w in combined_text)
        signals["blocking_keywords"] = sum(1 for w in blocking_words if w in combined_text)
        signals["business_impact"] = sum(1 for w in impact_words if w in combined_text)
        
        return signals
    
    def extract_ticket_info(self, subject: str, description: str) -> Dict[str, Any]:
        """
        Extract structured information from ticket
        
        Args:
            subject: Ticket subject
            description: Ticket description
        
        Returns:
            Extracted ticket information
        """
        return {
            "classification": self.classify_ticket(subject, description),
            "priority_signals": self.extract_priority_signals(subject, description),
            "word_count": len(description.split()),
            "has_screenshots": "[screenshot]" in description.lower() or "[image]" in description.lower(),
            "has_error_logs": "error" in description.lower() and ("stack" in description.lower() or "trace" in description.lower())
        }
