"""Persistent per-source pacing and honest stage evidence; never infer publication seconds."""
import json,hashlib
from datetime import datetime,timezone
from uuid import uuid4
from sqlalchemy import text

KEYS=('ecb','trump','fed','eia','treasury')
def state(c,key):
 r=c.execute(text('SELECT *,clock_timestamp() AS now FROM speed_sources WHERE source=:s'),{'s':key}).mappings().one()
 return dict(r)
def due(row):
 if not row['enabled']:return 'disabled'
 if row['pause_reason']:return 'paused'
 if row['next_attempt_at'] and row['next_attempt_at']>row['now']:return 'backoff' if row.get('failures',0)>0 else 'not_due'
 return 'due'
def begin(c,key):
 row=state(c,key)
 if due(row)!='due':return {'status':due(row)}
 c.execute(text('UPDATE speed_sources SET last_attempt_at=clock_timestamp(),next_attempt_at=clock_timestamp()+make_interval(secs=>interval_seconds) WHERE source=:s'),{'s':key})
 return {'status':'started','state':row,'started_at':row['now']}
def finish(c,key,start,report):
 old=state(c,key);failure=report['status'] in ('failed','stopped','error','blocked_unfinished_run')
 count=old['failures']+1 if failure else 0
 pause=report.get('pause_reason') or ('repeated_failure_needs_review' if count>=3 else None)
 seconds=max(old['interval_seconds'],min(3600,old['interval_seconds']*(2**min(count,5))),report.get('retry_seconds') or 0)
 c.execute(text('''UPDATE speed_sources SET last_finished_at=clock_timestamp(),next_attempt_at=CASE WHEN :failure THEN clock_timestamp()+make_interval(secs=>:delay) WHEN CAST(:next_at AS timestamptz) IS NOT NULL THEN GREATEST(clock_timestamp(),CAST(:next_at AS timestamptz)) ELSE GREATEST(clock_timestamp(),last_attempt_at+make_interval(secs=>:delay)) END,
   failures=:failures,pause_reason=COALESCE(:pause,pause_reason),last_result=CAST(:report AS jsonb) WHERE source=:s'''),
   {'s':key,'delay':seconds,'failure':failure,'next_at':report.get('next_attempt_at'),'failures':count,'pause':pause,'report':json.dumps(report,default=str,ensure_ascii=False)})
 c.execute(text('INSERT INTO speed_rounds(run_id,source,started_at,finished_at,report) VALUES(:id,:s,:start,clock_timestamp(),CAST(:r AS jsonb))'),
   {'id':uuid4(),'s':key,'start':start,'r':json.dumps(report,ensure_ascii=False,default=str)})
 # A finite operational window; immutable source documents/events are never pruned here.
 c.execute(text('DELETE FROM speed_rounds WHERE source=:s AND run_id NOT IN (SELECT run_id FROM speed_rounds WHERE source=:s ORDER BY finished_at DESC LIMIT 200)'),{'s':key})
def frontier(c,key,items):
 row=state(c,key);baseline=not row['baseline_ready'];urls=[i['url'] for i in items]
 known=set(c.execute(text('SELECT url FROM speed_candidates WHERE source=:s AND url=ANY(CAST(:urls AS text[]))'),{'s':key,'urls':urls}).scalars())
 gap=bool(not baseline and urls and not known)
 pending=c.execute(text("SELECT count(*) FROM speed_candidates WHERE source=:s AND status='pending'"),{'s':key}).scalar_one()
 if pending+len(set(urls)-known)>500:raise ValueError('pending_capacity')
 for item in items:
  c.execute(text('''INSERT INTO speed_candidates(source,url,title,is_baseline,status) VALUES(:s,:url,:title,:base,:status)
    ON CONFLICT DO NOTHING'''),{'s':key,'url':item['url'],'title':item['title'],'base':baseline or gap,'status':'baseline' if baseline or gap else 'pending'})
 c.execute(text('UPDATE speed_sources SET baseline_ready=true WHERE source=:s'),{'s':key})
 return {'baseline':baseline,'frontier_rows':len(urls),'coverage_gap':gap,'new_urls':len(set(urls)-known)}
def pending(c,key):
 return [dict(r) for r in c.execute(text("SELECT * FROM speed_candidates WHERE source=:s AND status='pending' ORDER BY first_seen_at DESC,url LIMIT 2"),{'s':key}).mappings()]
def mark(c,key,url,status,document_id=None,note=None):
 c.execute(text('UPDATE speed_candidates SET status=:status,document_id=:doc,note=:note,checked_at=clock_timestamp() WHERE source=:s AND url=:url'),{'s':key,'url':url,'status':status,'doc':document_id,'note':note})
