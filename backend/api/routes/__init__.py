"""API route modules, split by resource for navigability.

- ``tickets``   -> ``/tickets`` (ingest, retrieve, update, escalate)
- ``workflows`` -> ``/workflows`` (workflow status)
- ``drafts``    -> ``/drafts`` (agent approve/reject of AI drafts)

``backend/api/main.py`` wires these routers into the FastAPI app.
"""
from backend.api.routes.drafts import router as drafts_router
from backend.api.routes.tickets import router as tickets_router
from backend.api.routes.workflows import router as workflows_router

__all__ = ["tickets_router", "workflows_router", "drafts_router"]
