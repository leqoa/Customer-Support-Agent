"""Minimal API-key authentication stub.

*** PLACEHOLDER AUTH -- NOT PRODUCTION SECURITY ***

This checks a single shared secret (the ``X-API-Key`` header) against the
``API_KEY`` environment variable. It exists only to give the Phase 2 API
layer *some* gate in front of it while real authentication/authorization
(JWT/OAuth2, per-user API keys, scopes, etc.) is designed and built. It does
not support key rotation, multiple keys, expiry, or per-caller identity.

Mirrors how ``ZohoSync`` degrades when it isn't configured (see
``backend/integrations/zoho_sync.py``): if ``API_KEY`` is not set in the
environment, this is treated as "auth not configured yet" and all requests
are allowed through, with a one-time warning logged so it's obvious in
server logs that the API is running wide open. This keeps local/dev usage
frictionless while still failing closed once an operator opts in by setting
``API_KEY``.

TODO(security): replace with real JWT/OAuth2 based auth before any
non-development deployment.
"""
import logging
import os

from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)

_warned_unconfigured = False


def require_api_key(x_api_key: str = Header(default=None, alias="X-API-Key")) -> None:
    """FastAPI dependency enforcing the ``X-API-Key`` header.

    Behavior:
    - ``API_KEY`` env var unset -> auth is considered unconfigured (dev
      mode); a warning is logged once and the request is allowed through.
    - ``API_KEY`` env var set -> the caller must send a matching
      ``X-API-Key`` header, or the request is rejected with 401.
    """
    global _warned_unconfigured

    expected = os.getenv("API_KEY")

    if not expected:
        if not _warned_unconfigured:
            logger.warning(
                "API_KEY is not set; running in dev mode with API auth "
                "DISABLED. All requests will be allowed. Set the API_KEY "
                "environment variable to require X-API-Key on requests. "
                "This is a placeholder mechanism -- real auth (JWT/OAuth) "
                "is future work."
            )
            _warned_unconfigured = True
        return

    if x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
        )
