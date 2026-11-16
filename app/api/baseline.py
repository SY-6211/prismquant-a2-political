from typing import Annotated, Literal
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError
from app.api.errors import ErrorResponse
from app.baseline import build_baseline
from app.repositories.baseline import read_baseline

router=APIRouter(prefix='/api/v1',tags=['监控延迟基线'])


@router.get('/monitoring-baseline',summary='查看监控阶段耗时与未测量范围',
            responses={503:{'model':ErrorResponse}},
            description='只读、每次查询最多1000条运行及1000条明细，超限标记truncated。自动网络采集阶段耗时与回补分开；不代表公开到页面的实时性，不触发采集或模型。')
def baseline(request:Request, hours:Annotated[int,Query(ge=1,le=168)]=24,
             source:Literal['ecb','trump']|None=None):
    try:
        return build_baseline(read_baseline(request.app.state.settings,hours,source),hours)
    except (SQLAlchemyError,ValueError):
        raise HTTPException(status_code=503,detail={'code':'baseline_unavailable',
            'message':'基线暂不可用，请检查数据库及迁移版本。'}) from None
