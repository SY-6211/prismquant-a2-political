"""Local credential provisioning. Raw tokens never enter CLI arguments or stdout."""
import argparse
import json
import os
from pathlib import Path
from uuid import UUID,uuid4
from sqlalchemy import text
from app.chat_model import new_token
from app.core.config import PROJECT_ROOT,Settings
from app.repositories import chat as repo

PRIVATE=PROJECT_ROOT/'.runtime/chat-credentials'


def credential_path(path=None):
    root=PRIVATE.resolve()
    if not root.is_relative_to(PROJECT_ROOT.resolve()):raise ValueError('Private folder escaped project')
    target=(Path(path) if path else root/(uuid4().hex+'.json')).resolve()
    if not target.is_relative_to(root) or target.suffix!='.json':raise ValueError('Use a private JSON credential path')
    target.parent.mkdir(parents=True,exist_ok=True)
    return target


def provision(settings,*,name=None,user_id=None,days=30,path=None):
    if (name is None)==(user_id is None):raise ValueError('Choose a new user or existing user')
    target=credential_path(path)
    # Exclusive creation means a repeated command cannot overwrite a credential.
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    engine=None;commit_started=False
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            engine=repo.engine_for(settings)
            with engine.connect() as c:
                tx=c.begin()
                try:
                    user=repo.create_user(c,name) if name is not None else {'user_id':user_id}
                    token=new_token();issued=repo.issue_token(c,user['user_id'],token,days)
                    info={**issued,'access_token':token,'token_type':'Bearer','purpose':'private_agent_chat_user','display_name':user.get('display_name')}
                    json.dump(info,stream,ensure_ascii=False,default=str,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
                    commit_started=True;tx.commit()
                    return {'user_id':str(user['user_id']),'token_id':str(issued['token_id']),
                            'expires_at':issued['expires_at'].isoformat(),'credential_file':str(target),'secret_printed':False}
                except Exception:
                    if tx.is_active:tx.rollback()
                    raise
    except Exception:
        # On an uncertain commit, retain the private receipt for reconciliation;
        # never issue a second identity or token automatically.
        if not commit_started:target.unlink(missing_ok=True)
        raise
    finally:
        if engine:engine.dispose()


def main():
    parser=argparse.ArgumentParser(description='本机用户凭证管理；不调用模型，不显示令牌原文。')
    commands=parser.add_subparsers(dest='action',required=True)
    new=commands.add_parser('create-user');new.add_argument('--name',required=True)
    issue=commands.add_parser('issue-token');issue.add_argument('--user-id',required=True,type=UUID)
    for p in (new,issue):
        p.add_argument('--days',type=int,default=30);p.add_argument('--credential-file',type=Path)
    revoke=commands.add_parser('revoke-token');revoke.add_argument('--token-id',required=True,type=UUID)
    disable=commands.add_parser('disable-user');disable.add_argument('--user-id',required=True,type=UUID)
    commands.add_parser('list-users')
    args=parser.parse_args()
    try:
        settings=Settings()
        if args.action in ('create-user','issue-token'):
            result=provision(settings,name=getattr(args,'name',None),user_id=getattr(args,'user_id',None),days=args.days,path=args.credential_file)
        else:
            engine=repo.engine_for(settings)
            try:
                with engine.begin() as c:
                    if args.action=='revoke-token':result=repo.revoke_token(c,args.token_id)
                    elif args.action=='disable-user':result=repo.disable_user(c,args.user_id)
                    else:result={'items':[dict(r) for r in c.execute(text('SELECT user_id,display_name,created_at,disabled_at FROM agent_chat_users ORDER BY created_at DESC LIMIT 100')).mappings()]}
            finally:engine.dispose()
        print(json.dumps(result,ensure_ascii=False,default=str,indent=2));return 0
    except Exception:
        print(json.dumps({'status':'failed','message':'用户管理未完成。检查数据库、私有文件路径及现有凭证回执；不会自动覆盖文件或重新签发。'},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
