"""Read-only inspection of bounded ECB index discovery; GET never starts a scan."""
from typing import Annotated, Literal
from fastapi import APIRouter,HTTPException,Query,Request
from sqlalchemy.exc import SQLAlchemyError
from app.repositories.events import read_query
from app.repositories.ecb_discovery import select_candidates,select_status

router = APIRouter(prefix='/api/v1',tags=['ECB 快发现'])


def query(request,selector,*args):
    try:
        return read_query(request.app.state.settings,selector,*args)
    except (SQLAlchemyError,ValueError):
        raise HTTPException(503,detail={'code':'ecb_discovery_unavailable','message':'ECB 发现状态暂不可用，请检查连接及迁移。'}) from None


@router.get('/ecb-discovery',summary='查看 ECB 索引与 RSS 发现状态',
            description='只读，不发起采集。首次成功窗口为基线；只观察索引首块与当前 RSS，不代表完整覆盖或 60 秒时效已通过。')
def discovery(request:Request):
    return query(request,select_status)


@router.get('/ecb-discovery/candidates',summary='分页查看 ECB 发现候选')
def candidates(request:Request,limit:Annotated[int,Query(ge=1,le=100)]=20,
               cursor:Annotated[str|None,Query(max_length=1024)]=None,
               status:Literal['baseline','pending','saved','needs_review']|None=None):
    rows = query(request,select_candidates,limit,cursor,status)
    return {'items':rows[:limit],'next_cursor':rows[limit-1]['url'] if len(rows)>limit else None}
