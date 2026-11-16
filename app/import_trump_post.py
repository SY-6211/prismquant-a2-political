"""Explicit single-post import. Save the record and archive snapshot atomically."""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.common import DefusedXmlException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.database import database_url
from app.extract_trump_post import PARSER_VERSION, find_target, prepare_extraction
from app.preview_ecb import published_utc
from app.preview_trump import PERSON_ID, SOURCE_ID, TRUMP_FEED_URL, fetch_feed_items, load_source, preview_link
from app.preview_trump_post import archive_url, fetch_post


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaNote(DraftModel):
    position: StrictInt = Field(ge=0)
    kind: Literal["archive_image_description", "archive_transcript", "unavailable"]
    label: str = Field(min_length=1)
    text: str | None
    attachment_ref: str | None
    owner_archive_url: None
    association_status: Literal["unresolved"]

    @model_validator(mode="after")
    def check_text(self):
        if self.kind == "unavailable":
            if self.text is not None:
                raise ValueError("Unavailable media must not contain fabricated text")
        elif self.text is None or not self.text.strip():
            raise ValueError("Media text is required")
        return self


class RecordDraft(DraftModel):
    source_id: UUID
    person_id: UUID
    title: str
    content_type: Literal["public_statement"]
    provenance_type: Literal["third_party_archive"]
    verification_status: Literal["archive_only"]
    original_url: str
    retrieved_url: str
    published_at: AwareDatetime | None
    published_raw: str | None
    page_date: None
    language: None
    body_text: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    speaker_evidence: str

    @field_validator("title", "body_text", "speaker_evidence")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Nonblank record text required")
        return value

    @field_validator("retrieved_url")
    @classmethod
    def normalize_archive_url(cls, value: str) -> str:
        return archive_url(value)

    @field_validator("original_url")
    @classmethod
    def validate_original(cls, value: str) -> str:
        normalized = preview_link(value, original=True)
        if not normalized or not re.fullmatch(
            r"https://(?:www\.)?truthsocial\.com/@realDonaldTrump/(?:posts/)?\d+", normalized,
        ):
            raise ValueError("Expected archive-reported Trump original address")
        return normalized

    @field_validator("published_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value else None

    @model_validator(mode="after")
    def check_body_and_time(self):
        if self.content_hash != hashlib.sha256(self.body_text.encode("utf-8")).hexdigest():
            raise ValueError("Body hash does not match full text")
        if published_utc(self.published_raw) != utc_text(self.published_at):
            raise ValueError("RSS publication timestamp does not match raw value")
        return self


class ArchiveDraft(DraftModel):
    archive_author: Literal["Donald J. Trump"]
    archive_handle: Literal["@realDonaldTrump"]
    archive_post_type: Literal["original"]
    archive_removed_raw: str | None
    title_origin: Literal["archive_rss", "archive_listing"]
    page_post_published_at: AwareDatetime | None
    page_post_published_raw: str | None
    archive_captured_at: AwareDatetime | None
    archive_capture_raw: str | None
    image_count: StrictInt = Field(ge=0)
    video_count: StrictInt = Field(ge=0)
    inline_quotes: list[dict] = Field(max_length=0)
    quoted_posts: list[dict] = Field(max_length=0)
    archive_media_notes: list[MediaNote] = Field(max_length=100)
    parser_version: Literal[PARSER_VERSION]

    @field_validator("page_post_published_at", "archive_captured_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value else None

    @model_validator(mode="after")
    def check_times_and_order(self):
        for value, raw in ((self.page_post_published_at, self.page_post_published_raw),
                           (self.archive_captured_at, self.archive_capture_raw)):
            if published_utc(raw) != utc_text(value):
                raise ValueError("Archive timestamp does not match raw value")
        if [note.position for note in self.archive_media_notes] != list(range(len(self.archive_media_notes))):
            raise ValueError("Media note positions must preserve page order")
        return self


def utc_text(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def snapshot_hash(record: RecordDraft, archive: ArchiveDraft) -> str:
    payload = {
        "snapshot_format": "trump_archive_snapshot_v1",
        "record": record.model_dump(mode="json"), "archive_post": archive.model_dump(mode="json"),
    }
    value = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_archive(connection: Connection, record_id: UUID, archive: ArchiveDraft, digest: str) -> None:
    values = archive.model_dump()
    for name in ("inline_quotes", "quoted_posts", "archive_media_notes"):
        values[name] = json.dumps(values[name], ensure_ascii=False, allow_nan=False)
    values.update(record_id=record_id, snapshot_hash=digest)
    connection.execute(text(
        "INSERT INTO archive_post_details (record_id, archive_author, archive_handle, archive_post_type, "
        "archive_removed_raw, title_origin, page_post_published_at, page_post_published_raw, "
        "archive_captured_at, archive_capture_raw, image_count, video_count, inline_quotes, quoted_posts, "
        "archive_media_notes, parser_version, snapshot_hash) VALUES (:record_id, :archive_author, "
        ":archive_handle, :archive_post_type, :archive_removed_raw, :title_origin, :page_post_published_at, "
        ":page_post_published_raw, :archive_captured_at, :archive_capture_raw, :image_count, :video_count, "
        "CAST(:inline_quotes AS jsonb), CAST(:quoted_posts AS jsonb), CAST(:archive_media_notes AS jsonb), "
        ":parser_version, :snapshot_hash)"
    ), values)


def save_post(
    connection: Connection, extraction: dict, first_seen_at: datetime,
    *, source_id: UUID = SOURCE_ID, person_id: UUID = PERSON_ID,
) -> dict:
    """Caller commits the transaction; a savepoint also prevents partial writes if caught."""
    if extraction.get("extraction_complete") is not True or extraction.get("ingestion_eligible") is not True:
        raise ValueError("Complete eligible extraction required")
    record = RecordDraft.model_validate(extraction["record"])
    archive = ArchiveDraft.model_validate(extraction["archive_post"])
    if record.source_id != source_id or record.person_id != person_id:
        raise ValueError("Configured identity mismatch")
    if not isinstance(first_seen_at, datetime) or first_seen_at.utcoffset() is None:
        raise ValueError("First seen time must have a timezone")
    if record.published_at and archive.page_post_published_at and record.published_at != archive.page_post_published_at:
        raise ValueError("Publication timestamps disagree")
    digest = snapshot_hash(record, archive)
    settings = connection.execute(text(
        "SELECT s.feed_url, s.provenance_type, s.fetch_method, s.enabled AS source_enabled, "
        "p.monitoring_enabled, ps.enabled AS link_enabled "
        "FROM sources s JOIN person_sources ps ON ps.source_id=s.source_id "
        "JOIN people p ON p.person_id=ps.person_id "
        "WHERE s.source_id=:source_id AND p.person_id=:person_id FOR SHARE OF s, ps, p"
    ), {"source_id": source_id, "person_id": person_id}).mappings().one_or_none()
    if settings is None or (settings["feed_url"] != TRUMP_FEED_URL
                            or settings["provenance_type"] != "third_party_archive" or settings["fetch_method"] != "rss"):
        raise ValueError("Archive configuration or person association changed")
    values = {**record.model_dump(), "first_seen_at": first_seen_at.astimezone(timezone.utc)}
    with connection.begin_nested():
        identity = connection.execute(text(
            "INSERT INTO public_records (source_id, person_id, title, content_type, provenance_type, "
            "verification_status, original_url, retrieved_url, published_at, published_raw, page_date, "
            "language, body_text, content_hash, speaker_evidence, first_seen_at) VALUES ("
            ":source_id, :person_id, :title, :content_type, :provenance_type, :verification_status, "
            ":original_url, :retrieved_url, :published_at, :published_raw, :page_date, :language, "
            ":body_text, :content_hash, :speaker_evidence, :first_seen_at) "
            "ON CONFLICT (source_id, retrieved_url) DO NOTHING RETURNING record_id"
        ), values).scalar_one_or_none()
        if identity is not None:
            _insert_archive(connection, identity, archive, digest)
            action = "inserted"
        else:
            stored_record = connection.execute(text(
                "SELECT * FROM public_records WHERE source_id=:source_id AND retrieved_url=:retrieved_url FOR UPDATE"
            ), values).mappings().one()
            identity = stored_record["record_id"]
            stored_archive = connection.execute(text(
                "SELECT * FROM archive_post_details WHERE record_id=:id FOR UPDATE"
            ), {"id": identity}).mappings().one_or_none()
            action = "needs_review"
            if stored_archive is not None:
                try:
                    previous_record = RecordDraft.model_validate({key: stored_record[key] for key in RecordDraft.model_fields})
                    previous_archive = ArchiveDraft.model_validate({key: stored_archive[key] for key in ArchiveDraft.model_fields})
                    previous_hash = snapshot_hash(previous_record, previous_archive)
                    if previous_hash == digest == stored_archive["snapshot_hash"]:
                        action = "already_exists"
                except ValueError:
                    pass  # Invalid stored snapshots need review, never automatic repair.
        from app.event_model import snapshot_from_record
        from app.repositories.events import save_snapshot
        event = save_snapshot(
            connection, source_id, 'rss',
            snapshot_from_record(record.model_dump(), archive.model_dump(),
                                 review_status='needs_review' if action == 'needs_review' else 'accepted'),
            first_seen_at, record_id=identity, link_record=action != 'needs_review',
            metadata={'archive_captured_at': utc_text(archive.archive_captured_at),
                      'archive_capture_raw': archive.archive_capture_raw},
        )
    return {
        "status": "needs_review" if action == "needs_review" else "ok", "action": action,
        "saved": action == "inserted", "record_id": str(identity), "title": record.title,
        "body_characters": len(record.body_text), "media_note_count": len(archive.archive_media_notes), **event,
        **{key: settings[key] for key in ("source_enabled", "monitoring_enabled", "link_enabled")},
        "message": {
            "inserted": "主帖文字及归档详情已一起保存。",
            "already_exists": "相同内容及归档详情已存在，未重复添加或修改。",
            "needs_review": "已有内容或归档详情不同、不完整或哈希不符，需人工核对；原记录未覆盖。",
        }[action],
    }


def main() -> int:
    engine = None
    try:
        settings = Settings()
        load_source(settings)
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
            headers={"User-Agent": "PrismQuant-FYP/0.1 (manual single archive import)"},
        ) as client:
            candidate = find_target(fetch_feed_items(client))
            if candidate is None:
                print("当前 RSS 未找到已确认的目标帖子，已停止；未改选其他帖子或保存内容。")
                return 2
            extraction = prepare_extraction(fetch_post(client, candidate, include_full=True), candidate)
        if not extraction["extraction_complete"] or not extraction["ingestion_eligible"]:
            print("帖子内容不完整、标注需核对或不符合首版入库范围；未保存内容。")
            return 2
        engine = create_engine(
            database_url(settings), poolclass=NullPool, hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-c lock_timeout=5000 -c statement_timeout=10000"},
        )
        with engine.begin() as connection:
            result = save_post(connection, extraction, datetime.now(timezone.utc))
    except httpx.TimeoutException:
        print("第三方归档请求超时，入库未完成；未保存内容。")
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"第三方归档返回 HTTP {exc.response.status_code}，入库未完成；未保存内容。")
        return 1
    except httpx.RequestError:
        print("无法连接第三方归档站，请检查网络；未保存内容。")
        return 1
    except (ValueError, ParseError, DefusedXmlException):
        print("来源、人物关联或完整帖子数据不符合入库要求；未保存内容。")
        return 1
    except Exception:
        print("特朗普单帖入库失败，请检查数据库、权限及表结构；未显示连接凭证。")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"mode": "manual_single_archive_import", **result}, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "needs_review" else 0


if __name__ == "__main__":
    raise SystemExit(main())
