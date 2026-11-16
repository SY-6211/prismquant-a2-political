from uuid import UUID
from fastapi import APIRouter,Request,Depends,BackgroundTasks
from app.api.chat import credential,execute
from app.repositories import web_qa as repo
from app.web_qa import execute_worker

router=APIRouter(prefix='/api/v1/agent-chat',tags=['受控在线人物问答'])
@router.get('/generation-budget',summary='查看本人本轮在线回答额度')
def budget(request:Request,token:str=Depends(credential)):
    return execute(request,token,repo.summary)
@router.post('/conversations/{conversation_id}/answers/{run_id}/execute',status_code=202,summary='在已授权有限额度内提交一次回答')
def generate(request:Request,conversation_id:UUID,run_id:UUID,background:BackgroundTasks,token:str=Depends(credential)):
    value=execute(request,token,repo.enqueue,conversation_id,run_id)
    if value['created']:background.add_task(execute_worker,request.app.state.settings,token,conversation_id,run_id)
    return value
