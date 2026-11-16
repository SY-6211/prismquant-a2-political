"""One dedicated PostgreSQL session owns a source across requests and commits."""
import hashlib
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.collection_guard import CollectionBlocked
from app.collection_rules import SUPPORTED, evaluate_rule
from app.collection_runs import engine_for, execute_recorded
from app.repositories.collection_rules import select_context


def lock_key(source):
    sid = SUPPORTED[source][0]
    return int.from_bytes(hashlib.sha256(('prismquant.collect:' + str(sid)).encode()).digest()[:8], 'big', signed=True)


class SourceLease:
    def __init__(self, settings, source):
        if source not in SUPPORTED:
            raise ValueError('Unsupported source')
        self.settings, self.source = settings, source
        self.key = lock_key(source)
        self.engine = self.connection = None
        self.acquired = False

    def __enter__(self):
        try:
            self.engine = engine_for(self.settings)
            self.connection = self.engine.connect()
            self.acquired = self.connection.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': self.key}).scalar_one()
            self.pid = self.connection.execute(text('SELECT pg_backend_pid()')).scalar_one()
            self.connection.commit()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self.connection is not None:
                if self.connection.in_transaction():
                    self.connection.rollback()
                if self.acquired and not self.connection.invalidated:
                    self.connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': self.key})
                    self.connection.commit()
        except Exception:
            pass  # Closing the session also releases all its session locks.
        finally:
            if self.connection is not None:
                self.connection.close()
            if self.engine is not None:
                self.engine.dispose()

    def require(self, *, automatic=False, lock_settings=False):
        """Caller owns the transaction. Never reconnect and continue a lost lease."""
        conn = self.connection
        if not self.acquired or conn.closed or conn.invalidated:
            raise CollectionBlocked('Execution ownership lost')
        owns = conn.execute(text(
            "SELECT pg_backend_pid()=:pid AND EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
            'AND pid=pg_backend_pid() AND granted AND classid=CAST(:hi AS oid) AND objid=CAST(:lo AS oid) AND objsubid=1)'
        ), {'pid': self.pid, 'hi': (self.key >> 32) & 0xffffffff, 'lo': self.key & 0xffffffff}).scalar_one()
        if not owns:
            raise CollectionBlocked('Execution ownership lost')
        if automatic:
            if lock_settings:
                sid, pid, _, _ = SUPPORTED[self.source]
                conn.execute(text(
                    'SELECT s.source_id FROM sources s JOIN person_sources ps ON ps.source_id=s.source_id '
                    'JOIN people p ON p.person_id=ps.person_id WHERE s.source_id=:sid AND p.person_id=:pid '
                    'FOR SHARE OF s,ps,p'
                ), {'sid': sid, 'pid': pid}).all()
            if not evaluate_rule(self.source, select_context(conn, self.source)).eligible:
                raise CollectionBlocked('Automatic permission changed')

    def before_request(self, automatic):
        with self.connection.begin():
            self.require(automatic=automatic)
        if self.source == 'ecb':
            from app.ecb_http_pacing import pace
            pace(self.connection, lambda: self.require(automatic=automatic))

    def save(self, automatic, settings, *payload, discovery_transport='rss', observation_metadata=None):
        # The same session holds the source lock and commits the content. If it
        # disconnects mid-request, it cannot open a fresh connection and write.
        with self.connection.begin():
            self.require(automatic=automatic, lock_settings=True)
            if self.source == 'ecb':
                from app.import_ecb_speech import save_speech
                if discovery_transport == 'rss' and observation_metadata is None:
                    return save_speech(self.connection, *payload, datetime.now(timezone.utc))
                return save_speech(self.connection, *payload, datetime.now(timezone.utc),
                    discovery_transport=discovery_transport, observation_metadata=observation_metadata)
            from app.import_trump_post import save_post
            return save_post(self.connection, *payload, datetime.now(timezone.utc))


def execute_under_lease(lease, limit, client, runner, *, automatic=False, checkpoint=None, mode=None):
    def before_request():
        if checkpoint is not None:
            checkpoint()
        lease.before_request(automatic)

    def save(settings, *payload):
        if checkpoint is not None:
            checkpoint()
        return lease.save(automatic, settings, *payload)

    def guarded(settings, count, http):
        return runner(settings, count, http,
                      before_request=before_request,
                      save=save)
    return execute_recorded(lease.settings, lease.source, limit, client, guarded,
                            mode=mode or ('automatic_batch_import' if automatic else 'manual_batch_import'))


def execute_exclusive(settings, source, limit, client, runner):
    try:
        with SourceLease(settings, source) as lease:
            if not lease.acquired:
                return {'status': 'skipped_busy', 'source': source, 'saved': False,
                        'stop_reason': '该来源已有批量任务执行，本次跳过；未请求来源。'}
            return execute_under_lease(lease, limit, client, runner)
    except (SQLAlchemyError, ValueError):
        return {'status': 'stopped', 'source': source, 'saved': None,
                'stop_reason': '来源执行锁或数据库检查失败，已停止；如已开始写入，结果需按地址复核。'}
