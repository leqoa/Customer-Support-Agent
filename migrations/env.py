"""Alembic environment script.

Wires Alembic up to `backend.models.db.base.Base.metadata` and, critically,
to the same `DATABASE_URL` environment variable the application itself reads
-- so `alembic upgrade head` always targets whatever database the app would
actually connect to (SQLite locally by default, Postgres in prod), without
needing a hardcoded URL in `alembic.ini`.
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the repo root importable regardless of the cwd `alembic` is invoked
# from, so `backend.models.db...` resolves the same way it does for the app.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.models.db.base import Base, DATABASE_URL  # noqa: E402
import backend.models.db.models  # noqa: E402,F401 -- registers ORM classes on Base.metadata

# this is the Alembic Config object, which provides access to values within
# the .ini file in use.
config = context.config

# Prefer the DATABASE_URL environment variable (falling back to the same
# default backend.models.db.base uses) over whatever is in alembic.ini.
config.set_main_option("sqlalchemy.url", os.environ.get("DATABASE_URL", DATABASE_URL))

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though an
    Engine is acceptable here as well. By skipping the Engine creation we
    don't even need a DBAPI to be available.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a connection
    with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
