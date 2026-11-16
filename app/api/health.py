from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["服务状态"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    scope: Literal["api_process"]


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="检查接口服务是否运行",
    description=(
        "只检查接口进程能否响应，不连接数据库、公开来源或模型服务。"
        "status=ok 不代表采集或 Agent 已就绪。"
    ),
)
def health(request: Request) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=request.app.state.settings.service_name,
        version=request.app.version,
        scope="api_process",
    )
