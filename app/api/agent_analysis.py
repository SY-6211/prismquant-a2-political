"""Local read-only analysis history; generating analyses is not an anonymous API."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter,HTTPException,Query,Request
from sqlalchemy.exc import SQLAlchemyError
from app.repositories import agent_analysis as repo

router=APIRouter(prefix='/api/v1/agent-analyses',tags=['Agent 分析记录'])


def query(request,selector,*args):
    try:return repo.read(request.app.state.settings,selector,*args)
    except (ValueError,SQLAlchemyError):
        raise HTTPException(503,detail={'code':'analysis_unavailable','message':'分析记录暂不可用，请检查数据库及迁移。'}) from None


@router.get('',summary='获取已准备或已完成的 Agent 分析列表')
def listing(request:Request,person_id:UUID|None=None,event_id:UUID|None=None,
            limit:Annotated[int,Query(ge=1,le=100)]=20,cursor:UUID|None=None):
    rows=query(request,repo.select_list,person_id,event_id,limit,cursor)
    return {'items':rows[:limit],'next_cursor':rows[limit-1]['run_id'] if len(rows)>limit else None}


@router.get('/{run_id}',summary='查看分析、当时引用与分析版本')
def detail(request:Request,run_id:UUID):
    value=query(request,repo.select_detail,run_id)
    if value is None:raise HTTPException(404,detail={'code':'analysis_not_found','message':'未找到该分析。'})
    if value['status'] is None:value['status']='prepared_or_unfinished'
    return value
