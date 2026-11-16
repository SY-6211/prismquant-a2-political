"""Explicitly authenticated rule administration; no changes to old public GETs."""
import secrets
from datetime import datetime
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException,Query,Request
from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
from pydantic import BaseModel,ConfigDict,Field,AwareDatetime,model_validator
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import SQLAlchemyError
from app.core.database import database_url
from app.keyword_rules import Rule
from app.repositories import keywords as repo

bearer=HTTPBearer(auto_error=False)


def admin(request:Request,credentials:HTTPAuthorizationCredentials|None=Depends(bearer)):
    configured=request.app.state.settings.keyword_admin_token.get_secret_value()
    if not configured:raise HTTPException(503,detail={'code':'keyword_admin_disabled','message':'关键词管理 API 尚未配置管理员令牌，可先使用本机命令。'})
    if not credentials or not secrets.compare_digest(credentials.credentials.encode(),configured.encode()):
        raise HTTPException(401,detail={'code':'admin_required','message':'需要关键词管理员权限。'},headers={'WWW-Authenticate':'Bearer'})


router=APIRouter(prefix='/api/v1/keyword-rules',tags=['关键词规则管理'],dependencies=[Depends(admin)])


class Draft(BaseModel):
    model_config=ConfigDict(extra='forbid')
    definition:Rule
    note:str=Field(min_length=1,max_length=500)


class Edit(Draft):
    expected_revision:int=Field(ge=1)


class Preview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    limit:int=Field(default=100,ge=1,le=200)
    since:AwareDatetime|None=None
    until:AwareDatetime|None=None

    @model_validator(mode='after')
    def dates(self):
        if self.since and self.until and self.since>=self.until:raise ValueError('since must precede until')
        return self


class Publish(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version_id:UUID
    preview_id:UUID
    expected_revision:int=Field(ge=1)
    note:str=Field(min_length=1,max_length=500)


def execute(request,fn,*args):
    engine=None
    try:
        engine=create_engine(database_url(request.app.state.settings),poolclass=NullPool,hide_parameters=True,
            connect_args={'connect_timeout':5,'options':'-c statement_timeout=10000 -c lock_timeout=5000'})
        with engine.begin() as conn:return fn(conn,*args)
    except repo.Missing as error:raise HTTPException(404,detail={'code':'not_found','message':str(error)})
    except repo.Conflict as error:raise HTTPException(409,detail={'code':'conflict','message':str(error)})
    except (SQLAlchemyError,ValueError):raise HTTPException(503,detail={'code':'keywords_unavailable','message':'规则管理暂不可用，请检查数据库及迁移。'}) from None
    finally:
        if engine:engine.dispose()


@router.get('')
def listing(request:Request,limit:int=Query(default=20,ge=1,le=100),cursor:UUID|None=None):
    rows=execute(request,repo.listing,limit,cursor)
    return {'items':rows[:limit],'next_cursor':rows[limit-1]['rule_id'] if len(rows)>limit else None}


@router.post('',summary='创建规则草稿（不发布）')
def create(request:Request,body:Draft):return execute(request,repo.draft,body.definition,'http-keyword-admin',body.note)


@router.post('/{rule_id}/drafts',summary='保存新的不可变草稿版本')
def edit(request:Request,rule_id:UUID,body:Edit):
    return execute(request,repo.draft,body.definition,'http-keyword-admin',body.note,rule_id,body.expected_revision)


@router.get('/{rule_id}')
def detail(request:Request,rule_id:UUID):return execute(request,repo.one,rule_id)


@router.get('/{rule_id}/versions')
def history(request:Request,rule_id:UUID,limit:int=Query(default=20,ge=1,le=100),before:int|None=Query(default=None,ge=1)):
    rows=execute(request,repo.history,rule_id,before,limit)
    return {'items':rows[:limit],'next_before':rows[limit-1]['number'] if len(rows)>limit else None}


@router.post('/{rule_id}/versions/{version_id}/preview',summary='保存历史正文试跑结果，不通知、不调用模型')
def preview(request:Request,rule_id:UUID,version_id:UUID,body:Preview):
    return execute(request,repo.preview,rule_id,version_id,'http-keyword-admin',body.limit,body.since,body.until)


@router.post('/{rule_id}/publish',summary='凭本版本预览回执发布；选择旧版本可回滚')
def publish(request:Request,rule_id:UUID,body:Publish):
    return execute(request,repo.publish,rule_id,body.version_id,body.preview_id,body.expected_revision,'http-keyword-admin',body.note)


@router.get('/{rule_id}/previews/{preview_id}')
def saved_preview(request:Request,rule_id:UUID,preview_id:UUID):return execute(request,repo.saved_preview,rule_id,preview_id)


@router.get('/{rule_id}/publications')
def publications(request:Request,rule_id:UUID):return execute(request,repo.publications,rule_id)
