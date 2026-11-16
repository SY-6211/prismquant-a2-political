"""One transactional clock, so a committed cursor never skips a late commit."""
from sqlalchemy import text

RETAIN = 10000
PAGE = 100


def lock_clock(connection):
    # Acquire BEFORE event locks. All live snapshot transactions use this order.
    # No HTTP or model work is done while holding this short write transaction.
    connection.execute(text('SELECT position FROM content_stream_clock WHERE singleton FOR UPDATE')).scalar_one()


def append_change(connection, *, event_id, version_id, record_id, person_id, kind, review_status):
    position = connection.execute(text('UPDATE content_stream_clock SET position=position+1 '
        'WHERE singleton RETURNING position')).scalar_one()
    connection.execute(text('INSERT INTO content_changes '
        '(position,event_id,version_id,record_id,person_id,kind,review_status) '
        'VALUES (:position,:event,:version,:record,:person,:kind,:review)'),
        {'position': position, 'event': event_id, 'version': version_id, 'record': record_id,
         'person': person_id, 'kind': kind, 'review': review_status})
    connection.execute(text('DELETE FROM content_changes WHERE position<=:cutoff'), {'cutoff': position-RETAIN})


def select_batch(connection, after=None):
    # Called in a REPEATABLE READ transaction: clock and rows share one snapshot.
    row = connection.execute(text('SELECT epoch,position FROM content_stream_clock WHERE singleton')).mappings().one()
    floor = connection.execute(text('SELECT min(position) FROM content_changes')).scalar_one()
    head = row['position']
    result = {'epoch': row['epoch'].hex, 'head': head, 'floor': head if floor is None else floor-1, 'items': []}
    if after is not None and after[0] == result['epoch'] and result['floor'] <= after[1] <= head:
        result['items'] = [dict(item) for item in connection.execute(text(
            'SELECT position,event_id,version_id,record_id,person_id,kind,review_status,created_at '
            'FROM content_changes WHERE position>:after ORDER BY position LIMIT :limit'),
            {'after': after[1], 'limit': PAGE}).mappings()]
    return result


def read_batch(settings, after=None):
    from app.core.database import create_read_engine
    engine = create_read_engine(settings)
    try:
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as conn:
            return select_batch(conn, after)
    finally:
        engine.dispose()
