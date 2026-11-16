from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, Field, model_validator, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import ErrorResponse
from app.repositories.collection_runs import read_run, read_runs
from app.baseline import RunTiming, ItemTiming

RunStatus = Literal['running', 'completed', 'completed_with_issues', 'no_candidates', 'no_new_candidates', 'stopped', 'interrupted']
router = APIRouter(prefix='/api/v1', tags=['采集运行记录'])


class RunCounts(BaseModel):
    attempted: int = Field(ge=0)
    inserted: int = Field(ge=0)
    already_exists: int = Field(ge=0)
    needs_review: int = Field(ge=0)
    failed: int = Field(ge=0)
    interrupted: int = Field(ge=0)
    not_attempted: int = Field(ge=0)
    skipped_unsupported: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def consistent(self):
        if self.attempted != sum((self.inserted, self.already_exists, self.needs_review, self.failed, self.interrupted, self.skipped_unsupported)):
            raise ValueError('Inconsistent run counters')
        return self


class RunSummary(BaseModel):
    timing: RunTiming = Field(default_factory=RunTiming)
    run_id: UUID
    source: Literal['ecb', 'trump']
    source_id: UUID
    person_id: UUID
    mode: Literal['manual_batch_import', 'automatic_batch_import', 'manual_history_import']
    status: RunStatus
    batch_limit: int = Field(ge=1, le=5)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    stop_reason: str | None
    report_complete: bool
    recovered_at: AwareDatetime | None = None
    recovery_note: str | None = None
    source_enabled: bool | None
    monitoring_enabled: bool | None
    link_enabled: bool | None
    selection: dict[str, Annotated[int, Field(ge=0)]]
    summary: RunCounts | None

    @model_validator(mode='before')
    @classmethod
    def incomplete_counts_are_unknown(cls, value):
        value = dict(value)
        if not value.get('report_complete'):
            value['summary'] = None
        return value

    @field_validator('started_at', 'finished_at', 'recovered_at')
    @classmethod
    def utc(cls, value: datetime | None):
        return value.astimezone(timezone.utc) if value else None


class RunItem(BaseModel):
    timing: ItemTiming = Field(default_factory=ItemTiming)
    phase: Literal['precheck', 'detail']
    position: int = Field(ge=0)
    url: str | None
    title: str | None
    record_id: UUID | None
    status: Literal['skipped_existing', 'skipped_unsupported', 'needs_review', 'inserted', 'already_exists', 'failed', 'interrupted', 'not_attempted']
    reason: str | None
    write_outcome_unknown: bool


class RunDetail(RunSummary):
    items: list[RunItem]


class RunsPage(BaseModel):
    items: list[RunSummary]
    next_cursor: UUID | None


def unavailable():
    return HTTPException(status_code=503, detail={'code': 'database_unavailable', 'message': '运行记录暂不可用，请检查数据库及迁移版本。'})


@router.get('/collection-runs', response_model=RunsPage, summary='获取采集运行记录列表',
            responses={503: {'model': ErrorResponse}},
            description='只读查询人工及自动批量入库历史，mode 区分执行方式；按开始时间及 ID 倒序分页，不触发采集。source/status 可筛选，翻页保留筛选条件。running 仅表示未记录结束，进程强制结束后可能仍保留此状态；summary=null 表示没有完整汇总，不表示新增为零。当前仅用于本机开发。')
def list_runs(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20,
              cursor: UUID | None = None, source: Literal['ecb', 'trump'] | None = None,
              status: RunStatus | None = None):
    try:
        rows = read_runs(request.app.state.settings, limit, cursor, source, status)
        items = [RunSummary(**row) for row in rows[:limit]]
        return RunsPage(items=items, next_cursor=items[-1].run_id if len(rows) > limit else None)
    except (SQLAlchemyError, ValueError):
        raise unavailable() from None


@router.get('/collection-runs/{run_id}', response_model=RunDetail, summary='获取采集运行记录详情',
            responses={404: {'model': ErrorResponse}, 503: {'model': ErrorResponse}},
            description='返回汇总及 precheck/detail 两阶段明细，包括跳过和待核对原因。只保存状态元数据，不保存正文、转写或凭证。write_outcome_unknown 表示当前内容提交结果需复核。历史不能补回过去终端记录；未正常结束的运行不伪装为成功。')
def get_run(run_id: UUID, request: Request):
    try:
        row = read_run(request.app.state.settings, run_id)
        result = RunDetail(**row) if row is not None else None
    except (SQLAlchemyError, ValueError):
        raise unavailable() from None
    if result is None:
        raise HTTPException(status_code=404, detail={'code': 'collection_run_not_found', 'message': '未找到该采集运行记录。'})
    return result
