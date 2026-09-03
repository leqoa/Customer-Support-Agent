"""Knowledge base retrieval for AI draft generation.

This module implements Phase 2 Task 3 ("Knowledge Base Retrieval") from
docs/PHASE1_SUMMARY.md. It supplies the context consumed by
`AgentWorkflow._retrieve_knowledge` (see backend/core/agent_workflow.py) --
that method currently returns a placeholder shape:

    {"relevant_docs": [], "similar_tickets": [], "faq_items": []}

`KnowledgeRetriever` produces real values for those three lists (or the
building blocks for them) via three public methods:

    .search_knowledge_base(query, top_k)     -> "relevant_docs"
    .get_similar_tickets(ticket, ..., top_k) -> "similar_tickets"
    .retrieve_for_classification(category)   -> "faq_items"

Wiring this into `AgentWorkflow` is intentionally left to a separate PR.

MCP-optional design
--------------------
Phase 2 Task 2 (backend/integrations/mcp_layer.py) is being built in
parallel and may or may not exist yet in this checkout. This class never
imports that module directly. Instead it accepts an already-constructed
`mcp_registry` object (any object exposing `.execute_plugin(name, query)`,
per the documented MCP contract) via its constructor:

* When `mcp_registry` is provided, `search_knowledge_base` (and, if
  available, `retrieve_for_classification`) will try calling
  ``mcp_registry.execute_plugin("knowledge_base", {...})`` and normalize
  whatever comes back into this module's documented list-of-dicts shape.
* When `mcp_registry` is `None`, or the call is unavailable
  (`AttributeError`/`ImportError` from a half-finished plugin layer) or
  raises for any other reason, every method falls back to a local,
  dependency-free implementation so this class is genuinely useful on its
  own with zero external services and zero network calls:
    - `search_knowledge_base` falls back to local keyword search over
      documents supplied via `load_documents()`.
    - `get_similar_tickets` falls back to a keyword/category heuristic
      over an explicitly supplied candidate list (there is no ticket
      store yet).
    - `retrieve_for_classification` falls back to a small built-in FAQ
      dict keyed by `ClassificationCategory`, clearly tagged
      `"source": "builtin_fallback"` so it is never mistaken for real KB
      content.

Because of this, the class works standalone today and will start using
real knowledge-base plugins automatically the moment
`backend/integrations/mcp_layer.py` lands and a caller passes an
`MCPRegistry` instance in -- no changes needed here, and in no particular
merge order relative to that PR.
"""
import logging
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Union

from backend.core.ticket_processor import ClassificationCategory
from backend.models.ticket import Ticket

logger = logging.getLogger(__name__)

