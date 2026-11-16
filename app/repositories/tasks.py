"""PostgreSQL task queue. Session ownership protects live workers beyond TTL."""
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import text

from app.collection_guard import CollectionBlocked
from app.collection_rules import SUPPORTED
from app.task_types import LANES, Outcome, normalized_payload, payload_key

MAX_ACTIVE_PER_LANE = 1000

class TaskLeaseLost(CollectionBlocked):
    pass


def lock_key(value):
    return int.from_bytes(hashlib.sha256(('pq.task:' + value).encode()).digest()[:8], 'big', signed=True)


def enqueue(connection, kind, payload, request_id, *, max_failures=3):
    payload, lane, priority = normalized_payload(kind, payload)
    if type(max_failures) is not int or not 1 <= max_failures <= 10:
        raise ValueError('Max failures must be 1..10')
    source_id = SUPPORTED[payload['source']][0]
    key = payload_key(kind, payload, request_id)
    # Serialize bounded admission, not execution. Duplicate submissions continue
    # to return the existing task even when a lane is full.
    connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key('admission:' + lane)})
    known = connection.execute(text('SELECT task_id,status FROM collection_tasks WHERE source_id=:source AND kind=:kind '
        'AND (idempotency_key=:key OR (:kind=\'collect_source\' AND status IN (\'queued\',\'running\',\'retry\'))) '
        'ORDER BY CASE WHEN idempotency_key=:key THEN 0 ELSE 1 END LIMIT 1'),
        {'source': source_id, 'kind': kind, 'key': key}).mappings().one_or_none()
    if known is not None:
        return {'task_id': str(known['task_id']), 'status': known['status'], 'created': False, 'lane': lane}
    active = connection.execute(text("SELECT count(*) FROM collection_tasks WHERE lane=:lane AND status IN ('queued','running','retry')"),
                                {'lane': lane}).scalar_one()
    if active >= MAX_ACTIVE_PER_LANE:
        raise ValueError('Queue lane is full; wait for work to drain')
    identity = uuid4()
    row = connection.execute(text('INSERT INTO collection_tasks '
        '(task_id, source_id, kind, lane, idempotency_key, payload, priority, max_failures) '
        'VALUES (:id, :source, :kind, :lane, :key, CAST(:payload AS jsonb), :priority, :max) '
        'ON CONFLICT DO NOTHING RETURNING task_id, status'),
        {'id': identity, 'source': source_id, 'kind': kind, 'lane': lane, 'key': key,
         'payload': json.dumps(payload, ensure_ascii=False, allow_nan=False), 'priority': priority, 'max': max_failures}).mappings().one_or_none()
    if row is not None:
        return {'task_id': str(row['task_id']), 'status': row['status'], 'created': True, 'lane': lane}
    existing = connection.execute(text('SELECT task_id, status FROM collection_tasks WHERE source_id=:source '
        'AND kind=:kind AND idempotency_key=:key'), {'source': source_id, 'kind': kind, 'key': key}).mappings().one_or_none()
    if existing is None and kind == 'collect_source':
        existing = connection.execute(text("SELECT task_id, status FROM collection_tasks WHERE source_id=:source "
            "AND kind='collect_source' AND status IN ('queued','running','retry')"), {'source': source_id}).mappings().one_or_none()
    if existing is None:
        # A competing transaction may have completed between the conflict and
        # lookup. Let the caller retry the transaction, never fabricate a job ID.
        raise ValueError('Concurrent queue state changed; retry submission')
    return {'task_id': str(existing['task_id']), 'status': existing['status'], 'created': False, 'lane': lane}


def safe_result(value):
    result = {}
    if value.get('run_id'):
        result['run_id'] = str(UUID(str(value['run_id'])))
    if isinstance(value.get('summary'), dict):
        result['summary'] = {k: v for k, v in value['summary'].items() if k in (
            'attempted', 'inserted', 'already_exists', 'needs_review', 'skipped_unsupported', 'failed', 'interrupted', 'not_attempted')
            and type(v) is int and v >= 0}
    return result


