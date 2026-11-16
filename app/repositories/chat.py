"""Authenticated, owner-scoped storage. No AI/network execution or arbitrary roles."""
from uuid import uuid4
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool
from app.core.database import database_url
from app.chat_model import creation_hash,token_digest


class Unauthorized(Exception):pass
class Missing(Exception):pass
class Conflict(Exception):pass
class Invalid(Exception):pass


def engine_for(settings):
    return create_engine(database_url(settings),poolclass=NullPool,hide_parameters=True,
        connect_args={'connect_timeout':5,'options':'-c statement_timeout=5000 -c lock_timeout=5000'})


def authenticate(c,token):
    try:digest=token_digest(token)
    except ValueError:raise Unauthorized() from None
    # Share locks fence token revocation/disable until this request finishes.
    row=c.execute(text('SELECT u.user_id,u.display_name,t.token_id,t.expires_at FROM agent_chat_tokens t '
        'JOIN agent_chat_users u USING(user_id) WHERE t.token_digest=:digest AND t.revoked_at IS NULL '
        'AND t.expires_at>now() AND u.disabled_at IS NULL FOR SHARE OF t,u'),{'digest':digest}).mappings().one_or_none()
    if row is None:raise Unauthorized()
    return dict(row)


def execute(settings,token,fn,*args):
    engine=engine_for(settings)
    try:
        with engine.begin() as c:
            identity=authenticate(c,token)
            return fn(c,identity['user_id'],*args)
    finally:engine.dispose()


def me(c,user_id):
    return dict(c.execute(text('SELECT user_id,display_name,created_at FROM agent_chat_users WHERE user_id=:user'),{'user':user_id}).mappings().one())


def create_user(c,name):
    name=name.strip()
    if not 1<=len(name)<=80 or any(ord(ch)<32 for ch in name):raise Invalid('用户显示名需为1到80个单行字符。')
    return dict(c.execute(text('INSERT INTO agent_chat_users(user_id,display_name) VALUES (:id,:name) RETURNING user_id,display_name,created_at'),{'id':uuid4(),'name':name}).mappings().one())


def issue_token(c,user_id,token,days):
    if not 1<=days<=90:raise Invalid('凭证有效期需为1到90天。')
    user=c.execute(text('SELECT user_id FROM agent_chat_users WHERE user_id=:id AND disabled_at IS NULL FOR SHARE'),{'id':user_id}).scalar_one_or_none()
    if user is None:raise Missing()
    return dict(c.execute(text("INSERT INTO agent_chat_tokens(token_id,user_id,token_digest,expires_at) "
        "VALUES (:id,:user,:digest,now()+make_interval(days=>:days)) RETURNING token_id,user_id,expires_at"),
        {'id':uuid4(),'user':user_id,'digest':token_digest(token),'days':days}).mappings().one())


def revoke_token(c,token_id):
    row=c.execute(text('UPDATE agent_chat_tokens SET revoked_at=coalesce(revoked_at,now()) WHERE token_id=:id RETURNING token_id,user_id,revoked_at'),{'id':token_id}).mappings().one_or_none()
    if row is None:raise Missing()
    return dict(row)


def disable_user(c,user_id):
    row=c.execute(text('UPDATE agent_chat_users SET disabled_at=coalesce(disabled_at,now()) WHERE user_id=:id RETURNING user_id,disabled_at'),{'id':user_id}).mappings().one_or_none()
    if row is None:raise Missing()
    return dict(row)


def conversation(c,user_id,conversation_id,lock=False):
    row=c.execute(text('SELECT conversation_id,title,person_id,revision,message_count,archived,created_at,updated_at '
        'FROM agent_chat_conversations WHERE conversation_id=:id AND user_id=:user'+(' FOR UPDATE' if lock else '')),
        {'id':conversation_id,'user':user_id}).mappings().one_or_none()
    if row is None:raise Missing()
    return dict(row)


def create_conversation(c,user_id,body):
    fingerprint=creation_hash(body.title,body.person_id)
    if body.person_id is not None and c.execute(text('SELECT person_id FROM people WHERE person_id=:id'),{'id':body.person_id}).scalar_one_or_none() is None:
        raise Invalid('监控人物不存在。')
    inserted=c.execute(text('INSERT INTO agent_chat_conversations(conversation_id,user_id,client_request_id,creation_hash,title,person_id) '
        'VALUES (:id,:user,:request,:hash,:title,:person) ON CONFLICT(user_id,client_request_id) DO NOTHING RETURNING conversation_id'),
        {'id':uuid4(),'user':user_id,'request':body.client_request_id,'hash':fingerprint,'title':body.title,'person':body.person_id}).scalar_one_or_none()
    row=c.execute(text('SELECT conversation_id,creation_hash FROM agent_chat_conversations WHERE user_id=:user AND client_request_id=:request'),
        {'user':user_id,'request':body.client_request_id}).mappings().one()
    if row['creation_hash']!=fingerprint:raise Conflict('相同提交标识已用于不同会话内容。')
    return {'created':inserted is not None,'conversation':conversation(c,user_id,row['conversation_id'])}


