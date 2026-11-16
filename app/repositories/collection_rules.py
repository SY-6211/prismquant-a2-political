from sqlalchemy import text

from app.collection_rules import SUPPORTED
from app.core.database import create_read_engine


def select_context(connection, source):
    sid, pid, _, _ = SUPPORTED[source]
    row = connection.execute(text(
        'SELECT s.source_id,s.name AS source_name,s.feed_url,s.provenance_type,s.fetch_method, '
        's.poll_interval_seconds,s.enabled AS source_enabled,p.person_id,p.name_original AS person_name, '
        'p.monitoring_enabled,ps.source_id AS link_source_id,ps.person_id AS link_person_id,ps.enabled AS link_enabled '
        'FROM (SELECT CAST(:sid AS uuid) AS sid,CAST(:pid AS uuid) AS pid) e '
        'LEFT JOIN sources s ON s.source_id=e.sid LEFT JOIN people p ON p.person_id=e.pid '
        'LEFT JOIN person_sources ps ON ps.source_id=e.sid AND ps.person_id=e.pid'
    ), {'sid': sid, 'pid': pid}).mappings().one()
    return dict(row)


def read_contexts(settings, keys):
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            return {key: select_context(connection, key) for key in keys}
    finally:
        engine.dispose()
