"""Offline import and explicit local management: python -m app.source_catalog."""
import argparse,json,re
from pathlib import Path
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from app.core.config import PROJECT_ROOT,Settings
from app.core.database import database_url
from app.source_catalog_model import Review,public_url
from app.repositories import source_catalog as repo

MANIFEST=PROJECT_ROOT/'app/data/investat_source_catalog.json'
def load_manifest():
    if MANIFEST.stat().st_size>512000:raise ValueError('Manifest too large')
    value=json.loads(MANIFEST.read_text(encoding='utf8'))
    entries=value['entries']
    if value['schema_version']!=1 or not re.fullmatch('[a-f0-9]{64}',value['workbook_sha256']) or len(entries)!=84 or {e['number'] for e in entries}!=set(range(1,85)):raise ValueError('Invalid snapshot')
    labels=[]
    for entry in entries:
        public_url(entry['original']['reference_url'])
        if not entry['name'].strip() or len(entry['name'])>500 or len(entry['source_type'])>120:raise ValueError('Invalid source name')
        for label in entry['original']['labels']:
            if label['source_number']!=entry['number']:raise ValueError('Label/source mismatch')
            labels.append(label['number'])
    if value['original_label_count']!=114 or sorted(labels)!=list(range(1,115)):raise ValueError('Invalid original label coverage')
    return value

def execute(settings,fn,*args,readonly=False):
    engine=create_engine(database_url(settings),poolclass=NullPool,hide_parameters=True,connect_args={'connect_timeout':5,'options':'-c statement_timeout=10000 -c lock_timeout=5000'+(' -c default_transaction_read_only=on' if readonly else '')})
    try:
        with engine.begin() as c:return fn(c,*args)
    finally:engine.dispose()

def main(argv=None):
    parser=argparse.ArgumentParser(description='候选来源目录；导入和审核不会启用采集。')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('import-investat');sub.add_parser('summary')
    show=sub.add_parser('show');show.add_argument('candidate_id',type=UUID)
    edit=sub.add_parser('review');edit.add_argument('candidate_id',type=UUID);edit.add_argument('--file',type=Path,required=True);edit.add_argument('--actor',required=True)
    export=sub.add_parser('export');export.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        settings=Settings()
        if args.action=='import-investat':result=execute(settings,repo.import_manifest,load_manifest())
        elif args.action=='summary':result=execute(settings,repo.overview,readonly=True)
        elif args.action=='show':result=execute(settings,repo.one,args.candidate_id,readonly=True)
        elif args.action=='review':
            if args.file.stat().st_size>32768:raise ValueError('Review too large')
            body=Review.model_validate_json(args.file.read_text(encoding='utf8'))
            result=execute(settings,repo.review,args.candidate_id,body,args.actor)
        else:
            def export(c):return {'overview':repo.overview(c),'items':repo.listing(c,limit=10000)}
            result=execute(settings,export,readonly=True)
            with args.out.open('x',encoding='utf8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2,default=str)
            result={'exported':str(args.out),'collection_enabled':False}
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str));return 0
    except (Exception,):
        print('目录操作未完成：请检查迁移、候选编号、审核版本及JSON字段。未执行网络采集或模型调用。');return 1
if __name__=='__main__':raise SystemExit(main())
