"""Operational discovery reads and bounded client-reported notice receipts."""
from uuid import UUID
from fastapi import APIRouter,Request,HTTPException
from pydantic import BaseModel,ConfigDict,Field,FiniteFloat
from sqlalchemy.exc import SQLAlchemyError
from app.source_catalog import execute
from app.repositories import monitor_speed as repo
router=APIRouter(prefix='/api/v1/monitor-speed',tags=['持续发现与速度观测'])
class Receipt(BaseModel):
 model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
 session_id:UUID
 version_id:UUID
 notice_render_ms:FiniteFloat=Field(ge=0,le=60000)
@router.get('')
def status(request:Request):
 try:
  value=execute(request.app.state.settings,repo.overview,readonly=True)
  value['runtime_configured']=request.app.state.settings.fast_discovery_enabled
  return value
 except (SQLAlchemyError,ValueError):raise HTTPException(503,detail={'code':'speed_unavailable','message':'持续发现状态暂不可用，请检查迁移。'}) from None
@router.post('/receipts')
def receipt(request:Request,body:Receipt):
 origin=request.headers.get('origin')
 if origin and origin!=str(request.base_url).rstrip('/'):
  raise HTTPException(403,detail={'code':'same_origin_required','message':'回执仅接收同源页面。'})
 try:return execute(request.app.state.settings,lambda c:repo.receipt(c,body.session_id,body.version_id,body.notice_render_ms),readonly=False)
 except repo.ReceiptRateLimit:raise HTTPException(429,detail={'code':'receipt_rate_limit','message':'回执频率超限。'}) from None
 except (SQLAlchemyError,ValueError):raise HTTPException(503,detail={'code':'receipt_unavailable','message':'回执未记录，不影响原文阅读。'}) from None
