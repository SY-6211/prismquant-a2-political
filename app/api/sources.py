from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import ErrorResponse
from app.repositories.sources import read_sources

router = APIRouter(prefix="/api/v1", tags=["来源"])


class SourcePersonResponse(BaseModel):
    person_id: UUID
    name_original: str
    name_zh: str | None
    monitoring_enabled: bool
    link_enabled: bool


class SourceResponse(BaseModel):
    source_id: UUID
    name: str
    publisher_name: str
    provenance_type: Literal["official", "third_party_archive"]
    fetch_method: Literal["rss", "web_index"]
    feed_url: str
    homepage_url: str | None
    poll_interval_seconds: int
    enabled: bool
    created_at: AwareDatetime
    updated_at: AwareDatetime
    people: list[SourcePersonResponse]

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class SourcesListResponse(BaseModel):
    items: list[SourceResponse]
    next_cursor: UUID | None


@router.get(
    "/sources",
    response_model=SourcesListResponse,
    summary="获取来源列表",
    description=(
        "只读查询来源配置及关联人物，包含尚未启用的来源和关联。"
        "enabled 为来源开关，people 中 monitoring_enabled 为人物开关，link_enabled 为关联开关。"
        "这些开关不代表来源可用性；本接口不访问 RSS、不修改配置、不触发采集。"
        "按 source_id 升序分页，默认 20 条，最多 100 条；next_cursor 为空表示末页。"
        "时间返回 UTC。未配置认证，仅用于本机开发。"
    ),
    responses={503: {"model": ErrorResponse, "description": "数据库暂不可用"}},
)
def list_sources(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100, description="每页来源数")] = 20,
    cursor: Annotated[UUID | None, Query(description="上一次响应中的 next_cursor")] = None,
) -> SourcesListResponse:
    try:
        rows = read_sources(request.app.state.settings, limit, cursor)
    except (SQLAlchemyError, ValueError):
        raise HTTPException(
            status_code=503,
            detail={"code": "database_unavailable", "message": "数据库暂不可用，请稍后重试或检查本机数据库配置。"},
        ) from None
    items = [SourceResponse(**row) for row in rows[:limit]]
    return SourcesListResponse(
        items=items,
        next_cursor=items[-1].source_id if len(rows) > limit else None,
    )
