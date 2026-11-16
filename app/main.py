from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager,suppress
import asyncio

from app.api.health import router as health_router
from app.api.collection_runs import router as collection_runs_router
from app.api.collection_rules import router as collection_rules_router
from app.api.people import router as people_router
from app.api.public_records import router as public_records_router
from app.api.sources import router as sources_router
from app.api.operations import router as operations_router
from app.api.baseline import router as baseline_router
from app.api.events import router as events_router
from app.api.tasks import router as tasks_router
from app.api.ecb_discovery import router as ecb_discovery_router
from app.api.content_stream import router as content_stream_router
from app.api.keywords import router as keywords_router
from app.api.annotations import router as annotations_router,review_router
from app.api.agent_analysis import router as agent_analysis_router
from app.api.chat import router as chat_router
from app.api.web_qa import router as web_qa_router
from app.api.auto_analysis import router as auto_analysis_router
from app.api.source_catalog import router as source_catalog_router
from app.api.web_pilot import router as web_pilot_router
from app.api.monitor_speed import router as monitor_speed_router
from app.api.monitoring_status import router as monitoring_status_router
from app.api.quality import router as quality_router, review_router as quality_review_router
from app.api.module_info import router as module_info_router,operations_guard,protected
from app.api.reminders import router as reminders_router,private_router as reminder_feedback_router
from app.api.knowledge import router as knowledge_router
from app.core.config import PROJECT_ROOT, Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    @asynccontextmanager
    async def lifespan(application):
        tasks=[]
        if config.reminder_processing_enabled:
            from app.reminders import loop
            tasks.append(asyncio.create_task(loop(config)))
        if config.knowledge_indexing_enabled:
            from app.knowledge import loop as knowledge_loop
            tasks.append(asyncio.create_task(knowledge_loop(config)))
        try:yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):await task
    application = FastAPI(
        lifespan=lifespan,
        title=config.service_name,
        version="0.1.0",
        description=(
            "PrismQuant 政治人物监控后端。\n\n"
            "目前提供服务健康检查及独立的本地只读数据库检查命令；"
            "已提供人物列表、人物详情、来源配置及内容列表和详情只读查询；"
            "已支持人工批量入库、独立后台定时采集及运行记录查询；实际启用状态以配置与运行记录为准。"
            "已支持受控人物分析、私有会话、受控问答准备与引用/回答保存；真实问答试跑以验收记录为准。"
            "已提供人物问答界面、私有历史和引用查看；仅在用户具有有效限额授权时允许网页生成，不自动重试付费请求。"
            "已提供本地向量知识库与受控混合检索问答；持续付费模型任务需要独立授权。\n\n"
            "业务接口使用 /api/v1 前缀。"
        ),
    )
    application.state.settings = config
    application.include_router(auto_analysis_router)
    application.include_router(reminders_router)
    application.include_router(knowledge_router)
    application.include_router(web_pilot_router)
    application.include_router(monitor_speed_router)
    application.include_router(monitoring_status_router)
    application.include_router(quality_router)
    application.include_router(module_info_router)
    application.include_router(quality_review_router)
    application.include_router(reminder_feedback_router)
    @application.middleware('http')
    async def private_chat_cache(request,call_next):
        denied=operations_guard(request)
        if denied is not None:return denied
        response=await call_next(request)
        if request.url.path.startswith(('/api/v1/agent-chat','/api/v1/quality-review','/api/v1/knowledge','/api/v1/monitor-speed','/api/v1/monitoring-status')) or (request.url.path.startswith('/api/v1/source-catalog') and request.method!='GET'):
            response.headers['Cache-Control']='no-store';response.headers['Pragma']='no-cache'
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request,error):
        if request.url.path.startswith('/api/v1/knowledge'):
            return JSONResponse(status_code=422,content={'detail':{'code':'knowledge_invalid_input','message':'请核对人物、问题长度和发布日期范围。'}})
        if request.url.path.startswith('/api/v1/source-catalog'):
            return JSONResponse(status_code=422,content={'detail':{'code':'catalog_invalid_input','message':'来源目录参数格式不正确，请核对字段、URL和审核版本。'}})
        if request.url.path.startswith('/api/v1/agent-chat'):
            return JSONResponse(status_code=422,content={'detail':{'code':'chat_invalid_input','message':'会话参数格式不正确，请核对字段、长度和提交标识。'}})
        if request.url.path.startswith('/api/v1/quality-review'):
            return JSONResponse(status_code=422,content={'detail':{'code':'quality_invalid_input','message':'请核对评测字段、修订号与证据位置。'}})
        return await request_validation_exception_handler(request,error)
    if config.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["Accept", "Content-Type", "Last-Event-ID"],
        )
    application.include_router(health_router)
    application.include_router(people_router)
    application.include_router(sources_router)
    application.include_router(public_records_router)
    application.include_router(collection_runs_router)
    application.include_router(collection_rules_router)
    application.include_router(operations_router)
    application.include_router(baseline_router)
    application.include_router(events_router)
    application.include_router(tasks_router)
    application.include_router(ecb_discovery_router)
    application.include_router(content_stream_router)
    application.include_router(keywords_router)
    application.include_router(annotations_router)
    application.include_router(review_router)
    application.include_router(agent_analysis_router)
    application.include_router(chat_router)
    application.include_router(web_qa_router)
    application.include_router(source_catalog_router)
    application.mount("/monitor", StaticFiles(directory=PROJECT_ROOT / "frontend", html=True), name="monitor")
    if config.environment=='production':
        from fastapi.openapi.utils import get_openapi
        def schema():
            if application.openapi_schema:return application.openapi_schema
            value=get_openapi(title=application.title,version=application.version,description=application.description,routes=application.routes)
            value.setdefault('components',{}).setdefault('securitySchemes',{})['OperationsAdmin']={'type':'http','scheme':'bearer','description':'运行管理令牌；来源目录也接受独立来源管理员令牌。'}
            for path,methods in value['paths'].items():
                if protected(path):
                    for method,spec in methods.items():
                        if path=='/api/v1/monitor-speed/receipts' and method=='post':continue
                        spec['security']=[{'OperationsAdmin':[]}]
            application.openapi_schema=value;return value
        application.openapi=schema
    return application


app = create_app()
