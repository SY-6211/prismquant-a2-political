"""All writes, including concurrent transactions, stay in a disposable schema."""
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import text

import test_task_queue_db as fixtures
from test_event_model import record
from app.collection_rules import SUPPORTED
from app.event_model import snapshot_from_record
from app.repositories.events import save_snapshot
from app.repositories.content_stream import read_batch, select_batch


@unittest.skipUnless(os.environ.get('PQ_STREAM_DB_TESTS')=='1','Isolated-schema stream checks are opt-in')
class ContentStreamDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.TaskDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.TaskDatabaseTests.drop_schema.__func__)

    def setUp(self):
        fixtures.TaskDatabaseTests.setUp(self)
        self.source,self.person,*_=SUPPORTED['trump']
        self.now=datetime.now(timezone.utc)
        self.snapshot=snapshot_from_record(record(person_id=self.person,original_url=f'https://truthsocial.com/@test/{uuid4().int}'))

    def save(self,conn,snapshot=None,**kwargs):
        return save_snapshot(conn,self.source,'rss',snapshot or self.snapshot,self.now,**kwargs)

    def batch(self,after=None):
        with self.engine.connect().execution_options(isolation_level='REPEATABLE READ') as conn:
            return select_batch(conn,after)

    def test_committed_only_rollback_and_same_transaction_as_snapshot(self):
        head=self.batch()['head']
        with self.engine.connect() as conn:
            tx=conn.begin();self.save(conn)
            self.assertEqual(self.batch()['head'],head)
            tx.rollback()
        self.assertEqual(self.batch()['head'],head)
        with self.engine.begin() as conn:self.save(conn)
        batch=self.batch();self.assertEqual(batch['head'],head+1)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT count(*) FROM content_changes')).scalar_one(),1)

    def test_duplicate_and_cross_channel_same_snapshot_silent_but_a_b_a_announced(self):
        with self.engine.begin() as conn:
            self.save(conn);self.save(conn)
            save_snapshot(conn,self.source,'web_index',self.snapshot,self.now+timedelta(seconds=1))
        state=self.batch();epoch,head=state['epoch'],state['head']
        with self.engine.begin() as conn:
            changed=self.snapshot.model_copy(update={'main_text':'Revised','review_status':'needs_review'})
            save_snapshot(conn,self.source,'rss',changed,self.now+timedelta(seconds=2))
            save_snapshot(conn,self.source,'rss',self.snapshot,self.now+timedelta(seconds=3))
        items=self.batch((epoch,head))['items']
        self.assertEqual([item['kind'] for item in items],['revision','revision'])
        self.assertEqual(items[0]['review_status'],'needs_review')
        self.assertEqual(self.batch()['head'],head+2)

    def test_legacy_projection_does_not_broadcast_historical_database(self):
        before=self.batch()['head']
        with self.engine.begin() as conn:self.save(conn,observation_kind='legacy_projection')
        self.assertEqual(self.batch()['head'],before)

    def test_bounded_retention_and_invalid_resume_never_fabricates_replay(self):
        with patch('app.repositories.content_stream.RETAIN',2):
            for number in range(4):
                with self.engine.begin() as conn:self.save(conn,self.snapshot.model_copy(update={'main_text':str(number)}))
        state=self.batch();self.assertEqual(state['floor'],state['head']-2)
        self.assertEqual(len(self.batch((state['epoch'],state['floor']))['items']),2)
        self.assertEqual(self.batch((state['epoch'],state['floor']-1))['items'],[])
        self.assertEqual(self.batch(('b'*32,state['head']))['items'],[])

    def test_reader_uses_readonly_repeatable_snapshot_and_releases_connection(self):
        with patch('app.core.database.create_read_engine',return_value=self.engine), patch.object(self.engine,'dispose') as dispose:
            value=read_batch(None)
            self.assertEqual(value['head'],self.batch()['head']);dispose.assert_called_once()

    def test_commit_order_blocks_higher_cursor_until_lower_transaction_finishes(self):
        initial=self.batch();locked=threading.Event();attempting=threading.Event();release=threading.Event()
        other=self.snapshot.model_copy(update={'original_url':f'https://truthsocial.com/@test/{uuid4().int}'})
        def first():
            with self.engine.begin() as conn:
                a=self.save(conn);locked.set();self.assertTrue(release.wait(8));return a
        def second():
            self.assertTrue(locked.wait(8))
            with self.engine.begin() as conn:
                attempting.set();return self.save(conn,other)
        with ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(first);two=pool.submit(second)
            try:
                self.assertTrue(attempting.wait(8));time.sleep(.15)
                self.assertFalse(two.done());self.assertEqual(self.batch()['head'],initial['head'])
            finally:release.set()
            a,b=one.result(8),two.result(8)
        items=self.batch((initial['epoch'],initial['head']))['items']
        self.assertEqual([str(i['event_id']) for i in items],[a['event_id'],b['event_id']])
        self.assertEqual([i['position'] for i in items],[initial['head']+1,initial['head']+2])

    def test_rolled_back_first_writer_does_not_leave_a_gap_for_second(self):
        start=self.batch()
        with self.engine.connect() as conn:
            tx=conn.begin();self.save(conn);tx.rollback()
        with self.engine.begin() as conn:self.save(conn)
        self.assertEqual([row['position'] for row in self.batch((start['epoch'],start['head']))['items']],[start['head']+1])
