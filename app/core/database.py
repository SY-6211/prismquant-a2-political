"""Database URL construction; importing this module does not connect."""

from sqlalchemy import URL, create_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings


def database_url(settings: Settings) -> URL:
    if not settings.db_password.get_secret_value():
        raise ValueError("请先在 .env 中配置数据库密码。")
    return URL.create(
        "postgresql+psycopg",
        username=settings.db_user,
        password=settings.db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )


def create_read_engine(settings: Settings):
    return create_engine(
        database_url(settings), poolclass=NullPool, hide_parameters=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c default_transaction_read_only=on -c statement_timeout=5000",
        },
    )
