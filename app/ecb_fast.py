"""One finite ECB index/RSS discovery round; optional confirmed-page ingestion."""
import argparse
from datetime import datetime,timezone
import json

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.batch_ecb import SOURCE_ID
from app.collection_execution import SourceLease
from app.collection_failures import http_failure,recovery_policy
from app.collection_guard import CollectionBlocked
from app.collection_rules import evaluate_rule
from app.collection_runs import execute_recorded
from app.core.config import Settings
from app.ecb_fast_http import ECBReader,USER_AGENT
from app.import_ecb_speech import save_speech
from app.preview_ecb_speech import parse_speech
from app.repositories.collection_rules import select_context
from app.repositories.ecb_discovery import ensure_state,mark,pending,state


def _items(settings,limit,client,lease,commit,checkpoint,report):
    reader = ECBReader(client,lease,checkpoint=checkpoint,max_requests=5+limit)
    writing = False
    try:
        discovery = reader.discover()
        report['discovery']=discovery
        with lease.connection.begin():
            selected = pending(lease.connection,limit)
            total = lease.connection.execute(text("SELECT count(*) FROM ecb_discovery_candidates WHERE source_id=:id AND status='pending'"),{'id':SOURCE_ID}).scalar_one()
        report['selection'].update(candidate_count=total,selected=len(selected) if commit else 0,
                                    not_selected_due_limit=max(0,total-limit) if commit else total)
        if not commit:
            report['status']='no_new_candidates' if not selected else 'completed'
            return report
        report['items']=[{'url':row['url'],'rss_title':row['channel_metadata'][row['first_channel']]['title'],
                          'status':'not_attempted','reason':'等待详情归属校验。'} for row in selected]
        for row,item in zip(selected,report['items']):
            report['summary']['attempted']+=1
            body = reader.fetch(row['url'])
            detail = parse_speech(body,row['url'],include_body=True)
            if detail['verification_status']!='verified_page_attribution':
                item.update(status='needs_review',reason=detail['reason'])
                with lease.connection.begin():
                    lease.require(automatic=True)
                    mark(lease.connection,row['url'],'needs_review',detail['reason'])
            else:
                if checkpoint: checkpoint()
                writing = True
                with lease.connection.begin():
                    lease.require(automatic=True,lock_settings=True)
                    entry = row['channel_metadata'][row['first_channel']]
                    result = save_speech(lease.connection,detail,entry,row['first_seen_at'],
                        discovery_transport=row['first_channel'],observed_at=datetime.fromisoformat(reader.requests[-1]['observed_at']),
                        observation_metadata={'discovered_at':row['first_seen_at'].isoformat(),'channels':row['channel_metadata'],
                                              'detail_http_observation':reader.requests[-1],
                                              'reported_publication_time_verified':False})
                    mark(lease.connection,row['url'],'needs_review' if result['action']=='needs_review' else 'saved',
                         result['message'],result['record_id'])
                writing = False
                item.update(status=result['action'],reason=result['message'],record_id=result['record_id'])
                report['saved'] = report['saved'] or result['action']=='inserted'
            report['summary'][item['status']]+=1
        report['status']='completed_with_issues' if report['summary']['needs_review'] else ('completed' if selected else 'no_new_candidates')
    except httpx.HTTPStatusError as exc:
        report.update(status='stopped',stop_reason='ECB 返回非预期 HTTP 状态，当前轮停止。',**http_failure(exc))
    except httpx.RequestError:
        report.update(status='stopped',failure_kind='network',stop_reason='ECB 连接失败或超时。')
    except KeyboardInterrupt:
        report.update(status='interrupted',stop_reason='已停止当前轮；已提交内容保留。')
        if writing: report['write_outcome_unknown']=True
    except SQLAlchemyError:
        report.update(status='stopped',write_outcome_unknown=True,stop_reason='数据库操作结果需核对，已停止当前轮。')
    except (ValueError,KeyError,TypeError,IndexError,OverflowError):
        report.update(status='stopped',stop_reason='ECB 配置、权限、缓存或页面结构需核对。')
    finally:
        resolved = sum(report['summary'].get(key,0) for key in ('inserted','already_exists','needs_review','failed','interrupted'))
        if report['summary']['attempted']>resolved:
            begun = report['items'][resolved]
            begun.update(status='interrupted' if report['status']=='interrupted' else 'failed',reason=report.get('stop_reason'))
            if report.get('write_outcome_unknown'): begun['write_outcome_unknown']=True
            report['summary'][begun['status']]+=1
        report['summary']['not_attempted']=sum(item['status']=='not_attempted' for item in report['items'])
        report['http_observations']=reader.requests
    return report


