from uuid import UUID

from sqlalchemy import Connection, text

from app.core.config import Settings
from app.core.database import create_read_engine


def select_sources(connection: Connection, limit: int, cursor: UUID | None) -> list[dict]:
    # Page sources before aggregating their people. Shared feeds must not be
    # split into multiple source rows or lose associated people at page edges.
    predicate = "WHERE source_id > :cursor" if cursor is not None else ""
    query = text(
        "WITH page AS (SELECT source_id, name, publisher_name, provenance_type, "
        "fetch_method, feed_url, homepage_url, poll_interval_seconds, enabled, "
        "created_at, updated_at FROM sources " + predicate +
        " ORDER BY source_id ASC LIMIT :fetch_limit) "
        "SELECT s.*, COALESCE((SELECT jsonb_agg(jsonb_build_object("
        "'person_id', p.person_id, 'name_original', p.name_original, 'name_zh', p.name_zh, "
        "'monitoring_enabled', p.monitoring_enabled, 'link_enabled', ps.enabled) "
        "ORDER BY p.person_id) FROM person_sources ps JOIN people p USING(person_id) "
        "WHERE ps.source_id=s.source_id), '[]'::jsonb) AS people "
        "FROM page s ORDER BY s.source_id ASC"
    )
    parameters = {"fetch_limit": limit + 1}
    if cursor is not None:
        parameters["cursor"] = cursor
    return [dict(row) for row in connection.execute(query, parameters).mappings()]


def read_sources(settings: Settings, limit: int, cursor: UUID | None) -> list[dict]:
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            return select_sources(connection, limit, cursor)
    finally:
        engine.dispose()
