"""Finite official-person source verification. Existing records never become realtime samples."""
import argparse, hashlib, json, time
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import text
from app.core.config import Settings
from app.source_catalog import execute
from app.monitor_speed import run_source, allowed_person
from app.monitoring_status import summary
from app.scheduler_service import status
from app.web_http import Reader
from app.web_sources import get_source, discover
from app.web_extract import extract
from app.web_pilot import CACHE


def verify(settings):
    began=datetime.now(timezone.utc)
    before=execute(settings,summary,None,status(),settings.fast_discovery_enabled,readonly=True)
    row=next((r for r in before['items'] if r['source']=='fed' and r['latest_saved']),None)
    if row is None:raise ValueError('no_saved_official_person_sample')
    def stored(c):
        return dict(c.execute(text('SELECT r.retrieved_url,r.body_text,p.name_original FROM public_records r JOIN people p USING(person_id) WHERE r.record_id=:r'),
                              {'r':row['latest_saved']['record_id']}).mappings().one())
    saved=execute(settings,stored,readonly=True);source=get_source('fed')
    report=dict(source='fed',person_name=row['person_name'],person_id=str(row['person_id']),started_at=began.isoformat(),
                evidence_kind='known_saved_official_page_recheck',model_requests=0,model_cost_usd='0',
                saved_record={k:str(v) if v is not None else None for k,v in row['latest_saved'].items()},
                publication_to_display_verified=False)
    # Respect pacing and leases; never clear a source pause or force a scheduled scan.
    report['scheduled_round']=run_source(settings,'fed')
    clock=time.perf_counter()
    with Reader(source,cache_dir=CACHE,max_requests=4) as reader:
        page=reader.fetch(source.index);urls=discover(page.body,source,limit=50)
        article=reader.fetch(source.url(saved['retrieved_url'],detail=True))
        document=extract(article.body,url=article.url,source=source)
        report['http']=reader.audit
    report.update(index_contains_saved_url=any(r['url']==saved['retrieved_url'] for r in urls),
                  fetched_url=document['url'],extractor=document['extractor'],body_characters=document['body_characters'],
                  live_body_sha256=document['body_sha256'],stored_body_sha256=hashlib.sha256(saved['body_text'].encode()).hexdigest(),
                  body_matches_saved=document['body_sha256']==hashlib.sha256(saved['body_text'].encode()).hexdigest(),
                  speaker_name=document['person_name'],person_attribution_matches=document['person_name']==saved['name_original'],
                  source_recheck_duration_ms=round((time.perf_counter()-clock)*1000,3))
    def fresh(c):
        return [dict(r) for r in c.execute(text('''SELECT r.record_id,l.version_id,r.first_seen_at,d.detected_at,d.commit_returned_at
          FROM public_records r JOIN event_record_links l USING(record_id) JOIN speed_deliveries d USING(version_id)
          WHERE r.person_id=:p AND d.source='fed' AND NOT d.is_catchup AND d.detected_at>=:began
          ORDER BY d.detected_at DESC LIMIT 3'''),{'p':row['person_id'],'began':began}).mappings()]
    report['fresh_deliveries_during_check']=execute(settings,fresh,readonly=True)
    report['fresh_sample_status']='ready_for_browser_verification' if report['fresh_deliveries_during_check'] else 'waiting_for_real_new_publication'
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    report['notice']='实际复读已保存官方网页，验证提取与保存正文一致；复读耗时不计作首次发布至页面延迟。未合成新发言。'
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);args=p.parse_args()
    if args.out.exists():p.error('Output already exists')
    try:
        result=verify(Settings());args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf8')
        print(json.dumps({k:result[k] for k in ('source','person_name','body_matches_saved','person_attribution_matches','fresh_sample_status','model_requests')},ensure_ascii=False))
        return 0 if result['body_matches_saved'] and result['person_attribution_matches'] else 1
    except Exception:
        print(json.dumps(dict(status='requires_review',model_requests=0,message='官方来源链路核对未完成；未显示连接参数或凭证。'),ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