def delivery(c,key,version_id,detected,elapsed_ms,catchup=False):
 if not 0<=elapsed_ms<=1800000:return
 c.execute(text('''INSERT INTO speed_deliveries(version_id,source,detected_at,commit_returned_at,discovery_to_commit_ms,is_catchup)
 SELECT :v,:s,:at,clock_timestamp(),extract(epoch FROM(clock_timestamp()-CAST(:at AS timestamptz)))*1000,:catchup
 WHERE extract(epoch FROM(clock_timestamp()-CAST(:at AS timestamptz))) BETWEEN 0 AND 1800 ON CONFLICT DO NOTHING'''),{'v':version_id,'s':key,'at':detected,'catchup':catchup})
def control(c,key,enabled,*,resume_note=None):
 if key not in KEYS:raise ValueError('unknown_source')
 c.execute(text('UPDATE speed_sources SET enabled=:enabled,enabled_at=CASE WHEN :enabled AND NOT enabled THEN clock_timestamp() ELSE enabled_at END WHERE source=:s'),{'s':key,'enabled':enabled})
 if resume_note:
  if not 1<=len(resume_note.strip())<=500:raise ValueError('resume_note_required')
  c.execute(text("UPDATE speed_sources SET pause_reason=NULL,failures=0,next_attempt_at=NULL,last_result=last_result||jsonb_build_object('resume_note',CAST(:note AS text)) WHERE source=:s"),{'s':key,'note':resume_note.strip()})
 return state(c,key)
def overview(c):
 rows=[]
 for key in KEYS:
  row=state(c,key);row['status']=due(row)
  row['pending']=c.execute(text("SELECT count(*) FROM speed_candidates WHERE source=:s AND status='pending'"),{'s':key}).scalar_one()
  row['delivery_samples']=c.execute(text('SELECT count(*) FROM speed_deliveries WHERE source=:s AND NOT is_catchup'),{'s':key}).scalar_one()
  row['browser_notices']=c.execute(text('SELECT count(*) FROM speed_receipts r JOIN speed_deliveries d USING(version_id) WHERE d.source=:s AND NOT d.is_catchup'),{'s':key}).scalar_one()
  from app.baseline import distribution
  samples=list(c.execute(text('SELECT discovery_to_commit_ms FROM speed_deliveries WHERE source=:s AND NOT is_catchup ORDER BY commit_returned_at DESC LIMIT 1000'),{'s':key}).scalars())
  row['discovery_to_commit']=distribution(samples)
  row['measurement_start']='collection_round_start' if key in ('ecb','trump') else 'index_first_observed'
  receipts=list(c.execute(text('SELECT extract(epoch FROM(r.received_at-d.commit_returned_at))*1000 FROM speed_receipts r JOIN speed_deliveries d USING(version_id) WHERE d.source=:s AND NOT d.is_catchup AND r.received_at>=d.commit_returned_at ORDER BY r.received_at DESC LIMIT 1000'),{'s':key}).scalars())
  row['commit_to_notice_receipt']=distribution([float(x) for x in receipts]);row['receipt_evidence_kind']='client_reported_notice'
  row['publication_to_display_ms']=None;row['publication_latency_status']='not_measured';rows.append(row)
 return {'items':rows,'publication_to_display_verified':False,'target_seconds':60,'model_requests':0,'notice':'检查间隔与处理耗时不是发布到显示延迟；浏览器回执仅代表客户端声称呈现更新提示。'}
class ReceiptRateLimit(Exception):pass
def receipt(c,session_id,version_id,render_ms):
 c.execute(text('SELECT pg_advisory_xact_lock(348127615)'))
 row=c.execute(text('SELECT d.*,clock_timestamp() AS now FROM speed_deliveries d WHERE version_id=:v'),{'v':version_id}).mappings().one_or_none()
 if not row or row['is_catchup']:return {'recorded':False,'reason':'not_a_live_measured_delivery'}
 if not 0<=(row['now']-row['commit_returned_at']).total_seconds()<=600:return {'recorded':False,'reason':'receipt_window_closed'}
 if c.execute(text('SELECT 1 FROM speed_receipts WHERE session_id=:s AND version_id=:v'),{'s':session_id,'v':version_id}).scalar_one_or_none():return {'recorded':False,'reason':'duplicate'}
 if c.execute(text("SELECT count(*) FROM speed_receipts WHERE received_at>clock_timestamp()-interval '1 minute'")).scalar_one()>=120:raise ReceiptRateLimit()
 c.execute(text('INSERT INTO speed_receipts(session_id,version_id,notice_render_ms) VALUES(:s,:v,:ms)'),{'s':session_id,'v':version_id,'ms':render_ms})
 c.execute(text("DELETE FROM speed_receipts WHERE received_at<clock_timestamp()-interval '7 days'"))
 return {'recorded':True,'evidence_kind':'client_reported_notice','commit_to_receipt_upper_bound_ms':round((row['now']-row['commit_returned_at']).total_seconds()*1000,3),'publication_to_display_verified':False}
