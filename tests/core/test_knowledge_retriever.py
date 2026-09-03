"""Unit tests for backend.core.knowledge_retriever.KnowledgeRetriever.

No real network calls are made anywhere in these tests -- the MCP path is
exercised only via an in-process Mock, and the fallback path is entirely
local/in-memory by design.
"""
from unittest.mock import Mock

import pytest

from backend.core.knowledge_retriever import KnowledgeRetriever
from backend.core.ticket_processor import ClassificationCategory
from backend.models.ticket import CustomerInfo, Ticket, TicketContext


def make_ticket(ticket_id="T-1", subject="", description="", classification=None):
    ticket = Ticket(
        id=ticket_id,
        subject=subject,
        description=description,
        customer=CustomerInfo(id="C-1", name="Jane Doe", email="jane@example.com"),
    )
    ticket.ai_context = TicketContext(classification=classification)
    return ticket


SAMPLE_DOCS = [
    {
        "title": "How to reset your password",
        "content": "Go to account settings and click reset password to receive a reset link by email.",
        "source": "kb/account/password-reset",
    },
    {
        "title": "Understanding your invoice",
        "content": "Invoices are generated monthly and include subscription charges and applicable taxes.",
        "source": "kb/billing/invoice",
    },
    {
        "title": "Troubleshooting login errors",
        "content": "If login fails, check for typos, confirm the account isn't locked, and reset the password.",
        "source": "kb/account/login-errors",
    },
]


class TestLocalFallbackSearch:
    def test_returns_ranked_relevant_results(self):
        retriever = KnowledgeRetriever()
        retriever.load_documents(SAMPLE_DOCS)

        results = retriever.search_knowledge_base("password reset login", top_k=5)

        assert len(results) > 0
        titles = [r["title"] for r in results]
        # Both password/login docs should be relevant; invoice doc should not be first.
        assert "How to reset your password" in titles
        assert "Troubleshooting login errors" in titles
        # Ranked descending by score.
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_truncation(self):
        retriever = KnowledgeRetriever()
        retriever.load_documents(SAMPLE_DOCS)

        results = retriever.search_knowledge_base("account password login invoice", top_k=1)

        assert len(results) == 1

    def test_no_documents_returns_empty(self):
        retriever = KnowledgeRetriever()
        assert retriever.search_knowledge_base("anything") == []

    def test_empty_query_returns_empty(self):
        retriever = KnowledgeRetriever()
        retriever.load_documents(SAMPLE_DOCS)
        assert retriever.search_knowledge_base("") == []

    def test_result_shape(self):
        retriever = KnowledgeRetriever()
        retriever.load_documents(SAMPLE_DOCS)
        results = retriever.search_knowledge_base("password")
        for r in results:
            assert {"title", "content", "source", "score"} <= set(r.keys())


class TestMcpRegistryPathNone:
    def test_mcp_registry_none_end_to_end(self):
        """mcp_registry=None should never raise and should use local fallback cleanly."""
        retriever = KnowledgeRetriever(mcp_registry=None)
        retriever.load_documents(SAMPLE_DOCS)

        results = retriever.search_knowledge_base("password")

        assert isinstance(results, list)
        assert len(results) > 0


class TestMcpRegistryMocked:
    def test_mocked_mcp_used_and_normalized(self):
        mock_registry = Mock()
        mock_registry.execute_plugin.return_value = [
            {"title": "MCP Doc A", "content": "Content A", "source": "confluence", "score": 0.9},
            {"title": "MCP Doc B", "content": "Content B", "source": "confluence", "score": 0.4},
        ]

        retriever = KnowledgeRetriever(mcp_registry=mock_registry)
        results = retriever.search_knowledge_base("anything", top_k=5)

        mock_registry.execute_plugin.assert_called_once_with(
            "knowledge_base", {"query": "anything", "top_k": 5}
        )
        assert len(results) == 2
        assert results[0]["title"] == "MCP Doc A"  # ranked, higher score first
        assert results[0]["score"] == 0.9
        assert results[1]["title"] == "MCP Doc B"

    def test_mcp_dict_shape_is_normalized(self):
        mock_registry = Mock()
        mock_registry.execute_plugin.return_value = {
            "results": [{"title": "Doc", "content": "C", "source": "notion", "relevance": 0.7}]
        }

        retriever = KnowledgeRetriever(mcp_registry=mock_registry)
        results = retriever.search_knowledge_base("q")

        assert len(results) == 1
        assert results[0]["title"] == "Doc"
        assert results[0]["score"] == 0.7

    def test_mcp_missing_method_falls_back_without_crashing(self):
        """AttributeError from an incomplete MCP layer must never propagate."""
        class IncompleteRegistry:
            pass  # no execute_plugin at all

        retriever = KnowledgeRetriever(mcp_registry=IncompleteRegistry())
        retriever.load_documents(SAMPLE_DOCS)

        results = retriever.search_knowledge_base("password")

        assert len(results) > 0
        assert results[0]["source"] != "mcp_knowledge_base"

    def test_mcp_raising_exception_falls_back_without_crashing(self):
        mock_registry = Mock()
        mock_registry.execute_plugin.side_effect = RuntimeError("plugin exploded")

        retriever = KnowledgeRetriever(mcp_registry=mock_registry)
        retriever.load_documents(SAMPLE_DOCS)

        results = retriever.search_knowledge_base("password")

        assert len(results) > 0  # fell back to local search instead of raising


