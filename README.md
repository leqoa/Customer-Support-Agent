# AI-ITSS: Intelligent Autonomous Ticket Support System

An AI-powered customer support platform featuring structured agentic workflows, CRM/ITSM integration, and MCP-based plugin extensibility.

## Features

- 🤖 **Agentic Ticket Workflow**: Ticket classification, knowledge retrieval, draft generation, confidence evaluation, and escalation handling
- 🔗 **Zoho & Jira Integration**: Two-way sync with CRM and project management systems for customer support
- 📊 **Escalation Management**: Structured summaries and automated issue creation in Jira
- 🏷️ **Non-destructive CRM Tagging**: AI-state reflection without conflicts
- 🔌 **MCP/Plugin Layer**: Extensible integration with external tools and knowledge bases
- 💬 **Human-in-the-Loop**: AI assists while support agents remain in control
- 📈 **Analytics Dashboard**: Track performance and ticket metrics

## Project Structure

```
├── backend/
│   ├── core/
│   │   ├── ticket_processor.py
│   │   ├── agent_workflow.py
│   │   └── confidence_evaluator.py
│   ├── integrations/
│   │   ├── zoho_sync.py
│   │   ├── jira_sync.py
│   │   └── mcp_layer.py
│   ├── models/
│   │   └── ticket.py
│   └── utils/
│       └── escalation_formatter.py
├── frontend/
│   ├── public/
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   └── utils/
│   └── package.json
├── config/
│   ├── settings.yaml
│   └── mcp_plugins.yaml
├── docs/
│   └── API.md
└── docker-compose.yml
```

## Getting Started

See [Installation Guide](docs/INSTALLATION.md) for setup instructions.

## License

GPL-3.0
