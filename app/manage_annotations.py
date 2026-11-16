"""Trusted local corpus administration. No blind export or fabricated human labels."""
import argparse
import json
from pathlib import Path
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import SQLAlchemyError
from app.core.config import Settings
from app.core.database import database_url
from app.repositories import annotations as repo
from app.api.annotations import capture_records


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('list')
    demo=sub.add_parser('synthetic-demo');demo.add_argument('--out',type=Path)
    c=sub.add_parser('create');c.add_argument('--name',required=True)
    for name in ('show','capture','seal','export','benchmark','report'):
        command=sub.add_parser(name);command.add_argument('--dataset-id',required=True,type=UUID)
        if name=='capture':
            command.add_argument('--limit',default=200,type=int,choices=range(1,501),metavar='1..500')
            command.add_argument('--after-record-id',type=UUID)
        if name in ('export','benchmark'):command.add_argument('--split',choices=('train','dev'),default='dev')
        if name in ('export','benchmark','report'):command.add_argument('--out',type=Path)
        if name=='benchmark':command.add_argument('--version-id',action='append',required=True,type=UUID)
        if name=='report':command.add_argument('--run-id',required=True,type=UUID)
    args=p.parse_args();engine=None
    try:
        if getattr(args,'out',None) and args.out.exists():raise ValueError('Refusing to overwrite an existing output file')
        engine=create_engine(database_url(Settings()),poolclass=NullPool,hide_parameters=True,
            connect_args={'connect_timeout':5,'options':'-c statement_timeout=15000 -c lock_timeout=5000'})
        with engine.begin() as conn:
            if args.command=='create':result=repo.create(conn,args.name)
            elif args.command=='synthetic-demo':
                from app.annotation_demo import create_demo
                result=create_demo(conn)
            elif args.command=='list':result={'items':repo.datasets(conn,100)[:100],'limit':100}
            elif args.command=='show':result=repo.dataset(conn,args.dataset_id)
            elif args.command=='capture':result=capture_records(conn,args.dataset_id,args.limit,args.after_record_id)
            elif args.command=='seal':result=repo.seal(conn,args.dataset_id)
            elif args.command=='export':result=repo.export_rows(conn,args.dataset_id,args.split)
            elif args.command=='benchmark':result=repo.benchmark(conn,args.dataset_id,args.split,args.version_id)
            else:result=repo.run_report(conn,args.dataset_id,args.run_id)
        output=''.join(json.dumps(row,ensure_ascii=False,default=str)+'\n' for row in result) if args.command=='export' else json.dumps(result,ensure_ascii=False,indent=2,default=str)
        if getattr(args,'out',None):
            with args.out.open('x',encoding='utf-8') as file:file.write(output)
            print(json.dumps({'status':'saved','path':str(args.out),'command':args.command},ensure_ascii=False))
        else:print(output)
    except (ValueError,OSError,SQLAlchemyError):
        print(json.dumps({'status':'failed','message':'检查迁移、ID、数据集状态、train/dev 范围及输出路径；标注需独立标注员入口，不能导出盲测。'},ensure_ascii=False));return 1
    finally:
        if engine:engine.dispose()
    return 0


if __name__=='__main__':raise SystemExit(main())