def run_ecb_fast(settings,limit=3,*,commit=True,checkpoint=None,client_factory=None):
    if type(limit) is not int or not 1<=limit<=5:
        raise ValueError('ECB detail limit must be 1..5')
    report = {'source':'ecb','status':'stopped','saved':False,'selection':{},'items':[],
              'summary':dict(attempted=0,inserted=0,already_exists=0,needs_review=0,failed=0,interrupted=0,not_attempted=0),
              'timing':{'input_kind':'provided_items'},'publication_latency_verified':False}
    try:
        if checkpoint: checkpoint()
        with SourceLease(settings,'ecb') as lease:
            if not lease.acquired:
                return {**report,'status':'skipped_busy'}
            with lease.connection.begin():
                decision = evaluate_rule('ecb',select_context(lease.connection,'ecb'))
                if not decision.eligible:
                    return {**report,'status':decision.status}
                ensure_state(lease.connection)
                current = state(lease.connection)
                if current['pause_reason']:
                    return {**report,'status':'paused'}
                if current['retry_not_before'] and current['retry_not_before']>current['checked_at']:
                    return {**report,'status':'backoff','retry_not_before':current['retry_not_before']}
                if current['last_scan_at'] and (current['checked_at']-current['last_scan_at']).total_seconds()<60:
                    return {**report,'status':'not_due'}
                if lease.connection.execute(text("SELECT count(*) FROM collection_runs WHERE source_id=:id AND status='running'"),{'id':SOURCE_ID}).scalar_one():
                    return {**report,'status':'blocked_unfinished_run'}
                legacy = lease.connection.execute(text('SELECT pause_reason,retry_not_before,clock_timestamp() AS checked_at FROM collection_schedule WHERE source_id=:id'),{'id':SOURCE_ID}).mappings().one_or_none()
                if legacy and legacy['pause_reason']: return {**report,'status':'paused'}
                if legacy and legacy['retry_not_before'] and legacy['retry_not_before']>legacy['checked_at']:
                    return {**report,'status':'backoff','retry_not_before':legacy['retry_not_before']}
                lease.connection.execute(text('UPDATE ecb_discovery_state SET last_scan_at=clock_timestamp() WHERE source_id=:id'),{'id':SOURCE_ID})
            report.update(source_enabled=True,monitoring_enabled=True,link_enabled=True)
            factory = client_factory or (lambda:httpx.Client(timeout=httpx.Timeout(15,connect=10),follow_redirects=False,headers={'User-Agent':USER_AGENT}))
            with factory() as client:
                def runner(config,count,http):
                    return _items(config,count,http,lease,commit,checkpoint,report)
                report = execute_recorded(settings,'ecb',limit,client,runner,mode='automatic_batch_import') if commit else runner(settings,limit,client)
            with lease.connection.begin():
                lease.require()
                for observation in report.get('http_observations',[]):
                    if observation['url'].endswith('/versions.json') and observation['state'] in ('received','not_modified'):
                        lease.connection.execute(text('UPDATE ecb_discovery_state SET last_scan_at=GREATEST(last_scan_at,CAST(:at AS timestamptz)) WHERE source_id=:id'),
                                                 {'id':SOURCE_ID,'at':observation['observed_at']})
                failures,retry,pause = recovery_policy(report,current['failure_count'],60,datetime.now(timezone.utc))
                lease.connection.execute(text('UPDATE ecb_discovery_state SET failure_count=:failures,retry_not_before=:retry,'
                    "pause_reason=:pause,last_report=CAST(:report AS jsonb) || jsonb_build_object('recovery_notes',COALESCE(last_report->'recovery_notes','[]'::jsonb)) WHERE source_id=:id"),
                    {'id':SOURCE_ID,'failures':failures,'retry':retry,'pause':pause,'report':json.dumps(report,ensure_ascii=False,allow_nan=False,default=str)})
            return report
    except (SQLAlchemyError,ValueError):
        return {**report,'status':'stopped','write_outcome_unknown':True,'stop_reason':'执行会话或数据库状态需核对；未显示凭证。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once',action='store_true')
    mode.add_argument('--resume-source',action='store_true',help='人工核对后解除本索引渠道暂停；不会改变监控开关或未结束运行')
    parser.add_argument('--note')
    parser.add_argument('--commit',action='store_true',help='保存归属核对通过的新候选；默认仅建立/更新发现状态及缓存')
    parser.add_argument('--limit',type=int,default=3)
    args = parser.parse_args(argv)
    if args.resume_source:
        if not args.note or not 1<=len(args.note.strip())<=500 or args.commit:
            parser.error('渠道恢复需提供 1–500 字符 --note，且不能与 --commit 同用')
        with SourceLease(Settings(),'ecb') as lease:
            if not lease.acquired: return 2
            with lease.connection.begin():
                lease.require()
                ensure_state(lease.connection)
                lease.connection.execute(text("UPDATE ecb_discovery_state SET pause_reason=NULL,retry_not_before=NULL,failure_count=0, "
                    "last_report=jsonb_set(last_report,'{recovery_notes}',COALESCE(last_report->'recovery_notes','[]'::jsonb) || "
                    "jsonb_build_array(jsonb_build_object('note',CAST(:note AS text),'at',clock_timestamp()))) WHERE source_id=:id"),
                    {'id':SOURCE_ID,'note':args.note.strip()})
        print(json.dumps({'status':'channel_resumed','network_requested':False}))
        return 0
    report = run_ecb_fast(Settings(),args.limit,commit=args.commit)
    print(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    return 1 if report['status'] in ('stopped','interrupted','paused','blocked_unfinished_run') else 0


if __name__=='__main__':
    raise SystemExit(main())
