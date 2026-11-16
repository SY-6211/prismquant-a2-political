"""Local read-only queue observability; no task administration over HTTP."""
from typing import Annotated, Literal
from datetime import timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import ErrorResponse
from app.repositories.events import read_query
from app.repositories.tasks import select_attempts, select_stats, select_task, select_tasks
from app.task_types import Lane

TaskStatus = Literal['queued','running','retry','succeeded','dead','needs_review','cancelled']
router = APIRouter(prefix='/api/v1', tags=['持久任务队列'])
ERRORS = {503: {'model': ErrorResponse}}
DETAIL_ERRORS = {**ERRORS, 404: {'model': ErrorResponse}}


class TaskSummary(BaseModel):
    task_id: UUID
    source_id: UUID
    kind: str
    lane: Lane
    priority: int
    status: TaskStatus
    available_at: AwareDatetime
    attempt_count: int
    failure_count: int
    max_failures: int
    lease_expires_at: AwareDatetime | None
    last_code: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator('available_at', 'lease_expires_at', 'created_at', 'updated_at')
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc) if value else None


class TaskDetail(TaskSummary):
    payload: dict
    result: dict


class TaskList(BaseModel):
    items: list[TaskSummary]
    next_cursor: UUID | None


class Attempt(BaseModel):
    attempt_id: UUID
    attempt_number: int
    status: str
    code: str | None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    run_id: UUID | None

    @field_validator('started_at', 'finished_at')
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc) if value else None


class AttemptList(BaseModel):
    items: list[Attempt]
    next_before: int | None


class QueueStat(BaseModel):
    lane: Lane
    status: TaskStatus
    task_count: int
    ready_count: int
    oldest_created_at: AwareDatetime

    @field_validator('oldest_created_at')
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc)


class QueueStats(BaseModel):
    items: list[QueueStat]


def query(request, selector, *args):
    try:
        return read_query(request.app.state.settings, selector, *args)
    except (SQLAlchemyError, ValueError):
        raise HTTPException(503, detail={'code': 'queue_unavailable', 'message': '任务队列暂不可用，请检查数据库及迁移。'}) from None


def require(value):
    if value is None:
        raise HTTPException(404, detail={'code': 'task_not_found', 'message': '未找到该任务。'})
    return value


@router.get('/task-queue', response_model=QueueStats, summary='查看队列积压', responses=ERRORS,
            description='只读汇总；空队列返回空列表，不表示 worker 在线。媒体/AI 仅预留独立队列，尚无执行器。仅限本机开发。')
def stats(request: Request):
    return QueueStats(items=query(request, select_stats))


@router.get('/collection-tasks', response_model=TaskList, summary='获取任务列表', responses=ERRORS)
def tasks(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: UUID | None = None,
          status: TaskStatus | None = None, lane: Lane | None = None):
    rows = query(request, select_tasks, limit, cursor, status, lane)
    return TaskList(items=rows[:limit], next_cursor=rows[limit-1]['task_id'] if len(rows) > limit else None)


@router.get('/collection-tasks/{task_id}', response_model=TaskDetail, summary='获取任务详情', responses=DETAIL_ERRORS,
            description='任务状态与采集运行状态分开。succeeded 仅表示该有限任务完成，详情仍可能有待核对内容。'
                        '重试时间包含退避与 Retry-After；不在 GET 时启动或恢复任务。')
def task(task_id: UUID, request: Request):
    return require(query(request, select_task, task_id))


@router.get('/collection-tasks/{task_id}/attempts', response_model=AttemptList, summary='获取任务尝试历史', responses=DETAIL_ERRORS)
def attempts(task_id: UUID, request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20,
             before: Annotated[int | None, Query(ge=1)] = None):
    require(query(request, select_task, task_id))
    rows = query(request, select_attempts, task_id, limit, before)
    return AttemptList(items=rows[:limit], next_before=rows[limit-1]['attempt_number'] if len(rows) > limit else None)
