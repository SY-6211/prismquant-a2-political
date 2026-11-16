"""Explicit finite HTML pilot. No scheduler hook, arbitrary-URL API or model calls."""
import argparse,json
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID,uuid4
from app.core.config import Settings,PROJECT_ROOT
from app.source_catalog import execute
from app.source_catalog_model import Review
from app.web_sources import SOURCES,get_source,discover
from app.web_http import Reader,FetchError
from app.web_extract import extract,ExtractionError
from app.repositories import web_pilot as repo

CACHE=PROJECT_ROOT/'.runtime/web-pilot/http-cache'

def run(key,*,limit=1,save=False,settings=None,reader_factory=Reader):
    source=get_source(key)
    if type(limit)!=int or not 1<=limit<=3:raise ValueError('limit_must_be_1_to_3')
    report={'run_id':str(uuid4()),'source':key,'started_at':datetime.now(timezone.utc).isoformat(),
      'mode':'finite_html_pilot','saved':save,'continuous_collection_enabled':False,'model_requests':0,'items':[],'http':[]}
    documents=[]
    try:
        with reader_factory(source,cache_dir=CACHE,max_requests=10) as reader:
            try:
                index=reader.fetch(source.index);items=discover(index.body,source,limit=limit)
                if not items:raise ExtractionError('no_supported_article_links')
                for item in items:
                    result={**item}
                    try:
                        page=reader.fetch(item['url']);document=extract(page.body,url=page.url,source=source)
                        result.update(status='extracted',body_characters=document['body_characters'],attribution=document['attribution'],
                          page_date=document['page_date'],date_precision=document['date_precision'],person_name=document['person_name'],body_sha256=document['body_sha256'])
                        documents.append((result,document,page.fetched_at))
                    except (FetchError,ExtractionError) as error:result.update(status='needs_review',code=str(error))
                    report['items'].append(result)
            finally:report['http']=reader.audit
    except (FetchError,ExtractionError) as error:report['failure_code']=str(error)
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    success=sum(row['status']=='extracted' for row in report['items']);report['extracted']=success
    report['status']='failed' if not success else ('completed_with_issues' if report.get('failure_code') or success<len(report['items']) else 'completed')
    if save:
        def persist(c):
            for result,document,observed_at in documents:result.update(repo.save_document(c,key,document,observed_at))
            repo.save_run(c,report)
        execute(settings or Settings(),persist,readonly=False)
    else:
        # Preview contains full extraction for CLI export only; no hidden DB writes.
        for result,document,_ in documents:result['extraction']=document
    return report

def review_candidates(c):
    from app.repositories import source_catalog as catalog
    from sqlalchemy import text
    notes={
      1:dict(decision='awaiting_access',access='restricted',discovery_url=None,transport='unselected',attribution='unverified',evidence_urls=['https://www.apollo.com/institutional/governance/terms-of-use'],note='2026-11-13核对：条款明确限制data mine/data scrape及机器人采集；不试抓Daily Spark，不启用采集，需另行获得许可。'),
      2:dict(decision='awaiting_access',access='public_documented',discovery_url='https://www.sec.gov/newsroom/speeches-statements',transport='html',attribution='unverified',evidence_urls=['https://www.sec.gov/about/webmaster-frequently-asked-questions','https://www.sec.gov/about/developer-resources'],note='原条目是Form 4说明入口。新增SEC讲话入口仅作为人物政策研究备选，不冒称Form 4已接入。官方要求声明机器人联系信息；本轮没有用户提供的联系地址，未做程序试抓，待配置后单独验证。'),
      28:dict(decision='awaiting_access',access='requires_permission',discovery_url=None,transport='unselected',attribution='unverified',evidence_urls=['https://www.imf.org/en/about/copyright-and-terms'],note='2026-11-13核对：允许个人非商业非系统下载，系统性下载需书面许可，且另有摘录和模型训练限制。本模块不据此开启持续抓取、全文复制或模型训练。'),
      49:dict(decision='pilot_candidate',access='public_documented',discovery_url=SOURCES['eia'].index,transport='html',attribution='reviewed',evidence_urls=[SOURCES['eia'].policy_url,SOURCES['eia'].index],note='已核对官方Today in Energy实际发布入口与转载说明。安排有限HTML正文试接，内容为机构能源研究背景，不自动绑定某个人物；正文、日期和来源独立保存，不采集图片，不启用持续采集。'),
    }
    results=[]
    for number,body in notes.items():
        ids=c.execute(text('SELECT candidate_id FROM source_catalog_candidates WHERE number=:number ORDER BY import_id'),{'number':number}).scalars().all()
        if len(ids)!=1:raise ValueError('catalog_batch_not_unique')
        current=catalog.one(c,ids[0]);expected=Review(expected_revision=current['revision'],relevance='background',**body)
        comparison=expected.model_dump(exclude={'expected_revision'})
        if current['review']==comparison:results.append({'number':number,'action':'already_reviewed'});continue
        if current['revision']!=0:results.append({'number':number,'action':'existing_review_preserved'});continue
        catalog.review(c,ids[0],expected,'codex-documented-source-review');results.append({'number':number,'action':'review_appended'})
    return {'items':results,'collection_enabled':False}

