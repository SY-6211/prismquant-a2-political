from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import ErrorResponse
from app.collection_rules import RuleCheck, check_rules

router = APIRouter(prefix='/api/v1', tags=['自动采集规则'])


@router.get('/collection-rules', response_model=RuleCheck, summary='检查自动采集规则',
            responses={503: {'model': ErrorResponse}},
            description='只读检查受支持来源的配置及人物/来源/关联三个开关，返回逐项原因。eligible 仅表示配置允许，不代表已到执行时间或已启动监控；不访问 RSS，不写运行记录，不修改开关。读取间隔至少 60 秒；调度命令另行处理执行间隔、互斥及未结束运行，并在请求及写入前重查。接口不会启动调度，仅用于本机开发。')
def get_rules(request: Request, source: Literal['ecb', 'trump'] | None = None):
    try:
        return check_rules(request.app.state.settings, source)
    except (SQLAlchemyError, ValueError):
        raise HTTPException(status_code=503, detail={'code': 'database_unavailable', 'message': '规则检查暂不可用，请检查本机数据库和来源配置。'}) from None
