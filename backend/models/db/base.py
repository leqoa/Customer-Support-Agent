"""SQLAlchemy engine/session setup for the ITSS persistence layer.

The database backend is controlled entirely by the ``DATABASE_URL``
environment variable so that:

- Local development can run against a zero-config SQLite file
  (``sqlite:///./itss_dev.db``, the default when ``DATABASE_URL`` is unset).
- Production/staging can point at Postgres per ``config/settings.yaml``'s
  ``database.type: "postgresql"`` setting, e.g.::

      DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/itss

Nothing here talks to the database at import time beyond constructing the
(lazy) ``Engine`` object, so importing this module is always safe -- in
tests, tooling, or Alembic's ``env.py``.
"""
import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Default to a local SQLite file so `pip install -r requirements.txt` and go
# is enough for local development -- no Postgres server required.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./itss_dev.db")

# SQLite connections are only safe to use from the thread that created them
# by default; FastAPI (and most WSGI/ASGI servers) may hand a request-scoped
# session to a worker thread, so relax that check for SQLite specifically.
# Real client/server databases (Postgres, etc.) neither need nor accept this
# argument.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)

# autocommit=False / autoflush=False is the standard FastAPI-recommended
# configuration: each unit of work is explicit (commit()/rollback()) and
# queries never implicitly flush half-finished changes.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """FastAPI-style dependency that yields a `Session` and always closes it.

    This module doesn't wire itself into the API layer (that's built in a
    separate, parallel piece of work), but this is the standard shape that
    layer can plug in directly, e.g.::

        from fastapi import Depends
        from backend.models.db.base import get_db

        @app.get("/tickets/{ticket_id}")
        def read_ticket(ticket_id: str, db: Session = Depends(get_db)):
            return TicketRepository(db).get_by_id(ticket_id)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
