"""FastAPI application entrypoint for the AI-ITSS REST API (Phase 2).

Run locally with:

    uvicorn backend.api.main:app --reload

Interactive OpenAPI docs are available at ``/docs`` (Swagger UI) and
``/redoc`` (ReDoc) using FastAPI's defaults.
"""
import logging

from fastapi import FastAPI

from backend.api.routes import drafts_router, tickets_router, workflows_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="AI-ITSS API",
    description=(
        "REST API for the AI-powered IT support ticket platform: ticket "
        "ingestion, the AI agent workflow, and human agent review actions."
    ),
    version="0.2.0",
)

app.include_router(tickets_router)
app.include_router(workflows_router)
app.include_router(drafts_router)

# Other integrations land as their own routers once those parallel PRs are
# merged, e.g.:
#   from backend.integrations.zoho_webhooks import router as zoho_webhook_router
#   app.include_router(zoho_webhook_router)


@app.get("/health", tags=["meta"])
def health_check() -> dict:
    """Basic liveness check -- not part of the Phase 2 spec, but useful for
    load balancers / uptime checks."""
    return {"status": "ok"}