class TaskLease:
    def __init__(self, connection, task, ttl):
        self.connection, self.task, self.ttl = connection, task, ttl

    def _assert(self):
        conn = self.connection
        if conn.closed or conn.invalidated:
            raise TaskLeaseLost('Task session lost')
        key = lock_key(str(self.task['task_id']))
        owns = conn.execute(text("SELECT status='running' AND lease_token=:token AND EXISTS(SELECT 1 FROM pg_locks "
            "WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted "
            'AND classid=CAST(:hi AS oid) AND objid=CAST(:lo AS oid) AND objsubid=1) '
            'FROM collection_tasks WHERE task_id=:id'), {'id': self.task['task_id'], 'token': self.task['lease_token'],
            'hi': (key >> 32) & 0xffffffff, 'lo': key & 0xffffffff}).scalar_one_or_none()
        if not owns:
            raise TaskLeaseLost('Task ownership changed')

    def checkpoint(self):
        # Never reconnect an invalidated session and continue work.
        if self.connection.closed or self.connection.invalidated:
            raise TaskLeaseLost('Task session lost')
        with self.connection.begin():
            self._assert()
            self.connection.execute(text('UPDATE collection_tasks SET lease_expires_at=clock_timestamp()+make_interval(secs=>:ttl), '
                'updated_at=clock_timestamp() WHERE task_id=:id'), {'id': self.task['task_id'], 'ttl': self.ttl})

    def finish(self, outcome: Outcome):
        if outcome.disposition not in ('succeeded', 'deferred', 'retry', 'needs_review', 'cancelled'):
            raise ValueError('Invalid disposition')
        if not outcome.code.replace('_', '').isalnum() or len(outcome.code) > 80:
            raise ValueError('Only a short diagnostic code may be stored')
        result = safe_result(outcome.result)
        if outcome.not_before is not None and outcome.not_before.utcoffset() is None:
            raise ValueError('Retry time requires timezone')
        if self.connection.closed or self.connection.invalidated:
            raise TaskLeaseLost('Task session lost')
        with self.connection.begin():
            self._assert()
            row = self.connection.execute(text('SELECT failure_count,max_failures FROM collection_tasks WHERE task_id=:id FOR UPDATE'),
                                          {'id': self.task['task_id']}).mappings().one()
            failures = row['failure_count'] + (outcome.disposition == 'retry')
            status = 'retry' if outcome.disposition == 'deferred' else outcome.disposition
            if status == 'retry' and outcome.disposition != 'deferred' and failures >= row['max_failures']:
                status = 'dead'
            wait = max(5, min(21600, outcome.delay_seconds))
            if outcome.disposition == 'retry':
                wait = max(wait, min(21600, 60 * 2 ** min(failures-1, 8)))
            self.connection.execute(text('UPDATE collection_tasks SET status=:status, failure_count=:failures, '
                'available_at=GREATEST(clock_timestamp()+make_interval(secs=>:wait), COALESCE(CAST(:deadline AS timestamptz),clock_timestamp())), '
                'lease_token=NULL,lease_expires_at=NULL,worker_id=NULL,last_code=:code,result=CAST(:result AS jsonb), '
                'updated_at=clock_timestamp() WHERE task_id=:id'),
                {'id': self.task['task_id'], 'status': status, 'failures': failures, 'wait': wait, 'deadline': outcome.not_before,
                 'code': outcome.code, 'result': json.dumps(result, ensure_ascii=False)})
            self.connection.execute(text('UPDATE task_attempts SET status=:status, code=:code,finished_at=clock_timestamp(), '
                'run_id=CAST(:run AS uuid) WHERE task_id=:id AND lease_token=:token AND status=\'running\''),
                {'id': self.task['task_id'], 'token': self.task['lease_token'], 'status': 'deferred' if outcome.disposition == 'deferred' else status,
                 'code': outcome.code, 'run': result.get('run_id')})
        return {'task_id': str(self.task['task_id']), 'status': status, 'code': outcome.code, 'failure_count': failures}

    def close(self):
        # Ensure session locks cannot survive even if a caller supplied a pooled engine.
        self.connection.invalidate()
        self.connection.close()