class TestGetSimilarTickets:
    def test_no_candidates_returns_empty(self):
        retriever = KnowledgeRetriever()
        ticket = make_ticket(subject="Cannot log in", description="Login is broken")
        assert retriever.get_similar_tickets(ticket, candidate_tickets=None) == []
        assert retriever.get_similar_tickets(ticket, candidate_tickets=[]) == []

    def test_ranks_candidates_sensibly(self):
        retriever = KnowledgeRetriever()
        ticket = make_ticket(
            ticket_id="T-1",
            subject="Cannot log in to account",
            description="Login fails with an error after password reset",
            classification=ClassificationCategory.ACCOUNT.value,
        )
        close_match = make_ticket(
            ticket_id="T-2",
            subject="Cannot log in",
            description="Login fails with an error, password reset did not help",
            classification=ClassificationCategory.ACCOUNT.value,
        )
        unrelated = make_ticket(
            ticket_id="T-3",
            subject="Invoice question",
            description="Why was I charged twice for this month's subscription",
            classification=ClassificationCategory.BILLING.value,
        )

        results = retriever.get_similar_tickets(ticket, candidate_tickets=[unrelated, close_match], top_k=5)

        assert len(results) >= 1
        assert results[0]["ticket_id"] == "T-2"
        assert results[0]["similarity_score"] > 0
        assert "subject" in results[0]
        # Sorted descending.
        scores = [r["similarity_score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_excludes_self(self):
        retriever = KnowledgeRetriever()
        ticket = make_ticket(ticket_id="T-1", subject="Same ticket", description="Same ticket")
        results = retriever.get_similar_tickets(ticket, candidate_tickets=[ticket])
        assert results == []


class TestRetrieveForClassification:
    def test_builtin_fallback_tagged_correctly(self):
        retriever = KnowledgeRetriever()
        results = retriever.retrieve_for_classification(ClassificationCategory.BILLING.value)

        assert len(results) > 0
        for item in results:
            assert item["source"] == "builtin_fallback"

    def test_accepts_enum_or_string_category(self):
        retriever = KnowledgeRetriever()
        by_enum = retriever.retrieve_for_classification(ClassificationCategory.TECHNICAL)
        by_str = retriever.retrieve_for_classification("technical")
        assert by_enum == by_str

    def test_unknown_category_returns_empty(self):
        retriever = KnowledgeRetriever()
        assert retriever.retrieve_for_classification("not_a_real_category") == []

    def test_top_k_truncation(self):
        retriever = KnowledgeRetriever()
        results = retriever.retrieve_for_classification(ClassificationCategory.TECHNICAL.value, top_k=1)
        assert len(results) == 1

    def test_mcp_path_used_when_available(self):
        mock_registry = Mock()
        mock_registry.execute_plugin.return_value = [
            {"title": "Real FAQ", "content": "Real answer", "source": "confluence", "score": 0.95}
        ]
        retriever = KnowledgeRetriever(mcp_registry=mock_registry)

        results = retriever.retrieve_for_classification(ClassificationCategory.BILLING.value)

        mock_registry.execute_plugin.assert_called_once()
        assert results[0]["source"] == "confluence"


class TestCache:
    def test_repeated_identical_search_only_stores_once(self):
        retriever = KnowledgeRetriever()
        retriever.load_documents(SAMPLE_DOCS)

        retriever.search_knowledge_base("password", top_k=5)
        retriever.search_knowledge_base("password", top_k=5)

        assert len(retriever._cache) == 1

    def test_different_queries_cache_separately(self):
        retriever = KnowledgeRetriever()
        retriever.load_documents(SAMPLE_DOCS)

        retriever.search_knowledge_base("password")
        retriever.search_knowledge_base("invoice")

        assert len(retriever._cache) == 2

    def test_cache_avoids_recomputation(self):
        retriever = KnowledgeRetriever()
        retriever.load_documents(SAMPLE_DOCS)

        original_local_search = retriever._search_local
        call_count = {"n": 0}

        def counting_search(*args, **kwargs):
            call_count["n"] += 1
            return original_local_search(*args, **kwargs)

        retriever._search_local = counting_search

        retriever.search_knowledge_base("password", top_k=5)
        retriever.search_knowledge_base("password", top_k=5)

        assert call_count["n"] == 1

    def test_cache_eviction_bounded(self):
        retriever = KnowledgeRetriever(cache_size=2)
        retriever.load_documents(SAMPLE_DOCS)

        retriever.search_knowledge_base("password")
        retriever.search_knowledge_base("invoice")
        retriever.search_knowledge_base("login")

        assert len(retriever._cache) == 2

    def test_loading_documents_clears_cache(self):
        retriever = KnowledgeRetriever()
        retriever.load_documents(SAMPLE_DOCS)
        retriever.search_knowledge_base("password")
        assert len(retriever._cache) == 1

        retriever.load_documents(SAMPLE_DOCS)
        assert len(retriever._cache) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
