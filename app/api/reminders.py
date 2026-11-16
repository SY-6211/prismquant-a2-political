from typing import Annotated
from uuid import UUID
from fastapi import APIRouter,Request,Query,Depends,HTTPException
from sqlalchemy.exc import SQLAlchemyError
from app.api.chat import credential,execute
from app.reminder_model import Status,Priority,Feedback
from app.repositories import agent_analysis,reminders as repo,chat

router=APIRouter(prefix='/api/v1/reminders',tags=['站内关键词提醒'])
private_router=APIRouter(prefix='/api/v1/agent-chat',tags=['私有提醒反馈'])

def read(request,fn,*args):
    try:return agent_analysis.read(request.app.state.settings,fn,*args)
    except chat.Missing:raise HTTPException(404,detail={'code':'reminder_not_found','message':'提醒不存在。'}) from None
    except (ValueError,SQLAlchemyError):raise HTTPException(503,detail={'code':'reminders_unavailable','message':'提醒暂不可用，请检查数据库及迁移。'}) from None

@router.get('/summary',summary='查看提醒规则、处理状态和数量，不触发处理')
def summary(request:Request):
    value=read(request,repo.summary)
    value['processor_configured']=request.app.state.settings.reminder_processing_enabled
    value['poll_seconds']=request.app.state.settings.reminder_poll_seconds
    return value

@router.get('',summary='按优先级和时间查看关键词线索')
def listing(request:Request,person_id:UUID|None=None,status:Status|None='active',priority:Priority|None=None,
            limit:Annotated[int,Query(ge=1,le=50)]=30,cursor:UUID|None=None):
    rows=read(request,repo.listing,person_id,status,priority,limit,cursor)
    return {'items':rows[:limit],'next_cursor':rows[limit-1]['reminder_id'] if len(rows)>limit else None,'claim_support':'not_assessed'}

@router.get('/{reminder_id}',summary='查看规则命中、原文版本、合并项和审核记录')
def detail(request:Request,reminder_id:UUID):return read(request,repo.detail,reminder_id)

@private_router.post('/reminder-feedback',summary='本人提交误报或有用反馈，不改变全局提醒')
def feedback(request:Request,body:Feedback,token:str=Depends(credential)):
    return execute(request,token,repo.feedback,body)
