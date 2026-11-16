from uuid import UUID

from sqlalchemy import Connection, text

from app.core.config import Settings
from app.core.database import create_read_engine


def select_public_records(
    connection: Connection, limit: int, cursor: UUID | None, person_id: UUID | None,
    order: str = "record_id",
) -> list[dict]:
    if order not in {"record_id", "published_desc"}:
        raise ValueError("Unsupported content order")
    predicates = []
    parameters = {"fetch_limit": limit + 1}
    if cursor is not None:
        if order == "published_desc":
            anchor_filter = " AND a.person_id = :person_id" if person_id is not None else ""
            predicates.append(
                "(COALESCE(r.published_at, '-infinity'::timestamptz), r.record_id) < "
                "(SELECT COALESCE(a.published_at, '-infinity'::timestamptz), a.record_id "
                "FROM public_records a WHERE a.record_id = :cursor" + anchor_filter + ")"
            )
        else:
            predicates.append("r.record_id > :cursor")
        parameters["cursor"] = cursor
    if person_id is not None:
        predicates.append("r.person_id = :person_id")
        parameters["person_id"] = person_id
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    # Keep full bodies and extraction evidence out of the list query entirely.
    # Category and verification describe the saved record, not current settings.
    query = text(
        "SELECT r.record_id, r.source_id, r.person_id, r.title, r.content_type, "
        "r.provenance_type, r.original_url, r.retrieved_url, r.published_at, r.page_date, "
        "r.first_seen_at, r.language, r.verification_status, r.created_at, "
        "s.name AS source_name, s.publisher_name, "
        "p.name_original AS person_name_original, p.name_zh AS person_name_zh "
        "FROM public_records r JOIN sources s ON s.source_id=r.source_id "
        "LEFT JOIN people p ON p.person_id=r.person_id" + where +
        (" ORDER BY COALESCE(r.published_at, '-infinity'::timestamptz) DESC, r.record_id DESC"
         if order == "published_desc" else " ORDER BY r.record_id ASC") + " LIMIT :fetch_limit"
    )
    return [dict(row) for row in connection.execute(query, parameters).mappings()]


def read_public_records(
    settings: Settings, limit: int, cursor: UUID | None, person_id: UUID | None,
    order: str = "record_id",
) -> list[dict]:
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            return select_public_records(connection, limit, cursor, person_id, order)
    finally:
        engine.dispose()


def select_public_record(connection: Connection, record_id: UUID) -> dict | None:
    row = connection.execute(text(
        "SELECT r.record_id, r.source_id, r.person_id, r.title, r.content_type, "
        "r.provenance_type, r.original_url, r.retrieved_url, r.published_at, r.page_date, "
        "r.first_seen_at, r.language, r.verification_status, r.created_at, "
        "r.body_text, r.speaker_evidence, r.content_hash, r.published_raw, "
        "s.name AS source_name, s.publisher_name, "
        "p.name_original AS person_name_original, p.name_zh AS person_name_zh, "
        "CASE WHEN a.record_id IS NULL THEN NULL ELSE to_jsonb(a) END AS archive_post "
        "FROM public_records r JOIN sources s ON s.source_id=r.source_id "
        "LEFT JOIN people p ON p.person_id=r.person_id "
        "LEFT JOIN archive_post_details a ON a.record_id=r.record_id WHERE r.record_id=:record_id"
    ), {"record_id": record_id}).mappings().one_or_none()
    return dict(row) if row is not None else None


def read_public_record(settings: Settings, record_id: UUID) -> dict | None:
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            return select_public_record(connection, record_id)
    finally:
        engine.dispose()