def edit_conversation(c,user_id,conversation_id,body):
    value=conversation(c,user_id,conversation_id,lock=True)
    if value['revision']!=body.expected_revision:raise Conflict('会话已变化，请重新读取后修改。')
    c.execute(text('UPDATE agent_chat_conversations SET title=:title,archived=:archived,revision=revision+1,updated_at=clock_timestamp() '
        'WHERE conversation_id=:id AND user_id=:user'),{'id':conversation_id,'user':user_id,
        'title':body.title if body.title is not None else value['title'],
        'archived':body.archived if body.archived is not None else value['archived']})
    return conversation(c,user_id,conversation_id)


def list_conversations(c,user_id,limit=20,cursor=None,query=None,since=None,until=None,archived=False):
    if cursor is not None:conversation(c,user_id,cursor)  # Foreign and nonexistent cursors both 404.
    term=None if not query else '%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    return [dict(r) for r in c.execute(text("SELECT v.conversation_id,v.title,v.person_id,v.revision,v.message_count,v.archived,v.created_at,v.updated_at "
        "FROM agent_chat_conversations v WHERE v.user_id=:user AND v.archived=:archived "
        "AND (CAST(:since AS timestamptz) IS NULL OR v.created_at>=:since) AND (CAST(:until AS timestamptz) IS NULL OR v.created_at<:until) "
        "AND (CAST(:cursor AS uuid) IS NULL OR (v.created_at,v.conversation_id)<(SELECT created_at,conversation_id FROM agent_chat_conversations WHERE conversation_id=:cursor AND user_id=:user)) "
        "AND (CAST(:term AS text) IS NULL OR v.title ILIKE :term ESCAPE '\\' OR EXISTS(SELECT 1 FROM agent_chat_messages m WHERE m.conversation_id=v.conversation_id AND m.content ILIKE :term ESCAPE '\\') "
        "OR EXISTS(SELECT 1 FROM agent_qa_runs r JOIN agent_qa_results a USING(run_id) WHERE r.conversation_id=v.conversation_id AND a.answer->>'answer_zh' ILIKE :term ESCAPE '\\')) "
        "ORDER BY v.created_at DESC,v.conversation_id DESC LIMIT :limit"),
        {'user':user_id,'limit':limit+1,'cursor':cursor,'term':term,'since':since,'until':until,'archived':archived}).mappings()]


def append_user(c,user_id,conversation_id,body):
    value=conversation(c,user_id,conversation_id,lock=True)
    previous=c.execute(text('SELECT * FROM agent_chat_messages WHERE conversation_id=:id AND client_request_id=:request'),
        {'id':conversation_id,'request':body.client_request_id}).mappings().one_or_none()
    if previous is not None:
        if previous['content']!=body.content:raise Conflict('相同提交标识已用于不同问题。')
        return {'created':False,'message':dict(previous),'conversation_revision':value['revision'],'answer_status':'separate_history'}
    if value['archived']:raise Conflict('会话已归档，请先取消归档。')
    if value['message_count']>=1000:raise Conflict('此会话已达到1000条问题上限，请新建会话。')
    row=c.execute(text("INSERT INTO agent_chat_messages(message_id,conversation_id,client_request_id,position,role,content) "
        "VALUES (:id,:conversation,:request,:position,'user',:content) RETURNING *"),
        {'id':uuid4(),'conversation':conversation_id,'request':body.client_request_id,'position':value['message_count']+1,'content':body.content}).mappings().one()
    c.execute(text('UPDATE agent_chat_conversations SET message_count=message_count+1,revision=revision+1,updated_at=clock_timestamp() '
        'WHERE conversation_id=:id AND user_id=:user'),{'id':conversation_id,'user':user_id})
    return {'created':True,'message':dict(row),'conversation_revision':value['revision']+1,'answer_status':'separate_history'}


def messages(c,user_id,conversation_id,limit=50,after=0):
    value=conversation(c,user_id,conversation_id)
    rows=[dict(r) for r in c.execute(text('SELECT message_id,conversation_id,client_request_id,position,role,content,created_at '
        'FROM agent_chat_messages WHERE conversation_id=:id AND position>:after ORDER BY position LIMIT :limit'),
        {'id':conversation_id,'after':after,'limit':limit+1}).mappings()]
    return {'conversation':value,'items':rows[:limit],'next_after':rows[limit-1]['position'] if len(rows)>limit else None,
            'answer_status':'separate_history'}
