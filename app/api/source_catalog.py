"""Public inventory reads; optional independent admin review, no enabling endpoint."""
import secrets
from uuid import UUID
from fastapi import APIRouter,Request,Query,Depends,HTTPException
from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from app.source_catalog import execute
from app.source_catalog_model import Review,Decision
from app.repositories import source_catalog as repo
router=APIRouter(prefix='/api/v1/source-catalog',tags=['候选来源目录'])
bearer=HTTPBearer(auto_error=False,scheme_name='SourceCatalogAdmin')
def admin(request:Request,credential:HTTPAuthorizationCredentials|None=Depends(bearer)):
    configured=request.app.state.settings.source_catalog_admin_token.get_secret_value()
    if not configured:raise HTTPException(503,detail={'code':'catalog_admin_disabled','message':'网页审核写入未开启，可使用本机管理命令。'})
    if credential is None or not secrets.compare_digest(configured.encode(),credential.credentials.encode()):raise HTTPException(401,detail={'code':'catalog_admin_required','message':'需要独立的来源目录管理员凭证。'},headers={'WWW-Authenticate':'Bearer'})
def run(request,fn,*args,readonly=True):
    try:return execute(request.app.state.settings,fn,*args,readonly=readonly)
    except repo.Missing:raise HTTPException(404,detail={'code':'catalog_missing','message':'候选来源不存在。'}) from None
    except repo.Conflict:raise HTTPException(409,detail={'code':'catalog_conflict','message':'审核版本已变化，请先刷新。'}) from None
    except (SQLAlchemyError,ValueError):raise HTTPException(503,detail={'code':'catalog_unavailable','message':'来源目录暂不可用，请检查数据库和迁移。'}) from None
@router.get('/summary',summary='查看候选目录统计；非已接入来源统计')
def summary(request:Request):return run(request,repo.overview)
@router.get('',summary='分页检索候选来源')
def listing(request:Request,limit:int=Query(20,ge=1,le=100),cursor:UUID|None=None,q:str|None=Query(None,min_length=1,max_length=100),source_type:str|None=Query(None,max_length=120),decision:Decision|None=None,issue:str|None=Query(None,max_length=80),import_id:UUID|None=None):
    rows=run(request,repo.listing,limit,cursor,q,source_type,decision,issue,import_id)
    return {'items':rows[:limit],'next_cursor':rows[limit-1]['candidate_id'] if len(rows)>limit else None,'collection_enabled':False}
@router.get('/{candidate_id}')
def detail(request:Request,candidate_id:UUID):return run(request,repo.one,candidate_id)
@router.get('/{candidate_id}/reviews')
def reviews(request:Request,candidate_id:UUID,limit:int=Query(20,ge=1,le=100),before:int|None=Query(None,ge=1)):
    rows=run(request,repo.history,candidate_id,limit,before)
    return {'items':rows[:limit],'next_before':rows[limit-1]['revision'] if len(rows)>limit else None}
@router.post('/{candidate_id}/reviews',dependencies=[Depends(admin)],summary='保存审核记录，不启用采集')
def save_review(request:Request,candidate_id:UUID,body:Review):return run(request,repo.review,candidate_id,body,'http-catalog-admin',readonly=False)
