"""Read-only saved pilot evidence; viewing never performs a network fetch."""
from uuid import UUID
from typing import Literal
from fastapi import APIRouter,Request,Query,HTTPException
from sqlalchemy.exc import SQLAlchemyError
from app.source_catalog import execute
from app.repositories import web_pilot as repo
router=APIRouter(prefix='/api/v1/web-pilot',tags=['网页来源试接'])
def read(request,fn,*args):
    try:return execute(request.app.state.settings,fn,*args,readonly=True)
    except (SQLAlchemyError,ValueError):raise HTTPException(503,detail={'code':'web_pilot_unavailable','message':'网页试接记录暂不可用，请检查数据库迁移。'}) from None
@router.get('/summary',summary='查看网页试接结果；不抓取外站')
def summary(request:Request):return read(request,repo.overview)
@router.get('/documents',summary='已保存的网页提取样本')
def listing(request:Request,source:Literal['fed','eia','treasury']|None=None,limit:int=Query(20,ge=1,le=50)):
    return {'items':read(request,repo.listing,source,limit),'continuous_collection_enabled':False}
@router.get('/documents/{document_id}',summary='原文、提取版本与人物入库关联')
def detail(request:Request,document_id:UUID):
    value=read(request,repo.detail,document_id)
    if value is None:raise HTTPException(404,detail={'code':'web_document_missing','message':'网页样本不存在。'})
    return value
