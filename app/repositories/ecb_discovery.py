"""ECB channel state; callers keep the existing source lease across transactions."""
import json
from sqlalchemy import text
from app.batch_ecb import SOURCE_ID


def ensure_state(conn):
    conn.execute(text('INSERT INTO ecb_discovery_state(source_id) VALUES (:id) ON CONFLICT DO NOTHING'),{'id':SOURCE_ID})


def state(conn):
    row = conn.execute(text('SELECT *,clock_timestamp() AS checked_at FROM ecb_discovery_state WHERE source_id=:id'),{'id':SOURCE_ID}).mappings().one_or_none()
    return dict(row) if row else None


def snapshot(conn, channel, entries, *, base=None, coverage=None):
    if channel not in ('web_index','rss'):
        raise ValueError('Unknown ECB channel')
    ensure_state(conn)
    current = state(conn)
    baseline = not current['index_ready' if channel=='web_index' else 'rss_ready']
    unique = {}
    for entry in entries:
        url = entry['url']
        if url in unique and unique[url] != entry:
            raise ValueError('Conflicting metadata in one ECB channel')
        unique[url] = entry
    pending = conn.execute(text("SELECT count(*) FROM ecb_discovery_candidates WHERE source_id=:id AND status='pending'"),{'id':SOURCE_ID}).scalar_one()
    known = set(conn.execute(text('SELECT url FROM ecb_discovery_candidates WHERE source_id=:id AND url=ANY(CAST(:urls AS text[]))'),
                            {'id':SOURCE_ID,'urls':list(unique)}).scalars())
    if pending+len(set(unique)-known)>2000:
        raise ValueError('ECB pending discovery capacity reached')
    for url,entry in unique.items():
        conn.execute(text('INSERT INTO ecb_discovery_candidates(source_id,url,first_channel,channel_metadata,status) '
            'VALUES (:id,:url,:channel,CAST(:metadata AS jsonb),:status) ON CONFLICT(source_id,url) DO UPDATE '
            'SET channel_metadata=ecb_discovery_candidates.channel_metadata || EXCLUDED.channel_metadata,last_seen_at=clock_timestamp()'),
            {'id':SOURCE_ID,'url':url,'channel':channel,'metadata':json.dumps({channel:entry},ensure_ascii=False,allow_nan=False),
             'status':'baseline' if baseline else 'pending'})
    if channel=='web_index':
        conn.execute(text('UPDATE ecb_discovery_state SET index_ready=true,index_base=:base,index_window=CAST(:window AS jsonb),'
            'coverage=CAST(:coverage AS jsonb) WHERE source_id=:id'),{'id':SOURCE_ID,'base':base,
            'window':json.dumps(entries,ensure_ascii=False,allow_nan=False),'coverage':json.dumps(coverage or {},allow_nan=False)})
    else:
        conn.execute(text('UPDATE ecb_discovery_state SET rss_ready=true WHERE source_id=:id'),{'id':SOURCE_ID})
    return {'channel':channel,'baseline_established':baseline,'returned_candidates':len(unique)}


def pending(conn, limit):
    return [dict(row) for row in conn.execute(text("SELECT * FROM ecb_discovery_candidates WHERE source_id=:id AND status='pending' "
        'ORDER BY first_seen_at,url LIMIT :limit'),{'id':SOURCE_ID,'limit':limit}).mappings()]


def mark(conn, url, status, reason, record_id=None):
    if status not in ('saved','needs_review'):
        raise ValueError('Invalid candidate outcome')
    conn.execute(text('UPDATE ecb_discovery_candidates SET status=:status,last_reason=:reason,record_id=CAST(:record AS uuid) '
        'WHERE source_id=:id AND url=:url'),{'id':SOURCE_ID,'url':url,'status':status,'reason':reason,'record':record_id})


def select_status(conn):
    current = state(conn)
    # The raw HTTP cache and response bodies are intentionally excluded.
    stats = [dict(r) for r in conn.execute(text('SELECT status,count(*) AS candidate_count FROM ecb_discovery_candidates '
        'WHERE source_id=:id GROUP BY status ORDER BY status'),{'id':SOURCE_ID}).mappings()]
    return {'state':({k:current[k] for k in ('index_ready','rss_ready','index_base','coverage','last_scan_at','retry_not_before',
            'failure_count','pause_reason','last_report')} if current else None),'candidates':stats,
            'discovery_interval_seconds':60,'publication_latency_verified':False}


def select_candidates(conn, limit=20, cursor=None, status_filter=None):
    return [dict(r) for r in conn.execute(text('SELECT url,first_channel,channel_metadata,first_seen_at,last_seen_at,status,record_id,last_reason '
        'FROM ecb_discovery_candidates WHERE source_id=:id AND (CAST(:cursor AS text) IS NULL OR url>:cursor) '
        'AND (CAST(:status AS text) IS NULL OR status=:status) ORDER BY url LIMIT :limit'),
        {'id':SOURCE_ID,'cursor':cursor,'status':status_filter,'limit':limit+1}).mappings()]
