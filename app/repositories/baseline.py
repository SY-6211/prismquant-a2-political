from sqlalchemy import text
from app.core.database import create_read_engine
from app.collection_rules import SUPPORTED


def read_baseline(settings, hours, source=None):
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            configs = []
            for key, (sid, _, _, _) in SUPPORTED.items():
                if source and key != source:
                    continue
                row = connection.execute(text('SELECT name,poll_interval_seconds FROM sources WHERE source_id=:sid'), {'sid':sid}).mappings().one_or_none()
                configs.append({'source': key, 'source_name': row['name'] if row else key,
                                'configured_poll_interval_seconds': row['poll_interval_seconds'] if row else None})
            params = {'hours':hours, 'source':source}
            where = 'r.started_at >= now()-make_interval(hours => :hours)'
            if source:
                where += ' AND r.source=:source'
            runs = [dict(row) for row in connection.execute(text(
                'SELECT r.source,r.mode,r.timing FROM collection_runs r WHERE '+where+
                ' ORDER BY r.started_at DESC,r.run_id DESC LIMIT 1001'), params).mappings()]
            items = [dict(row) for row in connection.execute(text(
                'SELECT r.source,r.mode,i.status,i.write_outcome_unknown,i.timing '
                'FROM collection_runs r JOIN collection_run_items i ON i.run_id=r.run_id '
                "WHERE i.phase='detail' AND "+where+
                ' ORDER BY r.started_at DESC,r.run_id DESC,i.position ASC LIMIT 1001'), params).mappings()]
        return {'sources':configs,'runs':runs[:1000],'items':items[:1000],
                'truncated':len(runs)>1000 or len(items)>1000}
    finally:
        engine.dispose()
