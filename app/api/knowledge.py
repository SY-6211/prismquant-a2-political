"""Public saved-source retrieval, without generation or conversation access."""
from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, field_validator, model_validator
from sqlalchemy.exc import SQLAlchemyError
from app import knowledge
from app.repositories import knowledge as repo

router = APIRouter(prefix='/api/v1/knowledge', tags=['本地向量知识库'])


class Search(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    person_id: UUID
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None

    @field_validator('query')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Query must not be blank')
        return value.strip()

    @model_validator(mode='after')
    def dates(self):
        if self.since and self.until and self.since >= self.until:
            raise ValueError('Invalid publication date range')
        return self


@router.get('/status', summary='查看索引覆盖率及免费本地处理状态')
def status(request: Request):
    try:
        result = knowledge.read(request.app.state.settings, repo.status)
        result['processor_configured'] = request.app.state.settings.knowledge_indexing_enabled
        result['poll_seconds'] = request.app.state.settings.knowledge_poll_seconds
        at = result['control']['checked_at']
        age = (datetime.now(timezone.utc) - at).total_seconds() if at else None
        result['processor_active'] = bool(result['processor_configured'] and result['control']['enabled'] and age is not None and 0 <= age < result['poll_seconds'] * 3)
        return result
    except (ValueError, SQLAlchemyError):
        raise HTTPException(503, detail={'code': 'knowledge_unavailable', 'message': '知识库暂不可用，请检查数据库及迁移。'}) from None


@router.post('/search', summary='按人物、发布日期检索原文片段；不生成回答')
def search(request: Request, body: Search):
    # CPU use is bounded and same-origin. Team integration can proxy this route.
    origin = request.headers.get('origin')
    if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin != str(request.base_url).rstrip('/')):
        raise HTTPException(403, detail={'code': 'same_origin_required', 'message': '请从同源页面或总平台代理发起检索。'})
    try:
        return knowledge.search(request.app.state.settings, body.person_id, body.query, body.limit, body.since, body.until)
    except knowledge.Busy:
        raise HTTPException(409, detail={'code': 'knowledge_busy', 'message': '本地模型正在处理索引或另一条检索，请稍后再试。'}) from None
    except ValueError:
        raise HTTPException(400, detail={'code': 'knowledge_scope', 'message': '请缩短问题或缩小人物、日期范围；也请核对本地模型文件。'}) from None
    except (SQLAlchemyError, ImportError, RuntimeError, OSError):
        raise HTTPException(503, detail={'code': 'knowledge_unavailable', 'message': '本地检索暂不可用，请检查依赖、模型及索引状态。'}) from None
