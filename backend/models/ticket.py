"""Ticket data models for ITSS"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime


class TicketStatus(str, Enum):
    """Ticket status states"""
    NEW = "new"
    IN_PROGRESS = "in_progress"
    AWAITING_CUSTOMER = "awaiting_customer"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    CLOSED = "closed"


class TicketPriority(str, Enum):
    """Ticket priority levels"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AiWorkflowState(str, Enum):
    """AI workflow state tracking"""
    CLASSIFIED = "classified"
    KNOWLEDGE_RETRIEVED = "knowledge_retrieved"
    DRAFT_GENERATED = "draft_generated"
    CONFIDENCE_EVALUATED = "confidence_evaluated"
    AWAITING_REVIEW = "awaiting_review"
    REVIEWED = "reviewed"
    ESCALATED = "escalated"


@dataclass
class CustomerInfo:
    """Customer information from CRM"""
    id: str
    name: str
    email: str
    phone: Optional[str] = None
    account_id: Optional[str] = None
    crm_link: Optional[str] = None


@dataclass
class TicketContext:
    """Context and metadata for ticket processing"""
    classification: Optional[str] = None
    retrieved_knowledge: List[Dict[str, Any]] = field(default_factory=list)
    related_tickets: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    custom_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AiDraft:
    """AI-generated draft response"""
    content: str
    summary: str
    suggested_actions: List[str]
    confidence_score: float = 0.0
    model_used: str = "gpt-4"
    generated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class EscalationInfo:
    """Escalation tracking information"""
    reason: str
    escalation_type: str  # e.g., "technical", "billing", "vip"
    escalated_to: Optional[str] = None
    jira_issue_id: Optional[str] = None
    jira_issue_key: Optional[str] = None
    jira_url: Optional[str] = None
    escalated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Ticket:
    """Main Ticket model"""
    id: str
    subject: str
    description: str
    customer: CustomerInfo
    status: TicketStatus = TicketStatus.NEW
    priority: TicketPriority = TicketPriority.MEDIUM
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    assigned_to: Optional[str] = None
    
    # CRM Integration
    crm_ticket_id: Optional[str] = None
    crm_system: str = "zoho"  # e.g., "zoho", "zendesk"
    crm_link: Optional[str] = None
    
    # AI Workflow
    ai_workflow_state: AiWorkflowState = AiWorkflowState.CLASSIFIED
    ai_context: TicketContext = field(default_factory=TicketContext)
    ai_draft: Optional[AiDraft] = None
    
    # Escalation
    escalation_info: Optional[EscalationInfo] = None
    
    # Conversation history
    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    
    def add_message(self, role: str, content: str):
        """Add message to conversation history"""
        self.conversation_history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat()
        })
        self.updated_at = datetime.utcnow()
    
    def update_workflow_state(self, new_state: AiWorkflowState):
        """Update AI workflow state"""
        self.ai_workflow_state = new_state
        self.updated_at = datetime.utcnow()
    
    def mark_escalated(self, escalation_info: EscalationInfo):
        """Mark ticket as escalated"""
        self.escalation_info = escalation_info
        self.status = TicketStatus.ESCALATED
        self.update_workflow_state(AiWorkflowState.ESCALATED)
        # Add non-destructive tag for CRM
        self.ai_context.tags.append("AI-Escalated")
    
    def to_dict(self):
        """Convert ticket to dictionary"""
        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "customer": self.customer.__dict__,
            "status": self.status.value,
            "priority": self.priority.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "assigned_to": self.assigned_to,
            "crm_ticket_id": self.crm_ticket_id,
            "crm_system": self.crm_system,
            "ai_workflow_state": self.ai_workflow_state.value,
            "ai_context": self.ai_context.__dict__,
            "escalation_info": self.escalation_info.__dict__ if self.escalation_info else None,
            "conversation_history": self.conversation_history
        }
