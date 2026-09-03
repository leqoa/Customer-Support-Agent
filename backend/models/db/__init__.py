"""Database/persistence layer for ITSS.

This package adds a SQLAlchemy-based persistence layer on top of the plain
dataclasses in ``backend.models.ticket`` (``Ticket``, ``CustomerInfo``,
``TicketContext``, ``AiDraft``, ``EscalationInfo``) WITHOUT modifying those
dataclasses. Other modules keep working with them exactly as before; this
package is purely additive.

Modules:
- ``base``: engine/session setup, driven by the ``DATABASE_URL`` env var.
- ``models``: SQLAlchemy ORM classes (``TicketORM``, ``CustomerORM``,
  ``AiDraftORM``, ``EscalationInfoORM``).
- ``converters``: two-way functions between dataclasses and ORM rows.
- ``repository``: ``TicketRepository``, a small persistence-facing API the
  (separately built) HTTP layer can swap in for its in-memory ticket store.
"""
