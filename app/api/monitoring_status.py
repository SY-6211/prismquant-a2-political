"""Public read-only collection status, no paths, credentials, full reports or control actions."""
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from app.source_catalog import execute
from app.scheduler_service import status as collector_status
from app.monitoring_status import summary

router = APIRouter(prefix='/api/v1', tags=['人物来源状态'])


@router.get('/monitoring-status', summary='人物来源最近检查与入库状态；不触发采集或模型调用')
def status(request: Request, person_id: UUID | None = None):
    try:
        collector = collector_status()
        return execute(request.app.state.settings, summary, person_id,
                       {'status':collector.get('status'),'active':collector.get('active')},
                       request.app.state.settings.fast_discovery_enabled, readonly=True)
    except (SQLAlchemyError, ValueError, OSError):
        raise HTTPException(503, detail={'code':'monitoring_status_unavailable','message':'来源状态暂不可用，已保存内容仍可阅读。'}) from None
