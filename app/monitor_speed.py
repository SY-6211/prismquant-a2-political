"""Source discovery runtime. Each source has its own session lock and worker thread."""
import argparse,json,time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from uuid import UUID,uuid5,NAMESPACE_URL
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool
from app.core.config import Settings
from app.core.database import database_url
from app.repositories import monitor_speed as repo,web_pilot as documents
from app.source_catalog import execute
from app.web_sources import get_source,discover
from app.web_http import Reader,FetchError
from app.web_extract import extract,ExtractionError
from app.web_pilot import CACHE

def key_lock(key):
 import hashlib
 return int.from_bytes(hashlib.sha256(('speed-monitor:'+key).encode()).digest()[:8],'big',signed=True)

class Lease:
 def __init__(self,settings,key):self.settings=settings;self.key=key;self.acquired=False
 def __enter__(self):
  self.engine=create_engine(database_url(self.settings),poolclass=NullPool,hide_parameters=True,connect_args={'connect_timeout':5,'options':'-c statement_timeout=10000 -c lock_timeout=5000'})
  self.c=self.engine.connect();self.acquired=self.c.execute(text('SELECT pg_try_advisory_lock(:key)'),{'key':key_lock(self.key)}).scalar_one();self.pid=self.c.execute(text('SELECT pg_backend_pid()')).scalar_one();self.c.commit();return self
 def require(self):
  if not self.acquired or self.c.closed or self.c.invalidated:raise ValueError('lease_lost')
  if self.c.execute(text('SELECT pg_backend_pid()'),{}).scalar_one()!=self.pid:raise ValueError('lease_lost')
  row=repo.state(self.c,self.key)
  if not row['enabled'] or row['pause_reason']:raise ValueError('source_disabled_or_paused')
  return row
 def __exit__(self,*_):
  self.c.close();self.engine.dispose()

def allowed_person(c,document):
 if not document['eligible_for_person_import']:return False
 source_id=uuid5(NAMESPACE_URL,'pq-web-source:fed')
 return bool(c.execute(text('''SELECT EXISTS(SELECT 1 FROM people p JOIN person_sources ps USING(person_id) JOIN sources s USING(source_id)
 WHERE p.name_original=:name AND p.monitoring_enabled AND ps.enabled AND s.enabled AND s.source_id=:s AND s.fetch_method='web_index')'''),{'name':document['person_name'],'s':source_id}).scalar_one())

def html_round(lease,checkpoint,*,reader_factory=Reader):
 source=get_source(lease.key);report={'source':source.key,'transport':'web_index','items':[],'publication_to_display_verified':False};start=time.perf_counter();stages={'index_ms':0,'detail_ms':0,'extract_ms':0,'persist_ms':0}
 with reader_factory(source,cache_dir=CACHE,max_requests=12,checkpoint=checkpoint) as reader:
  phase=time.perf_counter();page=reader.fetch(source.index);items=discover(page.body,source,limit=50);stages['index_ms']+=round((time.perf_counter()-phase)*1000,3)
  if not items:raise ValueError('index_template_changed')
  with lease.c.begin():control=lease.require();frontier=repo.frontier(lease.c,source.key,items);todo=repo.pending(lease.c,source.key)
  report['frontier']=frontier
  if frontier['coverage_gap']:
   report.update(status='paused',pause_reason='frontier_gap_needs_review');return report
  for item in todo:
   checkpoint();phase=time.perf_counter();page=reader.fetch(item['url']);stages['detail_ms']+=round((time.perf_counter()-phase)*1000,3)
   phase=time.perf_counter();document=extract(page.body,url=page.url,source=source);stages['extract_ms']+=round((time.perf_counter()-phase)*1000,3)
   phase=time.perf_counter()
   with lease.c.begin():
    lease.require();saved=documents.save_document(lease.c,source.key,document,item['first_seen_at']);record=None
    if allowed_person(lease.c,document):record=documents.import_person(lease.c,saved['document_id'])
    repo.mark(lease.c,source.key,item['url'],'saved' if record else 'staged',saved['document_id'],'机构背景或非已启用人物保留为网页样本。' if not record else None)
   stages['persist_ms']+=round((time.perf_counter()-phase)*1000,3)
   elapsed=max(0,(datetime.now(timezone.utc)-item['first_seen_at']).total_seconds()*1000)
   if record and record['action']=='inserted':
    catchup=not document['page_date'] or not control['enabled_at'] or document['page_date']<control['enabled_at'].date().isoformat()
    with lease.c.begin():lease.require();repo.delivery(lease.c,source.key,record['version_id'],item['first_seen_at'],elapsed,catchup=catchup)
   report['items'].append({'url':item['url'],'document_id':saved['document_id'],'status':'saved' if record else 'staged','new_document':saved['created']})
  report['http']=reader.audit;report['stages_ms']=stages;report['conditional_cache_hits']=sum(x.get('http_status')==304 for x in reader.audit)
 report.update(status='baseline_created' if frontier['baseline'] else 'completed',round_duration_ms=round((time.perf_counter()-start)*1000,3));return report