def claim(engine, lanes=('raw',), *, worker_id=None, ttl=120):
    if not lanes or any(lane not in LANES for lane in lanes) or type(ttl) is not int or not 30 <= ttl <= 3600:
        raise ValueError('Invalid lanes or lease TTL')
    worker_id = worker_id or uuid4()
    conn = engine.connect()
    leased = False
    lease = None
    try:
        with conn.begin():
            rows = conn.execute(text("SELECT * FROM collection_tasks WHERE lane=ANY(CAST(:lanes AS text[])) AND "
                "((status IN ('queued','retry') AND available_at<=clock_timestamp()) "
                "OR (status='running' AND lease_expires_at<=clock_timestamp())) "
                'ORDER BY priority DESC,created_at,task_id LIMIT 100 FOR UPDATE SKIP LOCKED'), {'lanes': list(lanes)}).mappings().all()
            for row in rows:
                key = lock_key(str(row['task_id']))
                if not conn.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}).scalar_one():
                    continue
                # Raw and history network jobs for the same source share a resource.
                resource = lock_key('source:' + str(row['source_id'])) if row['lane'] in ('raw', 'history') else key
                if resource != key and not conn.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': resource}).scalar_one():
                    conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
                    continue
                failures = row['failure_count']
                if row['status'] == 'running':
                    failures += 1
                    conn.execute(text("UPDATE task_attempts SET status='abandoned',code='worker_session_lost',finished_at=clock_timestamp() "
                        "WHERE task_id=:id AND status='running'"), {'id': row['task_id']})
                    terminal = 'needs_review' if row['lane'] in ('media', 'analysis') else ('dead' if failures >= row['max_failures'] else None)
                    if terminal:
                        conn.execute(text('UPDATE collection_tasks SET status=:status, failure_count=:failures,lease_token=NULL, '
                            "lease_expires_at=NULL,worker_id=NULL,last_code='worker_session_lost',updated_at=clock_timestamp() WHERE task_id=:id"),
                            {'id': row['task_id'], 'status': terminal, 'failures': failures})
                        for held in dict.fromkeys((resource, key)):
                            conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': held})
                        continue
                token = uuid4()
                task = dict(row, lease_token=token, worker_id=worker_id, attempt_count=row['attempt_count']+1, failure_count=failures)
                conn.execute(text("UPDATE collection_tasks SET status='running',lease_token=:token,worker_id=:worker, "
                    'lease_expires_at=clock_timestamp()+make_interval(secs=>:ttl),attempt_count=:attempt,failure_count=:failures, '
                    'updated_at=clock_timestamp() WHERE task_id=:id'),
                    {'id': row['task_id'], 'token': token, 'worker': worker_id, 'ttl': ttl,
                     'attempt': task['attempt_count'], 'failures': failures})
                conn.execute(text('INSERT INTO task_attempts (attempt_id,task_id,attempt_number,lease_token,worker_id,status) '
                    "VALUES (:id,:task,:attempt,:token,:worker,'running')"), {'id': uuid4(), 'task': row['task_id'],
                    'attempt': task['attempt_count'], 'token': token, 'worker': worker_id})
                lease = TaskLease(conn, task, ttl)
                break
        leased = lease is not None
        return lease
    finally:
        if not leased:
            # Discard the dedicated session even if commit failed after taking
            # an advisory lock; no lock may escape back into a connection pool.
            conn.invalidate()
            conn.close()


def retry_reviewed(connection, task_id, note):
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
        raise ValueError('Provide a 1..500 character review note')
    # Transaction-scoped lock conflicts with a worker's session lock.
    if not connection.execute(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': lock_key(str(task_id))}).scalar_one():
        raise ValueError('Task worker still owns the session')
    row = connection.execute(text("UPDATE collection_tasks SET status='queued',failure_count=0,last_code='reviewed_retry', "
        "available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE task_id=:id AND status IN ('dead','needs_review') RETURNING task_id"),
        {'id': task_id}).scalar_one_or_none()
    if row is None:
        raise ValueError('Only dead or needs_review tasks may be reviewed and retried')
    connection.execute(text('INSERT INTO task_reviews (task_id,note) VALUES (:id,:note)'), {'id': task_id, 'note': note.strip()})
    return {'task_id': str(task_id), 'status': 'queued'}


FIELDS = 'task_id,source_id,kind,lane,priority,status,available_at,attempt_count,failure_count,max_failures,lease_expires_at,last_code,created_at,updated_at'


def select_tasks(connection, limit=20, cursor=None, status=None, lane=None):
    return [dict(r) for r in connection.execute(text('SELECT ' + FIELDS + ' FROM collection_tasks '
        'WHERE (CAST(:cursor AS uuid) IS NULL OR task_id>:cursor) '
        'AND (CAST(:status AS text) IS NULL OR status=:status) AND (CAST(:lane AS text) IS NULL OR lane=:lane) '
        'ORDER BY task_id LIMIT :limit'), {'cursor': cursor, 'status': status, 'lane': lane, 'limit': limit+1}).mappings()]


def select_task(connection, task_id):
    row = connection.execute(text('SELECT ' + FIELDS + ',payload,result FROM collection_tasks WHERE task_id=:id'),
                             {'id': task_id}).mappings().one_or_none()
    return dict(row) if row is not None else None


def select_attempts(connection, task_id, limit=20, before=None):
    return [dict(r) for r in connection.execute(text('SELECT attempt_id,attempt_number,status,code,started_at,finished_at,run_id '
        'FROM task_attempts WHERE task_id=:id AND (CAST(:before AS integer) IS NULL OR attempt_number<:before) '
        'ORDER BY attempt_number DESC LIMIT :limit'), {'id': task_id, 'before': before, 'limit': limit+1}).mappings()]


def select_stats(connection):
    return [dict(r) for r in connection.execute(text("SELECT lane,status,count(*) AS task_count, "
        "min(created_at) AS oldest_created_at, count(*) FILTER(WHERE status IN ('queued','retry') "
        'AND available_at<=clock_timestamp()) AS ready_count FROM collection_tasks GROUP BY lane,status ORDER BY lane,status')).mappings()]
