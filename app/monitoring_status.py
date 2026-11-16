"""Small public source summary, explicit projection only; GET never fetches external sites."""
from datetime import datetime, timezone
from uuid import uuid5, NAMESPACE_URL
from sqlalchemy import text
from app.batch_ecb import SOURCE_ID as ECB
from app.preview_trump import SOURCE_ID as TRUMP

SOURCES = (( 'ecb', ECB, '官方网页索引＋RSS兜底'),
           ('trump', TRUMP, '第三方归档RSS'),
           ('fed', uuid5(NAMESPACE_URL, 'pq-web-source:fed'), '官方网页正文采集'))
OK = ('completed', 'completed_with_issues', 'baseline_created', 'no_new_candidates')


def visible_state(row, collector, configured, now):
    if not row['source_enabled'] or not row['person_enabled'] or not row['link_enabled'] or not row['enabled']:
        return 'disabled'
    if row['pause_reason'] or row['last_result'].get('status') == 'paused': return 'paused'
    if not configured: return 'not_scheduled'
    if collector.get('status') != 'running' or not collector.get('active'): return 'offline'
    if row['failures']: return 'backoff'
    if row['last_result'].get('status') in ('failed', 'error', 'blocked_unfinished_run', 'stopped'): return 'needs_review'
    if not row['last_success_at']: return 'awaiting_check'
    age = (now-row['last_success_at']).total_seconds()
    if age > max(180, row['interval_seconds']*3): return 'stale'
    return 'watching'


def summary(c, person_id, collector, configured):
    now = datetime.now(timezone.utc); items = []
    for key, source_id, method in SOURCES:
        people = c.execute(text('''SELECT p.person_id,p.name_original,p.name_zh,p.monitoring_enabled AS person_enabled,
            ps.enabled AS link_enabled,s.name AS source_name,s.enabled AS source_enabled
            FROM sources s JOIN person_sources ps USING(source_id) JOIN people p USING(person_id)
            WHERE s.source_id=:s AND (CAST(:p AS uuid) IS NULL OR p.person_id=:p)
            ORDER BY p.person_id LIMIT 100'''), {'s':source_id,'p':person_id}).mappings()
        state = c.execute(text('''SELECT enabled,interval_seconds,failures,pause_reason,last_result,
            (SELECT max(finished_at) FROM speed_rounds r WHERE r.source=s.source
              AND r.report->>'status'=ANY(CAST(:ok AS text[]))) AS last_success_at
            FROM speed_sources s WHERE source=:key'''), {'key':key,'ok':list(OK)}).mappings().one_or_none()
        if not state: continue
        for person in people:
            row = {**dict(person), **dict(state)}
            latest = c.execute(text('''SELECT r.record_id,r.title,r.published_at,r.page_date,r.first_seen_at,
                r.created_at AS stored_at,l.version_id,l.event_id,d.detected_at,d.commit_returned_at,
                d.is_catchup,d.discovery_to_commit_ms
                FROM public_records r LEFT JOIN event_record_links l USING(record_id)
                LEFT JOIN speed_deliveries d ON d.version_id=l.version_id
                WHERE r.source_id=:s AND r.person_id=:p
                ORDER BY r.first_seen_at DESC,r.record_id DESC LIMIT 1'''),
                {'s':source_id,'p':person['person_id']}).mappings().one_or_none()
            items.append(dict(source=key,source_name=person['source_name'],person_id=person['person_id'],
                person_name=person['name_zh'] or person['name_original'],method=method,
                status=visible_state(row,collector,configured,now),interval_seconds=state['interval_seconds'],
                last_checked_at=state['last_success_at'],
                last_check_result='no_new_content' if state['last_result'].get('status') in OK and
                    not state['last_result'].get('items') and not (state['last_result'].get('summary') or {}).get('inserted') else
                    'checked' if state['last_result'].get('status') in OK else 'needs_review' if state['last_result'].get('status')=='paused' else 'pending',
                latest_saved=dict(latest) if latest else None,
                source_delay_note='归档站收录延迟尚不可控。' if key=='trump' else '检查间隔不等于发布至页面的延迟。',
                publication_to_display_verified=False))
    return dict(checked_at=now,collector_status=collector.get('status','unknown'),items=items,
                publication_to_display_verified=False,model_requests=0)
