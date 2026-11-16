from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import ErrorResponse
from app.repositories.people import read_people, read_person

router = APIRouter(prefix="/api/v1", tags=["人物"])


class PersonResponse(BaseModel):
    person_id: UUID
    name_original: str
    name_zh: str | None
    aliases: list[str]
    country_codes: list[str]
    monitoring_enabled: bool
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class PeopleListResponse(BaseModel):
    items: list[PersonResponse]
    next_cursor: UUID | None


@router.get(
    "/people",
    response_model=PeopleListResponse,
    summary="获取人物列表",
    description=(
        "只读查询人物基础资料，包括监控尚未开启的人物。"
        "按人物 ID 升序排列；默认每页 20 条，最多 100 条。"
        "next_cursor 非空时可传入下一次请求的 cursor；为空表示已到末页。"
        "时间统一返回 UTC。本阶段未配置登录认证，仅用于本机开发。"
    ),
    responses={503: {"model": ErrorResponse, "description": "数据库暂不可用"}},
)
def list_people(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100, description="每页条数")] = 20,
    cursor: Annotated[UUID | None, Query(description="上一次响应中的 next_cursor")] = None,
) -> PeopleListResponse:
    try:
        rows = read_people(request.app.state.settings, limit, cursor)
    except (SQLAlchemyError, ValueError):
        # Never expose SQL, connection parameters or driver exception messages.
        raise HTTPException(
            status_code=503,
            detail={"code": "database_unavailable", "message": "数据库暂不可用，请稍后重试或检查本机数据库配置。"},
        ) from None
    items = [PersonResponse(**row) for row in rows[:limit]]
    next_cursor = items[-1].person_id if len(rows) > limit else None
    return PeopleListResponse(items=items, next_cursor=next_cursor)


@router.get(
    "/people/{person_id}",
    response_model=PersonResponse,
    summary="获取人物详情",
    description=(
        "根据列表返回的 person_id，只读查询单个人物的八项基础资料。"
        "时间统一为 UTC，不修改监控状态。人物不存在返回 404，"
        "ID 不是有效 UUID 时返回 422，数据库不可用返回 503。"
        "目前不包含职务、任期、账号或发言。本阶段未配置认证，仅用于本机开发。"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "人物不存在"},
        503: {"model": ErrorResponse, "description": "数据库暂不可用"},
    },
)
def get_person(person_id: UUID, request: Request) -> PersonResponse:
    try:
        row = read_person(request.app.state.settings, person_id)
    except (SQLAlchemyError, ValueError):
        raise HTTPException(
            status_code=503,
            detail={"code": "database_unavailable", "message": "数据库暂不可用，请稍后重试或检查本机数据库配置。"},
        ) from None
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "person_not_found", "message": "未找到该人物。"},
        )
    return PersonResponse(**row)