def run_source(settings,key,*,checkpoint=None,reader_factory=Reader):
 checkpoint=checkpoint or (lambda:None)
 with Lease(settings,key) as lease:
  if not lease.acquired:return {'source':key,'status':'skipped_busy'}
  with lease.c.begin():started=repo.begin(lease.c,key)
  if started['status']!='started':return {'source':key,'status':started['status']}
  began=started['started_at'];clock=time.perf_counter()
  def check():
   checkpoint()
   with lease.c.begin():lease.require()
  try:
   check()
   if key in ('fed','eia','treasury'):report=html_round(lease,check,reader_factory=reader_factory)
   else:
    if key=='ecb':
     from app.ecb_fast import run_ecb_fast
     result=run_ecb_fast(settings,3,commit=True,checkpoint=check)
    else:
     from app.scheduler import run_source as legacy
     result=legacy(settings,'trump',3,checkpoint=check)
    if result.get('run_id'):
     with lease.c.begin():
      lease.require()
      inserted=list(lease.c.execute(text('''SELECT l.version_id,r.first_seen_at,r.published_at,r.page_date FROM collection_run_items i
        JOIN public_records r USING(record_id) JOIN event_record_links l USING(record_id)
        WHERE i.run_id=:run AND i.status='inserted' ''') ,{'run':result['run_id']}).mappings())
      for row in inserted:
       elapsed=max(0,(datetime.now(timezone.utc)-began).total_seconds()*1000)
       published=row['published_at'];catchup=published is None or published<started['state']['enabled_at']
       repo.delivery(lease.c,key,row['version_id'],began,elapsed,catchup=catchup)
    report={'source':key,'transport':'web_index+rss' if key=='ecb' else 'rss','status':result['status'],'run_id':result.get('run_id'),'summary':result.get('summary'),'reason':result.get('stop_reason') or result.get('reason'),'publication_to_display_verified':False,'fast_source_missing':key=='trump'}
    # Follow the adapter's actual last-attempt/finish clock; an outer not-due check must not add a second interval.
    with lease.c.begin():
     if key=='ecb':
      from app.batch_ecb import SOURCE_ID
      next_at=lease.c.execute(text('SELECT last_scan_at+interval \'60 seconds\' FROM ecb_discovery_state WHERE source_id=:id'),{'id':SOURCE_ID}).scalar_one_or_none()
     else:
      from app.preview_trump import SOURCE_ID
      next_at=lease.c.execute(text('SELECT GREATEST(q.last_attempt_at,q.last_finished_at)+make_interval(secs=>s.poll_interval_seconds) FROM collection_schedule q JOIN sources s USING(source_id) WHERE source_id=:id'),{'id':SOURCE_ID}).scalar_one_or_none()
     if next_at:report['next_attempt_at']=next_at
    if result.get('write_outcome_unknown'):report['pause_reason']='write_outcome_unknown'
   report['round_duration_ms']=round((time.perf_counter()-clock)*1000,3)
  except FetchError as error:
   report={'source':key,'status':'skipped_busy' if str(error)=='source_busy' else 'failed','code':str(error),'http_status':error.http_status,'retry_seconds':error.retry_seconds,'publication_to_display_verified':False}
   if error.http_status in (401,403) or (error.http_status==429 and error.retry_seconds is None) or str(error) in ('robots_disallowed','robots_html_response','host_clock_requires_review'):report['pause_reason']=str(error)
  except (ExtractionError,ValueError) as error:
   # No blind retries within a round. Persistent state applies backoff to subsequent rounds.
   report={'source':key,'status':'cancelled' if str(error) in ('worker_stop_requested','source_disabled_or_paused') else 'failed','code':'stopped_or_requires_review','publication_to_display_verified':False}
  except Exception:
   report={'source':key,'status':'failed','code':'operation_outcome_requires_review','pause_reason':'operation_outcome_requires_review','publication_to_display_verified':False}
  if lease.c.closed or lease.c.invalidated:raise ValueError('lease_lost')
  with lease.c.begin():
   if lease.c.execute(text('SELECT pg_backend_pid()')).scalar_one()!=lease.pid:raise ValueError('lease_lost')
   repo.finish(lease.c,key,began,report)
  return report

class Dispatcher:
 def __init__(self,settings,checkpoint=None):self.settings=settings;self.checkpoint=checkpoint;self.pool=ThreadPoolExecutor(max_workers=5,thread_name_prefix='pq-discovery');self.futures={};self.results={}
 def tick(self):
  for key in repo.KEYS:
   future=self.futures.get(key)
   if future and future.done():
    try:self.results[key]=future.result()
    except Exception:self.results[key]={'source':key,'status':'failed','reason':'状态需核对，未显示异常细节。'}
    self.futures.pop(key)
   if key not in self.futures:self.futures[key]=self.pool.submit(run_source,self.settings,key,checkpoint=self.checkpoint)
  return {'mode':'continuous_discovery_dispatch','items':[self.results.get(k,{'source':k,'status':'checking'}) for k in repo.KEYS]}
 def close(self):self.pool.shutdown(wait=True,cancel_futures=True)

def main(argv=None):
 p=argparse.ArgumentParser(description='持续发现控制；免费读取，不提交模型任务。');p.add_argument('action',choices=('status','enable','disable','once','resume'));p.add_argument('--source',choices=repo.KEYS);p.add_argument('--note');args=p.parse_args(argv)
 try:
  settings=Settings()
  if args.action=='status':result=execute(settings,repo.overview,readonly=True)
  elif args.action=='once':result={'items':[run_source(settings,k) for k in ([args.source] if args.source else repo.KEYS)]}
  else:
   if not args.source:raise ValueError('source_required')
   if args.action=='resume' and not args.note:raise ValueError('resume_note_required')
   result=execute(settings,lambda c:repo.control(c,args.source,args.action!='disable',resume_note=args.note if args.action=='resume' else None),readonly=False)
  print(json.dumps(result,ensure_ascii=False,indent=2,default=str));return 0
 except Exception:print('监控速度操作未完成，请核对配置、迁移和运行状态；未显示凭证，未调用模型。');return 1
if __name__=='__main__':raise SystemExit(main())
