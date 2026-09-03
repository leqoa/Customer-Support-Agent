"""Escalation summary formatter for structured escalation to engineering"""
import logging
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class EscalationFormatter:
    """
    Formats ticket escalations into structured summaries for engineering/specialists
    Provides clear context: problem statement, investigation results, action requested
    """
    
    @staticmethod
    def format_escalation_summary(
        ticket_id: str,
        customer_name: str,
        subject: str,
        original_issue: str,
        cs_investigation: str,
        requested_action: str,
        priority: str,
        supporting_info: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Format escalation summary for handoff to engineering
        
        Args:
            ticket_id: Original ticket ID
            customer_name: Customer name
            subject: Ticket subject
            original_issue: Original customer-reported issue
            cs_investigation: What CS has investigated
            requested_action: What needs to be done
            priority: Escalation priority
            supporting_info: Additional context (logs, reproduction steps, etc.)
        
        Returns:
            Formatted escalation summary
        """
        summary = {
            "escalation_id": f"ESC-{ticket_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "timestamp": datetime.utcnow().isoformat(),
            "ticket_id": ticket_id,
            "priority": priority,
            
            "problem_statement": {
                "customer": customer_name,
                "subject": subject,
                "description": original_issue
            },
            
            "customer_support_investigation": {
                "steps_taken": cs_investigation,
                "findings": [],
                "workarounds_attempted": []
            },
            
            "requested_action": {
                "action": requested_action,
                "urgency": priority,
                "target_team": None  # Will be set based on priority/type
            },
            
            "supporting_information": supporting_info or {},
            
            "handoff_checklist": [
                "Reproduce issue locally",
                "Check logs and error traces",
                "Identify root cause",
                "Estimate fix timeline",
                "Provide update to customer via support"
            ]
        }
        
        return summary
    
    @staticmethod
    def format_for_jira(
        escalation_summary: Dict[str, Any],
        jira_project_key: str = "ITSS"
    ) -> Dict[str, Any]:
        """
        Convert escalation summary to Jira issue format
        
        Args:
            escalation_summary: Formatted escalation summary
            jira_project_key: Target Jira project key
        
        Returns:
            Jira issue payload
        """
        priority_mapping = {
            "critical": "Highest",
            "high": "High",
            "medium": "Medium",
            "low": "Low"
        }
        
        jira_issue = {
            "fields": {
                "project": {"key": jira_project_key},
                "summary": f"[ESCALATED] {escalation_summary['problem_statement']['subject']}",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "heading",
                            "attrs": {"level": 1},
                            "content": [{"type": "text", "text": "Escalation Summary"}]
                        },
                        {
                            "type": "heading",
                            "attrs": {"level": 2},
                            "content": [{"type": "text", "text": "Problem Statement"}]
                        },
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": f"Customer: {escalation_summary['problem_statement']['customer']}"},
                                {"type": "hardBreak"},
                                {"type": "text", "text": f"Issue: {escalation_summary['problem_statement']['description']}"}  
                            ]
                        },
                        {
                            "type": "heading",
                            "attrs": {"level": 2},
                            "content": [{"type": "text", "text": "CS Investigation"}]
                        },
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": escalation_summary['customer_support_investigation']['steps_taken']}]
                        },
                        {
                            "type": "heading",
                            "attrs": {"level": 2},
                            "content": [{"type": "text", "text": "Requested Action"}]
                        },
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": escalation_summary['requested_action']['action']}]
                        }
                    ]
                },
                "issuetype": {"name": "Escalation"},
                "priority": {"name": priority_mapping.get(escalation_summary['priority'], "Medium")},
                "labels": [f"ticket-{escalation_summary['ticket_id']}", "customer-escalation", "ai-generated"]
            }
        }
        
        return jira_issue
