"""Project existing records locally; default is a read-only preview, no fetching."""
import argparse
import json

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.database import create_read_engine, database_url
from app.event_model import native_identity, snapshot_from_record
from app.repositories.events import save_snapshot


def project_records(connection, *, apply=False, limit=200):
    if not 1 <= limit <= 1000:
        raise ValueError('Limit must be 1..1000')
    rows = connection.execute(text('SELECT r.*, s.fetch_method, to_jsonb(a) AS archive_post '
        'FROM public_records r JOIN sources s ON s.source_id=r.source_id '
        'LEFT JOIN archive_post_details a ON a.record_id=r.record_id '
        'LEFT JOIN event_record_links l ON l.record_id=r.record_id WHERE l.record_id IS NULL '
        'ORDER BY r.record_id LIMIT :limit'), {'limit': limit}).mappings().all()
    prepared = []
    for row in rows:
        record = dict(row)
        snapshot = snapshot_from_record(record, record['archive_post'])
        platform, _ = native_identity(snapshot, record['record_id'])
        prepared.append((record, snapshot, platform))
    counts = {}
    for record, snapshot, platform in prepared:
        counts[platform] = counts.get(platform, 0) + 1
        if apply:
            archive = record['archive_post'] or {}
            save_snapshot(connection, record['source_id'], record['fetch_method'], snapshot, record['first_seen_at'],
                          record_id=record['record_id'], observation_kind='legacy_projection',
                          metadata={'archive_captured_at': archive.get('archive_captured_at'),
                                    'timing_note': 'Legacy first_seen_at; not a new publication measurement'})
    return {'mode': 'event_model_projection', 'applied': apply, 'selected': len(prepared), 'platforms': counts,
            'limit': limit, 'network_requested': False, 'legacy_rows_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Write event tables; preserve all legacy rows and switches')
    parser.add_argument('--limit', type=int, default=200)
    args = parser.parse_args()
    engine = None
    try:
        settings = Settings()
        engine = (create_engine(database_url(settings), poolclass=NullPool, hide_parameters=True,
                  connect_args={'connect_timeout': 5, 'options': '-c lock_timeout=5000 -c statement_timeout=30000'})
                  if args.apply else create_read_engine(settings))
        with engine.begin() as connection:
            result = project_records(connection, apply=args.apply, limit=args.limit)
    except Exception:
        print(json.dumps({'status': 'failed', 'message': '投影失败，事务已回滚；请检查迁移、权限和旧记录格式。'}, ensure_ascii=False))
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
