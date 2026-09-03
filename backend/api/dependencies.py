"""FastAPI dependencies for the API layer.

Currently provides an in-memory ``TicketStore``. There is no database layer
yet (a separate parallel PR is adding SQLAlchemy models), so this is a
deliberately small placeholder: it keeps tickets in a process-local dict and
exposes the narrow interface (``get`` / ``save`` / ``list``) that a real
DB-backed store would also need to satisfy. When the DB layer lands, this
class can be swapped for one backed by a session/repository without touching
route handlers, since they only ever depend on ``get_ticket_store()``.

Using FastAPI's ``Depends`` (rather than a bare module-level singleton
imported directly by routes) also means tests can override
``get_ticket_store`` with a fresh, isolated store per test instead of sharing
global state across the test suite.
"""
from threading import Lock
from typing import Dict, List, Optional

from backend.models.ticket import Ticket


class TicketStore:
    """A minimal in-memory ticket store.

    NOTE: This is a Phase 2 placeholder. It is not persistent (data is lost
    on process restart) and offers no concurrency guarantees beyond a simple
    lock around mutations. It exists purely to unblock the API layer ahead
    of the real database-backed implementation.
    """

    def __init__(self) -> None:
        self._tickets: Dict[str, Ticket] = {}
        self._lock = Lock()

    def get(self, ticket_id: str) -> Optional[Ticket]:
        return self._tickets.get(ticket_id)

    def save(self, ticket: Ticket) -> None:
        with self._lock:
            self._tickets[ticket.id] = ticket

    def list(self) -> List[Ticket]:
        return list(self._tickets.values())

    def delete(self, ticket_id: str) -> None:
        with self._lock:
            self._tickets.pop(ticket_id, None)


# Process-wide default store used by the running application. Tests should
# NOT rely on this directly -- override the `get_ticket_store` dependency
# instead so each test gets an isolated store.
_default_store = TicketStore()


def get_ticket_store() -> TicketStore:
    """FastAPI dependency that yields the shared ticket store.

    Override this with ``app.dependency_overrides[get_ticket_store] = ...``
    in tests to inject a fresh ``TicketStore`` per test.
    """
    return _default_store
