from sqlalchemy import text

from app.core.database import create_read_engine


def select_runs(connection, limit, cursor=None, source=None, status=None):
    predicates, params = [], {'limit': limit + 1}
    if cursor is not None:
        predicates.append('(started_at,run_id)<(SELECT started_at,run_id FROM collection_runs WHERE run_id=:cursor)')
        params['cursor'] = cursor
    for key, value in (('source', source), ('status', status)):
        if value is not None:
            predicates.append(key + '=:' + key)
            params[key] = value
    where = ' WHERE ' + ' AND '.join(predicates) if predicates else ''
    return [dict(row) for row in connection.execute(text(
        'SELECT * FROM collection_runs' + where + ' ORDER BY started_at DESC,run_id DESC LIMIT :limit'
    ), params).mappings()]


def select_run(connection, run_id):
    row = connection.execute(text('SELECT * FROM collection_runs WHERE run_id=:id'), {'id': run_id}).mappings().one_or_none()
    if row is None:
        return None
    result = dict(row)
    result['items'] = [dict(item) for item in connection.execute(text(
        "SELECT * FROM collection_run_items WHERE run_id=:id ORDER BY CASE phase WHEN 'precheck' THEN 0 ELSE 1 END,position"
    ), {'id': run_id}).mappings()]
    return result


def read_runs(settings, limit, cursor=None, source=None, status=None):
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            return select_runs(connection, limit, cursor, source, status)
    finally:
        engine.dispose()


def read_run(settings, run_id):
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            # Stable snapshot so concurrent finalization cannot mix header and detail states.
            connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            return select_run(connection, run_id)
    finally:
        engine.dispose()