def main(argv=None):
    parser=argparse.ArgumentParser(description='有限网页试接与原文提取；不会启动持续监控或付费模型')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('sources');sub.add_parser('status');sub.add_parser('review-candidates')
    fetch=sub.add_parser('run');fetch.add_argument('--source',choices=[*SOURCES,'all'],required=True);fetch.add_argument('--limit',type=int,choices=range(1,4),default=1);fetch.add_argument('--save',action='store_true');fetch.add_argument('--out',type=Path)
    one=sub.add_parser('show');one.add_argument('document_id',type=UUID)
    imp=sub.add_parser('import-person');imp.add_argument('document_id',type=UUID)
    offline=sub.add_parser('extract-file');offline.add_argument('--html',type=Path,required=True);offline.add_argument('--url',required=True);offline.add_argument('--source',choices=SOURCES);offline.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if getattr(args,'out',None) and args.out.exists():raise ValueError('output_already_exists')
        if args.action=='sources':result={'items':[vars(s) for s in SOURCES.values()],'continuous_collection_enabled':False}
        elif args.action=='status':result=execute(Settings(),repo.overview,readonly=True)
        elif args.action=='review-candidates':result=execute(Settings(),review_candidates,readonly=False)
        elif args.action=='show':
            result=execute(Settings(),repo.detail,args.document_id,readonly=True)
            if result is None:raise ValueError('document_not_found')
        elif args.action=='import-person':result=execute(Settings(),repo.import_person,args.document_id,readonly=False)
        elif args.action=='extract-file':
            from app.source_catalog_model import public_url
            public_url(args.url)
            if args.html.stat().st_size>2*1024*1024:raise ValueError('file_too_large')
            source=get_source(args.source) if args.source else None
            if source:source.url(args.url,detail=True)
            result=extract(args.html.read_bytes(),url=args.url,source=source)
        else:result={'items':[run(key,limit=args.limit,save=args.save) for key in (SOURCES if args.source=='all' else [args.source])],'model_requests':0,'continuous_collection_enabled':False}
        if getattr(args,'out',None):
            with args.out.open('x',encoding='utf8') as f:json.dump(result,f,ensure_ascii=False,indent=2,default=str)
            result={'output':str(args.out),'model_requests':0,'continuous_collection_enabled':False}
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str));return 0
    except Exception as error:
        # Never print arbitrary network exceptions/connection settings.
        known=('invalid_','unknown_','limit_','not_','url_','output_','file_','document_','person_','source_','stored_','existing_','ambiguous_','catalog_','extraction_')
        code=str(error) if isinstance(error,ValueError) and str(error).startswith(known) else 'web_pilot_operation_failed'
        print(json.dumps({'status':'failed','code':code,'model_requests':0},ensure_ascii=False));return 1
if __name__=='__main__':raise SystemExit(main())
