"""Private conversation API. Saving a question never triggers a model request."""
from datetime import datetime
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException,Query,Request
from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from app.chat_model import TOKEN_RE,CreateConversation,EditConversation,UserMessage
from app.repositories import chat as repo
from app.repositories import qa
from app.agent_qa import Prepare,prepare

bearer=HTTPBearer(auto_error=False,scheme_name='ChatUserToken',description='本机独立签发的会话用户凭证；不能使用模型、关键词管理员或标注员密钥。')
router=APIRouter(prefix='/api/v1/agent-chat',tags=['Agent 用户与会话存储'])


def unauthorized():
    return HTTPException(401,detail={'code':'chat_auth_required','message':'需要有效的会话用户凭证。'},headers={'WWW-Authenticate':'Bearer'})


def credential(value:HTTPAuthorizationCredentials|None=Depends(bearer)):
    if value is None or not TOKEN_RE.fullmatch(value.credentials):raise unauthorized()
    return value.credentials


def execute(request,token,fn,*args):
    try:return repo.execute(request.app.state.settings,token,fn,*args)
    except repo.Unauthorized:raise unauthorized() from None
    except repo.Missing:raise HTTPException(404,detail={'code':'chat_not_found','message':'会话或记录不存在。'}) from None
    except repo.Conflict as error:raise HTTPException(409,detail={'code':'chat_conflict','message':str(error)}) from None
    except repo.Invalid as error:raise HTTPException(422,detail={'code':'chat_invalid','message':str(error)}) from None
    except (SQLAlchemyError,ValueError):
        raise HTTPException(503,detail={'code':'chat_unavailable','message':'会话存储暂不可用，请检查数据库及迁移。'}) from None


@router.get('/me',summary='查看当前凭证对应的用户身份')
def me(request:Request,token:str=Depends(credential)):
    return execute(request,token,repo.me)


@router.post('/conversations',summary='创建私有会话，不调用模型')
def create(request:Request,body:CreateConversation,token:str=Depends(credential)):
    return execute(request,token,repo.create_conversation,body)


@router.get('/conversations',summary='分页查看本人的会话历史',
            description='按会话创建时间倒序，q字面搜索标题及已保存问题；时间范围为会话创建时间，since含边界、until不含。归档会话显式选择archived=true。')
def listing(request:Request,token:str=Depends(credential),limit:Annotated[int,Query(ge=1,le=100)]=20,
            cursor:UUID|None=None,q:Annotated[str|None,Query(min_length=1,max_length=100)]=None,
            since:datetime|None=None,until:datetime|None=None,archived:bool=False):
    if any(value is not None and value.utcoffset() is None for value in (since,until)) or (since and until and since>=until):
        raise HTTPException(422,detail={'code':'chat_invalid_dates','message':'时间需包含时区，且开始时间早于结束时间。'})
    rows=execute(request,token,repo.list_conversations,limit,cursor,q,since,until,archived)
    return {'items':rows[:limit],'next_cursor':rows[limit-1]['conversation_id'] if len(rows)>limit else None}


@router.get('/conversations/{conversation_id}',summary='重新打开本人的会话')
def detail(request:Request,conversation_id:UUID,token:str=Depends(credential)):
    return execute(request,token,repo.conversation,conversation_id)


@router.patch('/conversations/{conversation_id}',summary='修改会话标题或归档状态')
def edit(request:Request,conversation_id:UUID,body:EditConversation,token:str=Depends(credential)):
    return execute(request,token,repo.edit_conversation,conversation_id,body)


@router.post('/conversations/{conversation_id}/messages',summary='保存用户问题，不生成回答',
             description='只接受用户问题；相同client_request_id和相同原文重复提交不重复保存，内容不同返回409。返回answer_status=separate_history，回答通过独立answers资源查询。')
def append(request:Request,conversation_id:UUID,body:UserMessage,token:str=Depends(credential)):
    return execute(request,token,repo.append_user,conversation_id,body)


@router.get('/conversations/{conversation_id}/messages',summary='按顺序分页查看本人的已保存问题')
def messages(request:Request,conversation_id:UUID,token:str=Depends(credential),
             limit:Annotated[int,Query(ge=1,le=100)]=50,after:Annotated[int,Query(ge=0,le=1000)]=0):
    return execute(request,token,repo.messages,conversation_id,limit,after)


@router.post('/conversations/{conversation_id}/answers',summary='保存问答输入；来源导航直接返回本地结果，不调用模型',description='read_official_sources=true 时，识别为来源查找的问题可有限读取固定官方入口；其他问题沿用资料准备与独立模型执行。')
def prepare_answer(request:Request,conversation_id:UUID,body:Prepare,token:str=Depends(credential)):
    # Reuse the same bounded HTTP error handling; prepare handles its own short
    # authenticated transactions and a separate read-only retrieval snapshot.
    try:
        run=prepare(request.app.state.settings,token,conversation_id,body)
        return execute(request,token,qa.detail,conversation_id,run['run_id'])
    except repo.Unauthorized:raise unauthorized() from None
    except repo.Missing:raise HTTPException(404,detail={'code':'chat_not_found','message':'会话或记录不存在。'}) from None
    except repo.Conflict as error:raise HTTPException(409,detail={'code':'chat_conflict','message':str(error)}) from None
    except repo.Invalid as error:raise HTTPException(422,detail={'code':'chat_invalid','message':str(error)}) from None
    except (SQLAlchemyError,ValueError):raise HTTPException(503,detail={'code':'qa_unavailable','message':'问答准备暂不可用，请检查输入范围、数据库及迁移。'}) from None


@router.get('/conversations/{conversation_id}/answers',summary='分页查看本人的问答与回答版本')
def answer_history(request:Request,conversation_id:UUID,token:str=Depends(credential),
                   limit:Annotated[int,Query(ge=1,le=100)]=20,cursor:UUID|None=None,message_id:UUID|None=None):
    rows=execute(request,token,qa.listing,conversation_id,limit,cursor,message_id)
    return {'items':rows[:limit],'next_cursor':rows[limit-1]['run_id'] if len(rows)>limit else None}


@router.get('/conversations/{conversation_id}/answers/{run_id}',summary='查看本人回答、引用和当时输入')
def saved_answer(request:Request,conversation_id:UUID,run_id:UUID,token:str=Depends(credential)):
    return execute(request,token,qa.detail,conversation_id,run_id)
