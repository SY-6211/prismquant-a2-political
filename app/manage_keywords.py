"""Local trusted administrator CLI; does not enable HTTP writes or collection."""
import argparse
import json
from pathlib import Path
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import SQLAlchemyError
from app.core.config import Settings
from app.core.database import database_url
from app.keyword_rules import Rule
from app.repositories import keywords as repo


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('list')
    c=sub.add_parser('draft');c.add_argument('--file',required=True,type=Path);c.add_argument('--rule-id',type=UUID);c.add_argument('--expected-revision',type=int)
    h=sub.add_parser('versions');h.add_argument('--rule-id',required=True,type=UUID)
    v=sub.add_parser('preview');v.add_argument('--rule-id',required=True,type=UUID);v.add_argument('--version-id',required=True,type=UUID);v.add_argument('--limit',type=int,choices=range(1,201),default=100,metavar='1..200')
    a=sub.add_parser('publish');a.add_argument('--rule-id',required=True,type=UUID);a.add_argument('--version-id',required=True,type=UUID);a.add_argument('--preview-id',required=True,type=UUID);a.add_argument('--expected-revision',required=True,type=int)
    for parser in (c,a):parser.add_argument('--note',required=True)
    args=p.parse_args();engine=None
    try:
        if hasattr(args,'note') and not 1<=len(args.note.strip())<=500:raise ValueError('Provide a note of 1..500 characters')
        if args.command=='draft':
            if args.file.stat().st_size>65536:raise ValueError('Rule file exceeds 64 KiB')
            definition=Rule.model_validate_json(args.file.read_text(encoding='utf-8-sig'))
        engine=create_engine(database_url(Settings()),poolclass=NullPool,hide_parameters=True,
            connect_args={'connect_timeout':5,'options':'-c statement_timeout=10000 -c lock_timeout=5000'})
        with engine.begin() as conn:
            if args.command=='list':result={'items':repo.listing(conn,100)[:100],'limit':100}
            elif args.command=='versions':result={'items':repo.history(conn,args.rule_id,limit=100)[:100],'limit':100}
            elif args.command=='draft':result=repo.draft(conn,definition,'local-cli',args.note,args.rule_id,args.expected_revision)
            elif args.command=='preview':result=repo.preview(conn,args.rule_id,args.version_id,'local-cli',args.limit)
            else:result=repo.publish(conn,args.rule_id,args.version_id,args.preview_id,args.expected_revision,'local-cli',args.note)
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    except (ValueError,OSError,SQLAlchemyError):
        print(json.dumps({'status':'failed','message':'操作未完成。请检查迁移、文件、ID、预览回执及当前 revision；未提交部分已回滚。'},ensure_ascii=False));return 1
    finally:
        if engine:engine.dispose()
    return 0


if __name__=='__main__':raise SystemExit(main())