DEFAULT_CACHE_SIZE = 128
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class KnowledgeRetriever:
    """
    Supplies knowledge-base context, similar tickets, and FAQ items to the
    AI draft-generation step.

    Falls back to local keyword search when no MCP knowledge-base plugin
    is configured (see module docstring for the full MCP-optional design).
    """

    #: Small, clearly-labeled example FAQ content used only when no MCP
    #: knowledge-base plugin is configured. Reuses ClassificationCategory
    #: from ticket_processor.py rather than redefining categories.
    _BUILTIN_FAQ: Dict[str, List[Dict[str, Any]]] = {
        ClassificationCategory.TECHNICAL.value: [
            {
                "question": "The application is showing an error or has crashed. What should I do first?",
                "answer": (
                    "Ask the customer for the exact error text or a screenshot, note when it "
                    "started, and check the status page for known incidents before troubleshooting."
                ),
                "source": "builtin_fallback",
            },
            {
                "question": "How do I gather enough detail to diagnose a technical issue?",
                "answer": (
                    "Request browser/application logs and exact reproduction steps, then attach "
                    "them to the ticket for engineering follow-up."
                ),
                "source": "builtin_fallback",
            },
        ],
        ClassificationCategory.BILLING.value: [
            {
                "question": "A customer is disputing a charge on their invoice.",
                "answer": (
                    "Verify the charge against the account's subscription history. If it appears "
                    "to be in error, start the refund/credit process per policy."
                ),
                "source": "builtin_fallback",
            },
            {
                "question": "How long do refunds take to process?",
                "answer": "Approved refunds post to the original payment method within 5-10 business days.",
                "source": "builtin_fallback",
            },
        ],
        ClassificationCategory.ACCOUNT.value: [
            {
                "question": "A customer can't log in or has lost account access.",
                "answer": (
                    "Confirm identity, then send a password reset link. If MFA is blocking access, "
                    "verify identity through a secondary channel before disabling it."
                ),
                "source": "builtin_fallback",
            },
        ],
        ClassificationCategory.GENERAL.value: [
            {
                "question": "General inquiry that doesn't fit a specific category.",
                "answer": (
                    "Acknowledge the request, clarify what the customer needs, and route to the "
                    "appropriate team if necessary."
                ),
                "source": "builtin_fallback",
            },
        ],
        ClassificationCategory.FEATURE_REQUEST.value: [
            {
                "question": "A customer is requesting a new feature.",
                "answer": (
                    "Thank the customer, log the request in the product feedback tracker, and let "
                    "them know there's no committed timeline but the input is valued."
                ),
                "source": "builtin_fallback",
            },
        ],
        ClassificationCategory.BUG_REPORT.value: [
            {
                "question": "A customer reports unexpected/broken behavior.",
                "answer": (
                    "Confirm reproduction steps, check for existing known issues, and file a bug "
                    "report with engineering if it's new."
                ),
                "source": "builtin_fallback",
            },
        ],
    }

    def __init__(self, mcp_registry: Optional[Any] = None, cache_size: int = DEFAULT_CACHE_SIZE):
        """
        Initialize the retriever.

        Args:
            mcp_registry: Optional object exposing
                `.execute_plugin(plugin_name, query)` (see
                backend/integrations/mcp_layer.py's documented contract).
                May be None -- everything still works via local fallbacks.
            cache_size: Max number of distinct (query, top_k) results to
                keep in the in-memory search cache before evicting the
                least-recently-used entry.
        """
        self.mcp_registry = mcp_registry
        self._documents: List[Dict[str, Any]] = []
        # Simple bounded LRU cache: {(query, top_k): List[Dict]}
        self._cache: "OrderedDict[tuple, List[Dict[str, Any]]]" = OrderedDict()
        self._cache_size = max(1, cache_size)

    # ------------------------------------------------------------------
    # Document loading (for the local fallback search)
    # ------------------------------------------------------------------

    def load_documents(self, docs: List[Dict[str, Any]]) -> None:
        """
        Load documents to be searched by the local keyword fallback.

        Each doc should have at least `title`, `content`, and `source`
        keys. Replaces any previously loaded documents and clears the
        search cache, since results may now differ.

        Args:
            docs: List of document dicts.
        """
        self._documents = docs or []
        self._cache.clear()

    # ------------------------------------------------------------------
    # search_knowledge_base
    # ------------------------------------------------------------------

    def search_knowledge_base(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Search the knowledge base for documents relevant to `query`.

        Tries the configured MCP knowledge-base plugin first (if any);
        falls back to local keyword search over documents loaded via
        `load_documents()` otherwise. Results are cached by (query, top_k).

        Args:
            query: Free-text search query.
            top_k: Maximum number of results to return.

        Returns:
            List of dicts, each at least: {"title", "content", "source", "score"}.
        """
        if not query:
            return []

        cache_key = (query, top_k)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        results = self._search_via_mcp(query, top_k)
        if results is None:
            results = self._search_local(query, top_k)

        self._cache_set(cache_key, results)
        return results

    def _search_via_mcp(self, query: str, top_k: int) -> Optional[List[Dict[str, Any]]]:
        """Attempt the MCP path; return None if unavailable/failed so caller can fall back."""
        raw = self._call_mcp_plugin({"query": query, "top_k": top_k})
        if raw is None:
            return None
        return self._normalize_mcp_results(raw)[:top_k]

    def _call_mcp_plugin(self, query_payload: Dict[str, Any]) -> Optional[Any]:
        """
        Call the configured MCP registry's `execute_plugin`, guarded so a
        missing/incomplete MCP layer never crashes this class.

        Returns the raw plugin result, or None if no registry is
        configured, the plugin/method doesn't exist yet, or the call
        raised for any reason.
        """
        if self.mcp_registry is None:
            return None
        try:
            return self.mcp_registry.execute_plugin("knowledge_base", query_payload)
        except (ImportError, AttributeError) as e:
            logger.info(f"MCP knowledge_base plugin not available yet ({e}); using local fallback")
            return None
        except Exception as e:  # noqa: BLE001 - never let a flaky plugin break retrieval
            logger.warning(f"MCP knowledge_base plugin call failed ({e}); using local fallback")
            return None

    @staticmethod
    def _normalize_mcp_results(raw: Any) -> List[Dict[str, Any]]:
        """Normalize an arbitrary MCP plugin response into our documented shape."""
        if raw is None:
            return []

        if isinstance(raw, dict):
            for key in ("results", "documents", "items", "data"):
                value = raw.get(key)
                if isinstance(value, list):
                    raw = value
                    break
            else:
                raw = [raw]

        if not isinstance(raw, list):
            logger.warning(f"Unexpected MCP knowledge_base result type ({type(raw)}); ignoring")
            return []

        normalized = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            score = item.get("score", item.get("relevance", 0.0))
            try:
                score = float(score) if score is not None else 0.0
            except (TypeError, ValueError):
                score = 0.0
            normalized.append({
                "title": item.get("title") or item.get("name") or "Untitled",
                "content": item.get("content") or item.get("snippet") or item.get("text") or "",
                "source": item.get("source") or item.get("url") or "mcp_knowledge_base",
                "score": score,
            })

        normalized.sort(key=lambda d: d["score"], reverse=True)
        return normalized

    def _search_local(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """
        Local fallback: simple keyword-overlap scoring over documents
        loaded via `load_documents()`. No ML/embedding dependency -- this
        is a fallback, not the real production search path.
        """
        if not self._documents:
            return []

        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        scored = []
        for doc in self._documents:
            haystack = f"{doc.get('title', '')} {doc.get('content', '')}"
            doc_terms = self._tokenize(haystack)
            if not doc_terms:
                continue
            overlap = sum(1 for term in query_terms if term in doc_terms)
            if overlap == 0:
                continue
            score = round(overlap / len(query_terms), 3)
            scored.append({
                "title": doc.get("title", "Untitled"),
                "content": doc.get("content", ""),
                "source": doc.get("source", "local_fallback"),
                "score": score,
            })

        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:top_k]

    # ------------------------------------------------------------------
    # get_similar_tickets
    # ------------------------------------------------------------------

    def get_similar_tickets(
        self,
        ticket: Ticket,
        candidate_tickets: Optional[List[Ticket]] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Find tickets similar to `ticket` among `candidate_tickets`.

        There is no ticket store/DB yet (separate Phase 2 work), so this
        never looks anything up on its own: if `candidate_tickets` is
        None (or empty), it returns `[]`. When candidates are supplied,
        similarity is a simple heuristic combining subject/description
        keyword overlap (Jaccard) with a bonus for sharing the same
        classification category.

        Args:
            ticket: The ticket to find matches for.
            candidate_tickets: Tickets to compare against, or None.
            top_k: Maximum number of results to return.

        Returns:
            List of {"ticket_id", "similarity_score", "subject"} dicts,
            sorted by similarity_score descending.
        """
        if not candidate_tickets:
            return []

        ticket_terms = set(self._tokenize(f"{ticket.subject} {ticket.description}"))
        ticket_category = getattr(ticket.ai_context, "classification", None)

        scored = []
        for candidate in candidate_tickets:
            if candidate is ticket or getattr(candidate, "id", None) == getattr(ticket, "id", None):
                continue

            candidate_terms = set(self._tokenize(f"{candidate.subject} {candidate.description}"))
            union = ticket_terms | candidate_terms
            jaccard = len(ticket_terms & candidate_terms) / len(union) if union else 0.0

            candidate_category = getattr(candidate.ai_context, "classification", None)
            category_bonus = (
                0.2 if ticket_category and candidate_category and ticket_category == candidate_category else 0.0
            )

            score = round(min(jaccard + category_bonus, 1.0), 3)
            if score <= 0:
                continue

            scored.append({
                "ticket_id": candidate.id,
                "similarity_score": score,
                "subject": candidate.subject,
            })

        scored.sort(key=lambda d: d["similarity_score"], reverse=True)
        return scored[:top_k]

    # ------------------------------------------------------------------
    # retrieve_for_classification
    # ------------------------------------------------------------------

    def retrieve_for_classification(
        self, category: Union[str, ClassificationCategory], top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Retrieve FAQ-style items relevant to a classification category.

        Tries the configured MCP knowledge-base plugin first (if any);
        falls back to a small built-in set of example FAQ entries per
        `ClassificationCategory`, each tagged `"source": "builtin_fallback"`
        so it's never mistaken for real KB content.

        Args:
            category: A `ClassificationCategory` value or its raw string
                (e.g. "technical"), typically from
                `TicketProcessor.classify_ticket()`.
            top_k: Maximum number of results to return.

        Returns:
            List of FAQ item dicts, each at least {"question", "answer", "source"}.
        """
        category_value = category.value if isinstance(category, ClassificationCategory) else str(category)

        mcp_raw = self._call_mcp_plugin({"category": category_value, "mode": "faq", "top_k": top_k})
        if mcp_raw is not None:
            return self._normalize_mcp_results(mcp_raw)[:top_k]

        return self._builtin_faq_fallback(category_value)[:top_k]

    def _builtin_faq_fallback(self, category_value: str) -> List[Dict[str, Any]]:
        items = self._BUILTIN_FAQ.get(category_value, [])
        # Nothing to rank against a query here -- preserve curated order,
        # but still expose a relevance score for downstream consumers/tests
        # that expect ranked results.
        return [
            {**item, "relevance_score": round(1.0 - (idx * 0.05), 3)}
            for idx, item in enumerate(items)
        ]

    # ------------------------------------------------------------------
    # Cache helpers (bounded, simple LRU-by-insertion-order)
    # ------------------------------------------------------------------

    def _cache_get(self, key: tuple) -> Optional[List[Dict[str, Any]]]:
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)
        return self._cache[key]

    def _cache_set(self, key: tuple, value: List[Dict[str, Any]]) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)  # evict least-recently-used

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Lowercase alphanumeric tokenizer used by all local heuristics."""
        if not text:
            return []
        return _TOKEN_RE.findall(text.lower())
