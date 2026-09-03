"""Provider-agnostic LLM service (Phase 2)

Provides a single `LLMService` class that can generate draft responses,
classify tickets, and produce escalation summaries using either OpenAI or
Anthropic as the backing provider, selected via configuration/env vars.

Design notes
------------
- No provider SDK is used. The `openai`/`anthropic` Python packages are not
  installed in this project, so both providers are called with minimal raw
  HTTP wrappers built on top of `requests` (already a project dependency).
  This mirrors the pattern already used by `backend/integrations/zoho_sync.py`.
- If no API key is configured for the active provider (or every retry of a
  live call fails), the service does NOT crash. It logs a warning/error and
  falls back to a clearly-labeled deterministic mock response, the same way
  `ZohoSync` falls back to mock/no-op behavior when its token is missing.
- Token/cost accounting is approximate. Real provider responses include a
  `usage` object with actual token counts (OpenAI: `usage.prompt_tokens` /
  `usage.completion_tokens`; Anthropic: `usage.input_tokens` /
  `usage.output_tokens`) and this service extracts and uses those numbers
  whenever a response actually contains them. The `len(text) // 4` heuristic
  is only a fallback for the rare case where a provider response is missing
  the usage field, and should eventually be replaced/validated against real
  billing data.
"""
import os
import re
import time
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests

from backend.models.ticket import AiDraft
from backend.core.ticket_processor import TicketProcessor, ClassificationCategory

logger = logging.getLogger(__name__)

# --- Provider configuration -------------------------------------------------

DEFAULT_MODELS = {
    "openai": "gpt-4",
    "anthropic": "claude-sonnet-5",
}

SUPPORTED_PROVIDERS = set(DEFAULT_MODELS.keys())

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

# Retry/backoff configuration for transient HTTP failures on live calls.
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

# Very rough $/1K-token estimates, used only for the approximate cost tracker
# below. These are NOT authoritative pricing - swap in real numbers (or read
# them from provider usage/billing responses) before relying on this for
# actual cost reporting.
APPROX_COST_PER_1K_TOKENS = {
    "openai": {"prompt": 0.03, "completion": 0.06},
    "anthropic": {"prompt": 0.003, "completion": 0.015},
}

VALID_CLASSIFICATION_CATEGORIES = {c.value for c in ClassificationCategory}


class LLMCallError(Exception):
    """Raised when a live LLM call could not be completed (missing key,
    exhausted retries, or an unexpected response shape)."""


