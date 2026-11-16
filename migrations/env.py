from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import Settings
from app.core.database import database_url


def run_offline() -> None:
    # No credentials required to inspect the migration's SQL.
    context.configure(
        dialect_name="postgresql", literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(
        database_url(Settings()),
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c lock_timeout=5000 -c statement_timeout=30000",
        },
    )
    try:
        with engine.connect() as connection:
            context.configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
