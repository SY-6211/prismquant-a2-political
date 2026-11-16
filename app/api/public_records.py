from datetime import date, datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import ErrorResponse
from app.repositories.public_records import read_public_record, read_public_records

router = APIRouter(prefix="/api/v1", tags=["内容"])


class PublicRecordSummary(BaseModel):
    record_id: UUID
    source_id: UUID
    source_name: str
    publisher_name: str
    person_id: UUID | None
    person_name_original: str | None
    person_name_zh: str | None
    title: str
    content_type: Literal["public_statement", "institution_announcement", "media_quote", "unconfirmed"]
    provenance_type: Literal["official", "third_party_archive"]
    original_url: str | None
    retrieved_url: str
    published_at: AwareDatetime | None
    page_date: date | None
    first_seen_at: AwareDatetime
    language: str | None
    verification_status: Literal["verified_page_attribution", "archive_only", "media_quote_only", "needs_review"]
    created_at: AwareDatetime

    @field_validator("published_at", "first_seen_at", "created_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None


class PublicRecordsListResponse(BaseModel):
    items: list[PublicRecordSummary]
    next_cursor: UUID | None


class ArchiveInlineQuoteResponse(BaseModel):
    position: int = Field(ge=0)
    text: str
    attribution: str | None


class ArchiveQuotedPostResponse(BaseModel):
    position: int = Field(ge=0)
    archive_url: str | None
    parent_archive_url: str | None
    original_url: str | None
    archive_author: str | None
    archive_handle: str | None
    published_at: AwareDatetime | None
    published_raw: str | None
    text: str | None
    image_count: int = Field(ge=0)
    video_count: int = Field(ge=0)
    inline_quotes: list[ArchiveInlineQuoteResponse]

    @field_validator("published_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None


class ArchiveMediaNoteResponse(BaseModel):
    position: int = Field(ge=0)
    kind: Literal["archive_image_description", "archive_transcript", "unavailable", "other_archive_note"]
    label: str | None
    text: str | None
    attachment_ref: str | None
    owner_archive_url: str | None
    association_status: str


class ArchivePostResponse(BaseModel):
    record_id: UUID
    archive_author: str
    archive_handle: str
    archive_post_type: Literal["original", "quote", "retruth"]
    archive_removed_raw: str | None
    title_origin: Literal["archive_rss", "archive_listing"]
    page_post_published_at: AwareDatetime | None
    page_post_published_raw: str | None
    archive_captured_at: AwareDatetime | None
    archive_capture_raw: str | None
    image_count: int = Field(ge=0)
    video_count: int = Field(ge=0)
    inline_quotes: list[ArchiveInlineQuoteResponse]
    quoted_posts: list[ArchiveQuotedPostResponse]
    archive_media_notes: list[ArchiveMediaNoteResponse]
    parser_version: str
    snapshot_hash: str
    created_at: AwareDatetime

    @field_validator("page_post_published_at", "archive_captured_at", "created_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None


class PublicRecordDetail(PublicRecordSummary):
    body_text: str
    speaker_evidence: str | None
    content_hash: str
    published_raw: str | None
    archive_post: ArchivePostResponse | None = None


@router.get(
    "/public-records",
    response_model=PublicRecordsListResponse,
    summary="获取内容列表",
    description=(
        "只读查询已保存的公开内容，包括监控开关关闭时人工导入的记录。"
        "返回标题、人物、来源、时间和归属状态，不返回完整正文或触发采集。"
        "可按 person_id 筛选；没有匹配记录返回空列表。"
        "默认按 record_id 升序分页；order=published_desc 时按发布时间倒序、未知时间在最后，同一时间按 ID 倒序。"
        "默认 20 条、最多 100 条；next_cursor 为空表示末页。翻页保持 order 不变，锚点记录删除后请从第一页刷新。"
        "翻页时保留相同的 person_id 筛选条件。时间统一 UTC，未知时间保留为空；"
        "page_date 只表示页面日期。provenance_type 是入库时的来源类别快照，"
        "verified_page_attribution 仅表示页面归属匹配，不代表事实已独立核实。"
        "未配置认证，仅用于本机开发。"
    ),
    responses={503: {"model": ErrorResponse, "description": "数据库暂不可用"}},
)
def list_public_records(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100, description="每页内容数")] = 20,
    cursor: Annotated[UUID | None, Query(description="上一次响应中的 next_cursor")] = None,
    person_id: Annotated[UUID | None, Query(description="按人物 ID 筛选；留空返回全部已保存内容")] = None,
    order: Literal["record_id", "published_desc"] = "record_id",
) -> PublicRecordsListResponse:
    try:
        if order == "record_id":
            rows = read_public_records(request.app.state.settings, limit, cursor, person_id)
        else:
            rows = read_public_records(request.app.state.settings, limit, cursor, person_id, order)
    except (SQLAlchemyError, ValueError):
        raise HTTPException(
            status_code=503,
            detail={"code": "database_unavailable", "message": "数据库暂不可用，请稍后重试或检查本机数据库配置。"},
        ) from None
    items = [PublicRecordSummary(**row) for row in rows[:limit]]
    return PublicRecordsListResponse(
        items=items,
        next_cursor=items[-1].record_id if len(rows) > limit else None,
    )


@router.get(
    "/public-records/{record_id}",
    response_model=PublicRecordDetail,
    summary="获取内容详情",
    description=(
        "使用内容列表中的 record_id，只读获取单条已保存内容。"
        "返回列表信息以及完整 body_text、speaker_evidence、content_hash 和 published_raw；"
        "正文不截断，不重新访问来源网页或修改数据。"
        "有归档详情时另返回 archive_post，包含归档作者、账号、帖子类型、独立时间、"
        "引用和完整附件说明；没有关联详情时为 null。"
        "主帖正文、引用、归档图片说明及转写分开返回，不能合并为同一人物的发言。"
        "archive_removed_raw 是归档提供的原始标记，不代表本接口核对过原帖删除情况；"
        "unresolved 表示附件所属关系未确定。archive_only 仅依据第三方归档。"
        "content_hash 是保存正文 UTF-8 字节的 SHA-256；speaker_evidence 是页面中的归属依据。"
        "verified_page_attribution 仅表示页面归属匹配，不代表事实已独立核实。"
        "未知时间或人物保持为空；时间统一 UTC，page_date 仅表示日期。"
        "记录不存在返回 404，非法 UUID 返回 422，数据库不可用返回 503。"
        "未配置认证，仅用于本机开发。"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "内容记录不存在"},
        503: {"model": ErrorResponse, "description": "数据库暂不可用"},
    },
)
def get_public_record(record_id: UUID, request: Request) -> PublicRecordDetail:
    try:
        row = read_public_record(request.app.state.settings, record_id)
        result = PublicRecordDetail(**row) if row is not None else None
    except (SQLAlchemyError, ValueError):
        raise HTTPException(
            status_code=503,
            detail={"code": "database_unavailable", "message": "数据库暂不可用，请稍后重试或检查本机数据库配置。"},
        ) from None
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "public_record_not_found", "message": "未找到该内容记录。"},
        )
    return result