class LLMService:
    """Provider-agnostic LLM service.

    Usage:
        service = LLMService()  # reads LLM_PROVIDER env var, defaults to "openai"
        draft = service.generate_draft(ticket, context, knowledge)
        classification = service.classify_with_llm(ticket.subject, ticket.description)
        summary = service.generate_escalation_summary(ticket)
    """

    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None):
        resolved_provider = (provider or os.getenv("LLM_PROVIDER") or "openai").lower().strip()
        if resolved_provider not in SUPPORTED_PROVIDERS:
            logger.warning(
                f"Unsupported LLM provider '{resolved_provider}'; "
                f"falling back to 'openai'. Supported providers: {sorted(SUPPORTED_PROVIDERS)}"
            )
            resolved_provider = "openai"

        self.provider = resolved_provider
        self.model = model or DEFAULT_MODELS[self.provider]

        # Used for the keyword-based classification fallback path.
        self.ticket_processor = TicketProcessor()

        # Running usage/cost tracker. Real per-call figures are threaded in
        # from provider `usage` objects where available (see _track_usage).
        self.usage: Dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

        api_key_env = self._api_key_env_var()
        if not os.getenv(api_key_env):
            logger.warning(
                f"{api_key_env} is not configured. LLMService will not call "
                f"the {self.provider} API and will fall back to mock responses."
            )

    # --- Public API ----------------------------------------------------

    def generate_draft(self, ticket, context: dict, knowledge: dict) -> AiDraft:
        """Generate an AI draft response for a ticket.

        Falls back to a clearly-labeled mock draft (model_used="mock-fallback")
        if no API key is configured or the live call fails after retries.
        """
        template = self._load_template("draft_generation")
        prompt = template.format(
            subject=ticket.subject,
            description=ticket.description,
            classification=(context or {}).get("classification", "unknown"),
            knowledge=json.dumps(knowledge or {}, default=str),
        )
        system = (
            "You are a helpful, professional customer support assistant. "
            "Follow the requested response format exactly."
        )

        try:
            text = self._call_llm(prompt, system=system)
        except LLMCallError as e:
            logger.error(f"generate_draft: LLM call failed, using mock fallback: {e}")
            return self._mock_draft()

        return self._parse_draft_response(text)

    def classify_with_llm(self, subject: str, description: str) -> Dict[str, Any]:
        """Classify a ticket using the LLM.

        Returns a dict like:
            {"category": str, "confidence": float, "reasoning": str, "source": "llm"}

        If the live call fails, or the response can't be parsed safely, this
        falls back to `TicketProcessor().classify_ticket(...)` and marks the
        result with "source": "keyword_fallback".
        """
        template = self._load_template("classification")
        prompt = template.format(subject=subject, description=description)
        system = "You are a ticket classification assistant for a customer support system."

        try:
            text = self._call_llm(prompt, system=system)
        except LLMCallError as e:
            logger.error(
                f"classify_with_llm: LLM call failed, falling back to keyword classifier: {e}"
            )
            return self._keyword_fallback_classification(subject, description, fallback_reason=str(e))

        parsed = self._parse_classification_response(text)
        if parsed is None:
            logger.error(
                "classify_with_llm: could not parse LLM response, falling back to keyword classifier"
            )
            return self._keyword_fallback_classification(
                subject, description, fallback_reason="unparseable_llm_response", raw_response=text
            )

        parsed["source"] = "llm"
        return parsed

    def generate_escalation_summary(self, ticket) -> str:
        """Generate a human-readable escalation summary for a ticket.

        Falls back to a clearly-labeled mock summary if the live call fails.
        """
        template = self._load_template("escalation_summary")
        classification = getattr(ticket.ai_context, "classification", None) if getattr(
            ticket, "ai_context", None
        ) else None
        draft_summary = (
            ticket.ai_draft.summary if getattr(ticket, "ai_draft", None) else "N/A"
        )
        prompt = template.format(
            ticket_id=ticket.id,
            subject=ticket.subject,
            description=ticket.description,
            classification=classification or "unknown",
            draft_summary=draft_summary,
        )
        system = "You write concise, actionable escalation summaries for human support agents."

        try:
            text = self._call_llm(prompt, system=system)
            return text.strip()
        except LLMCallError as e:
            logger.error(
                f"generate_escalation_summary: LLM call failed, using mock fallback: {e}"
            )
            return self._mock_escalation_summary(ticket)

    # --- Provider dispatch ----------------------------------------------

    def _api_key_env_var(self) -> str:
        return "OPENAI_API_KEY" if self.provider == "openai" else "ANTHROPIC_API_KEY"

    def _call_llm(self, prompt: str, system: Optional[str] = None) -> str:
        """Dispatch to the configured provider's raw HTTP call.

        Raises LLMCallError if no API key is configured, or if the live call
        fails after retries. Callers are expected to catch this and fall back
        to a mock response.
        """
        api_key_env = self._api_key_env_var()
        if not os.getenv(api_key_env):
            raise LLMCallError(f"Missing {api_key_env}; no API key configured for provider '{self.provider}'")

        if self.provider == "openai":
            return self._call_openai(prompt, system=system)
        elif self.provider == "anthropic":
            return self._call_anthropic(prompt, system=system)

        # Should be unreachable given __init__ validation, but keep it safe.
        raise LLMCallError(f"Unsupported provider: {self.provider}")

    def _call_openai(self, prompt: str, system: Optional[str] = None) -> str:
        """Raw HTTP call to OpenAI's chat completions endpoint.

        Retries transient failures (network errors, 5xx, unexpected response
        shape) up to MAX_RETRIES times with manual exponential backoff.
        Raises LLMCallError if every attempt fails.
        """
        api_key = os.getenv("OPENAI_API_KEY")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "messages": messages}

        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    OPENAI_CHAT_COMPLETIONS_URL, headers=headers, json=payload, timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"]

                usage = data.get("usage") or {}
                self._track_usage(
                    prompt_tokens=usage.get("prompt_tokens", self._approx_tokens(prompt)),
                    completion_tokens=usage.get("completion_tokens", self._approx_tokens(text)),
                )
                return text
            except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as e:
                last_error = e
                logger.warning(
                    f"OpenAI call attempt {attempt}/{MAX_RETRIES} failed: {e}"
                )
                if attempt < MAX_RETRIES:
                    time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))

        logger.error(f"OpenAI call failed after {MAX_RETRIES} attempts: {last_error}")
        raise LLMCallError(f"OpenAI call failed after {MAX_RETRIES} attempts: {last_error}")

    def _call_anthropic(self, prompt: str, system: Optional[str] = None) -> str:
        """Raw HTTP call to Anthropic's messages endpoint.

        Retries transient failures up to MAX_RETRIES times with manual
        exponential backoff. Raises LLMCallError if every attempt fails.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    ANTHROPIC_MESSAGES_URL, headers=headers, json=payload, timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
                content_blocks = data.get("content") or []
                text = "".join(
                    block.get("text", "") for block in content_blocks if block.get("type") == "text"
                )
                if not text:
                    raise ValueError("No text content block in Anthropic response")

                usage = data.get("usage") or {}
                self._track_usage(
                    prompt_tokens=usage.get("input_tokens", self._approx_tokens(prompt)),
                    completion_tokens=usage.get("output_tokens", self._approx_tokens(text)),
                )
                return text
            except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as e:
                last_error = e
                logger.warning(
                    f"Anthropic call attempt {attempt}/{MAX_RETRIES} failed: {e}"
                )
                if attempt < MAX_RETRIES:
                    time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))

        logger.error(f"Anthropic call failed after {MAX_RETRIES} attempts: {last_error}")
        raise LLMCallError(f"Anthropic call failed after {MAX_RETRIES} attempts: {last_error}")

    # --- Response parsing -------------------------------------------------

    def _parse_draft_response(self, text: str) -> AiDraft:
        """Defensively parse the structured draft response.

        The template asks the model to reply with SUMMARY/ACTIONS/RESPONSE
        sections. If any section is missing/malformed, fall back to
        reasonable defaults derived from the raw text rather than raising.
        """
        summary = ""
        actions: List[str] = []
        content = text.strip() if text else ""

        try:
            summary_match = re.search(r"SUMMARY:\s*(.+)", text)
            if summary_match:
                summary = summary_match.group(1).strip().splitlines()[0].strip()

            actions_match = re.search(r"ACTIONS:\s*(.*?)(?:RESPONSE:|$)", text, re.DOTALL)
            if actions_match:
                actions = [
                    line.strip().lstrip("-*").strip()
                    for line in actions_match.group(1).splitlines()
                    if line.strip().lstrip("-*").strip()
                ]

            response_match = re.search(r"RESPONSE:\s*(.*)", text, re.DOTALL)
            if response_match:
                content = response_match.group(1).strip()
        except (TypeError, AttributeError) as e:
            logger.warning(f"Failed to parse structured draft response, using raw text: {e}")

        if not content:
            content = "Draft response based on ticket analysis and knowledge base."
        if not summary:
            summary = (content[:120] + "...") if len(content) > 120 else content
        if not actions:
            actions = ["Review ticket details", "Respond to customer"]

        return AiDraft(
            content=content,
            summary=summary,
            suggested_actions=actions,
            model_used=self.model,
        )

    def _parse_classification_response(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse the CATEGORY/CONFIDENCE/REASONING response format.

        Returns None (rather than raising) if the response is malformed, so
        callers can safely fall back to keyword-based classification.
        """
        if not text:
            return None
        try:
            category_match = re.search(r"CATEGORY:\s*([a-zA-Z_]+)", text)
            confidence_match = re.search(r"CONFIDENCE:\s*([0-9]*\.?[0-9]+)", text)
            reasoning_match = re.search(r"REASONING:\s*(.+)", text, re.DOTALL)

            if not category_match or not confidence_match:
                return None

            category = category_match.group(1).strip().lower()
            if category not in VALID_CLASSIFICATION_CATEGORIES:
                return None

            confidence = float(confidence_match.group(1))
            confidence = max(0.0, min(1.0, confidence))

            reasoning = reasoning_match.group(1).strip() if reasoning_match else ""

            return {"category": category, "confidence": confidence, "reasoning": reasoning}
        except (ValueError, AttributeError) as e:
            logger.warning(f"Error parsing classification response: {e}")
            return None

    def _keyword_fallback_classification(
        self,
        subject: str,
        description: str,
        fallback_reason: Optional[str] = None,
        raw_response: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = self.ticket_processor.classify_ticket(subject, description)
        fallback: Dict[str, Any] = {
            "category": result["category"],
            "confidence": result["confidence"],
            "reasoning": "Fell back to keyword-based classification (TicketProcessor).",
            "source": "keyword_fallback",
        }
        if fallback_reason:
            fallback["fallback_reason"] = fallback_reason
        if raw_response is not None:
            fallback["raw_llm_response"] = raw_response
        return fallback

    # --- Mock fallbacks -----------------------------------------------

    def _mock_draft(self) -> AiDraft:
        """Deterministic mock draft, structurally identical to the
        placeholder in agent_workflow.py, tagged so downstream consumers can
        tell this was not a real LLM call."""
        return AiDraft(
            content="Draft response based on ticket analysis and knowledge base.",
            summary="Addressing customer's technical issue.",
            suggested_actions=["Troubleshooting Step 1", "Troubleshooting Step 2"],
            model_used="mock-fallback",
        )

    def _mock_escalation_summary(self, ticket) -> str:
        return (
            f"[MOCK-FALLBACK] Escalation summary for ticket {ticket.id} "
            f"('{ticket.subject}'). No live LLM call was made because no "
            f"provider credentials were configured or the call failed. "
            f"Please review the ticket manually."
        )

    # --- Usage / cost tracking -------------------------------------------

    def _approx_tokens(self, text: str) -> int:
        """Rough token-count approximation (~4 chars/token). Only used when a
        provider response is missing its real `usage` field - real responses
        should always be preferred (see _call_openai/_call_anthropic)."""
        if not text:
            return 0
        return max(1, len(text) // 4)

    def _track_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.usage["prompt_tokens"] += prompt_tokens
        self.usage["completion_tokens"] += completion_tokens
        self.usage["estimated_cost_usd"] = round(
            self.usage["estimated_cost_usd"] + self._estimate_cost(prompt_tokens, completion_tokens),
            6,
        )

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        rates = APPROX_COST_PER_1K_TOKENS.get(self.provider, {"prompt": 0.0, "completion": 0.0})
        return (prompt_tokens / 1000.0) * rates["prompt"] + (completion_tokens / 1000.0) * rates["completion"]

    # --- Template loading -------------------------------------------------

    def _load_template(self, name: str) -> str:
        """Load a prompt template from templates/llm/{name}.txt relative to
        the repository root, regardless of current working directory."""
        templates_dir = Path(__file__).resolve().parent.parent.parent / "templates" / "llm"
        template_path = templates_dir / f"{name}.txt"
        try:
            return template_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.error(f"Could not load LLM template '{name}' from {template_path}: {e}")
            raise
