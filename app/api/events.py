"""Additive event/history API; no network fetches or model calls."""
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import ErrorResponse
from app.event_model import EventSnapshot, Provenance, Transport, Verification
from app.repositories.events import read_query, select_event, select_events, select_version, select_versions

router = APIRouter(prefix='/api/v1', tags=['事件与版本'])
ERRORS = {503: {'model': ErrorResponse}}
DETAIL_ERRORS = {**ERRORS, 404: {'model': ErrorResponse}}


class EventSummary(BaseModel):
    event_id: UUID
    platform: str
    native_id: str
    version_id: UUID
    version_number: int
    channel_id: UUID
    source_id: UUID
    transport: Transport
    source_name: str
    publisher_name: str
    person_id: UUID | None
    person_name_original: str | None
    person_name_zh: str | None
    provenance_type: Provenance
    verification_status: Verification
    review_status: str
    title: str
    post_type: str
    published_at: AwareDatetime | None
    last_observed_at: AwareDatetime

    @field_validator('published_at', 'last_observed_at')
    @classmethod
    def utc(cls, value: datetime | None):
        return value.astimezone(timezone.utc) if value is not None else None


class EventDetail(EventSummary):
    payload: EventSnapshot
    snapshot_hash: str
    linked_record_ids: list[UUID]


class EventList(BaseModel):
    items: list[EventSummary]
    next_cursor: UUID | None


class VersionSummary(BaseModel):
    version_id: UUID
    version_number: int
    channel_id: UUID
    source_id: UUID
    transport: Transport
    person_id: UUID | None
    provenance_type: Provenance
    verification_status: Verification
    review_status: str
    snapshot_hash: str
    created_at: AwareDatetime


class VersionDetail(VersionSummary):
    payload: EventSnapshot


class VersionList(BaseModel):
    items: list[VersionSummary]
    next_before: int | None


class Observation(BaseModel):
    observation_id: int
    version_id: UUID
    observed_at: AwareDatetime
    observation_kind: str
    metadata: dict


class ObservationList(BaseModel):
    items: list[Observation]
    next_before: int | None


def query(request, selector, *args):
    try:
        return read_query(request.app.state.settings, selector, *args)
    except (SQLAlchemyError, ValueError):
        raise HTTPException(503, detail={'code': 'database_unavailable', 'message': '事件数据暂不可用，请检查数据库连接及迁移。'}) from None


def require(value):
    if value is None:
        raise HTTPException(404, detail={'code': 'event_not_found', 'message': '未找到该事件或版本。'})
    return value


@router.get('/events', response_model=EventList, summary='获取事件列表', responses=ERRORS,
            description='只读。按事件 ID 升序分页，人物筛选使用最新观测版本的归属。渠道与来源归属分开；原生 ID 相同才合并。'
                        'latest 表示最近观测，不表示已核实内容；needs_review 版本须核对。未配置认证，仅供本机开发。')
def list_events(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                cursor: UUID | None = None, person_id: UUID | None = None):
    rows = query(request, select_events, limit, cursor, person_id)
    return EventList(items=rows[:limit], next_cursor=rows[limit-1]['event_id'] if len(rows) > limit else None)


@router.get('/events/{event_id}', response_model=EventDetail, summary='获取事件及独立内容卡片', responses=DETAIL_ERRORS,
            description='payload.main_text 为主文；quoted_content 为引用；media_notes 为归档附件说明或转写。'
                        '三者独立，引用与转写不可合并为本人发言。第三方归档保留 archive_only；时间未知保持 null。'
                        'last_observed_at 不是发布时间，也不是客户端显示时间。')
def get_event(event_id: UUID, request: Request):
    return require(query(request, select_event, event_id))


@router.get('/events/{event_id}/versions', response_model=VersionList, summary='获取事件历史版本', responses=DETAIL_ERRORS)
def list_versions(event_id: UUID, request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                  before: Annotated[int | None, Query(ge=1)] = None):
    require(query(request, select_event, event_id))
    rows = query(request, select_versions, event_id, limit, before)
    return VersionList(items=rows[:limit], next_before=rows[limit-1]['version_number'] if len(rows) > limit else None)


@router.get('/events/{event_id}/versions/{version_id}', response_model=VersionDetail,
            summary='读取某一历史版本的完整内容', responses=DETAIL_ERRORS)
def get_version(event_id: UUID, version_id: UUID, request: Request):
    return require(query(request, select_version, event_id, version_id))


def select_observations(connection, event_id, limit, before):
    return [dict(row) for row in connection.execute(text('SELECT observation_id, version_id, observed_at, '
        'observation_kind, metadata FROM event_observations WHERE event_id=:id '
        'AND (CAST(:before AS bigint) IS NULL OR observation_id < :before) '
        'ORDER BY observation_id DESC LIMIT :limit'), {'id': event_id, 'before': before, 'limit': limit+1}).mappings()]


@router.get('/events/{event_id}/observations', response_model=ObservationList, summary='获取观测历史', responses=DETAIL_ERRORS,
            description='按写入序号倒序。A→B→A 会复用 A 的版本，但新增观测记录，保留内容回退的过程。')
def list_observations(event_id: UUID, request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                      before: Annotated[int | None, Query(ge=1)] = None):
    require(query(request, select_event, event_id))
    rows = query(request, select_observations, event_id, limit, before)
    return ObservationList(items=rows[:limit], next_before=rows[limit-1]['observation_id'] if len(rows) > limit else None)


def select_record_event(connection, record_id):
    event_id = connection.execute(text('SELECT event_id FROM event_record_links WHERE record_id=:id'),
                                  {'id': record_id}).scalar_one_or_none()
    return select_event(connection, event_id) if event_id is not None else None


@router.get('/public-records/{record_id}/event', response_model=EventDetail,
            summary='从旧内容 ID 查找对应事件', responses=DETAIL_ERRORS)
def get_record_event(record_id: UUID, request: Request):
    return require(query(request, select_record_event, record_id))
