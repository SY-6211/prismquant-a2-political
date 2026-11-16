"""Read-only automation state; no public switch or spending endpoint."""
from fastapi import APIRouter,Request,HTTPException
from sqlalchemy.exc import SQLAlchemyError
from app.repositories import agent_analysis,auto_analysis

router=APIRouter(prefix='/api/v1/agent-analysis-automation',tags=['自动分析状态'])
@router.get('',summary='查看自动分析授权与任务状态，不触发生成')
def status(request:Request):
    try:return agent_analysis.read(request.app.state.settings,auto_analysis.status)
    except (ValueError,SQLAlchemyError):raise HTTPException(503,detail={'code':'auto_analysis_unavailable','message':'自动分析状态暂不可用，请核对迁移。'}) from None
