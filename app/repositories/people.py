from uuid import UUID

from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_read_engine


def read_people(settings: Settings, limit: int, cursor: UUID | None) -> list[dict]:
    engine = create_read_engine(settings)
    # Only fixed SQL fragments are joined; values are always bound parameters.
    predicate = "WHERE person_id > :cursor" if cursor is not None else ""
    query = text(
        "SELECT person_id, name_original, name_zh, aliases, country_codes, "
        "monitoring_enabled, created_at, updated_at FROM people "
        + predicate + " ORDER BY person_id ASC LIMIT :fetch_limit"
    )
    parameters = {"fetch_limit": limit + 1}
    if cursor is not None:
        parameters["cursor"] = cursor
    try:
        with engine.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters).mappings()]
    finally:
        engine.dispose()


def read_person(settings: Settings, person_id: UUID) -> dict | None:
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT person_id, name_original, name_zh, aliases, country_codes, "
                    "monitoring_enabled, created_at, updated_at FROM people "
                    "WHERE person_id = :person_id"
                ),
                {"person_id": person_id},
            ).mappings().one_or_none()
            return dict(row) if row is not None else None
    finally:
        engine.dispose()
