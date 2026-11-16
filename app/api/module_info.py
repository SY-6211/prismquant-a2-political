"""Public integration metadata; operational reads require an admin in production."""
import secrets
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse

router=APIRouter(prefix='/api/v1',tags=['模块集成'])
OPERATIONS=('/api/v1/system-status','/api/v1/monitoring-baseline','/api/v1/monitor-speed',
 '/api/v1/collection-runs','/api/v1/collection-rules','/api/v1/task-queue','/api/v1/collection-tasks',
 '/api/v1/ecb-discovery','/api/v1/source-catalog','/api/v1/web-pilot','/api/v1/agent-analysis-automation')


def protected(path):
    return any(path==prefix or path.startswith(prefix+'/') for prefix in OPERATIONS)


def operations_guard(request):
    settings=request.app.state.settings
    if settings.environment!='production' or not protected(request.url.path):return None
    # Limited public, untrusted timing receipts do not disclose operations data.
    if request.url.path=='/api/v1/monitor-speed/receipts' and request.method=='POST':return None
    token=request.headers.get('authorization','').removeprefix('Bearer ')
    candidates=[settings.keyword_admin_token.get_secret_value()]
    if request.url.path.startswith('/api/v1/source-catalog'):
        candidates.append(settings.source_catalog_admin_token.get_secret_value())
    if any(secret and secrets.compare_digest(secret.encode(),token.encode()) for secret in candidates):return None
    return JSONResponse(status_code=403,content={'detail':{'code':'operations_role_required','message':'此接口需要运行管理权限。'}},headers={'Cache-Control':'no-store'})


@router.get('/module-info',summary='模块版本、界面模式与集成边界，不返回凭证')
def info(request:Request):
    settings=request.app.state.settings
    return {'module':'prismquant-person-monitor','version':request.app.version,'api_prefix':'/api/v1',
        'console_mode':settings.console_mode,'operations_protected':settings.environment=='production',
        'runtime_instance':settings.runtime_instance,'private_transport':'same_origin_bearer',
        'identity_integration':'server_verified_token_mapping_required','model_calls_on_startup':0,
        'm2':'deferred','publication_to_display_verified':False}
