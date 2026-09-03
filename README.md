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

### Zoho Integration Configuration

`backend/integrations/zoho_sync.py` authenticates to Zoho using an OAuth2
refresh-token flow. Set the following environment variables:

| Variable | Required | Description |
| --- | --- | --- |
| `ZOHO_OAUTH_CLIENT_ID` | Yes | OAuth2 client id from the Zoho API console |
| `ZOHO_OAUTH_CLIENT_SECRET` | Yes | OAuth2 client secret from the Zoho API console |
| `ZOHO_REFRESH_TOKEN` | Yes | Long-lived refresh token issued for the client |
| `ZOHO_ACCOUNTS_BASE` | No | Zoho accounts/token endpoint, for region-specific data centers (default: `https://accounts.zoho.com`; e.g. `https://accounts.zoho.eu`, `https://accounts.zoho.in`, `https://accounts.zoho.com.au`) |
| `ZOHO_API_BASE` | No | Zoho CRM API base URL (default: `https://www.zohoapis.com`) |

Access tokens are fetched on demand and cached in memory until shortly
before they expire, then automatically refreshed.

`ZOHO_API_TOKEN` (a static bearer token) is still supported as a
**deprecated** fallback for older deployments when the OAuth2 variables
above are not set, but it is never refreshed and will eventually expire or
be revoked. New deployments should use the OAuth2 flow.

## License

GPL-3.0
