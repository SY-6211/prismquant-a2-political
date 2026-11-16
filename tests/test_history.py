import base64
import json
import tempfile
import unittest
import os
from uuid import uuid4
from unittest.mock import MagicMock, patch
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from app.backfill import validate_plan, write_plan
from app.history_sources import (cursor_boundary, discover_ecb, discover_trump, in_range,
                                 parse_ecb_chunk, read_bytes, validate_trump_index)
from app.extract_trump_post import prepare_extraction
from app.import_trump_post import ArchiveDraft, RecordDraft
from app.preview_trump_post import parse_post
from test_preview_trump_post import candidate, page, status


def next_page(stamp):
    cursor = base64.b64encode(json.dumps({'status_created_at': stamp, '_pointsToNextItems': True}).encode()).decode()
    return 'https://www.trumpstruth.org/?sort=desc&per_page=100&cursor=' + cursor


class HistoryTests(unittest.TestCase):
    def test_allowed_pagination_rejects_other_hosts_queries_and_schemes(self):
        for url in ('http://www.trumpstruth.org/', 'https://evil.example/?cursor=x',
                    'https://www.trumpstruth.org/statuses/1', 'https://www.trumpstruth.org/?sort=asc',
                    'https://www.trumpstruth.org/?per_page=999999', 'https://www.trumpstruth.org/?redirect=x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_trump_index(url)
        self.assertEqual(cursor_boundary(next_page('2026-09-19 00:00:00')).date(), date(2026,9,19))

    def test_range_is_inclusive_and_rejects_naive_unknown_time(self):
        self.assertTrue(in_range('2026-09-20T00:00:00Z', date(2026,9,20), date(2026,10,19)))
        self.assertTrue(in_range('2026-10-19T23:59:59Z', date(2026,9,20), date(2026,10,19)))
        self.assertFalse(in_range('2026-10-20T00:00:00Z', date(2026,9,20), date(2026,10,19)))
        self.assertFalse(in_range('2026-10-01', date(2026,9,20), date(2026,10,19)))

    def test_ecb_flat_index_filters_speaker_and_url(self):
        header = ['pub_timestamp','boardmember','documentTypes','publicationProperties']
        stamp = int(datetime(2026,10,1,tzinfo=timezone.utc).timestamp())
        rows = [stamp,'Christine Lagarde',['/press/key/date/2026/html/ecb.sp261001~abc.en.html'],{'Title':'Speech'},
                stamp,'Other Speaker',['/press/key/date/2026/html/ecb.sp261001~def.en.html'],{'Title':'Other'}]
        items, times = parse_ecb_chunk(json.dumps(rows), header)
        self.assertEqual(len(items),1); self.assertEqual(len(times),2)
        self.assertEqual(items[0]['metadata_origin'],'ecb_history_index')
        self.assertTrue(items[0]['title'].startswith('Christine Lagarde:'))
        with self.assertRaises(ValueError): parse_ecb_chunk('[1]',header)

    def test_ecb_version_is_dynamic_and_coverage_is_bounded(self):
        header = ['pub_timestamp','boardmember','documentTypes','publicationProperties']
        def handler(request):
            path = request.url.path
            if path.endswith('versions.json'): return httpx.Response(200,json=[{'version':'123','hash':'abc'}])
            if path.endswith('metadata.json'): return httpx.Response(200,json={'header':header,'chunk_size':1,'chunk_group_size':10,'total_records':2,'sort':['!pub_timestamp']})
            return httpx.Response(200,json=[1790812800,'Christine Lagarde',['/press/key/date/2026/html/ecb.sp261001~abc.en.html'],{'Title':'Speech'}])
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = discover_ecb(client,date(2026,9,20),date(2026,10,19),1,pause=lambda _:None)
        self.assertFalse(result['coverage_complete']); self.assertIn('/123/abc/',result['next_url'])

    def test_redirect_is_not_followed(self):
        requests=[]
        def handler(request):
            requests.append(str(request.url)); return httpx.Response(302,headers={'Location':'https://evil.example'})
        with httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=False) as client:
            with self.assertRaises(httpx.HTTPStatusError): read_bytes(client,'https://www.trumpstruth.org/',lambda _:None)
        self.assertEqual(len(requests),1)

    def test_ecb_crosses_chunk_groups_using_record_offsets(self):
        header=['pub_timestamp','boardmember','documentTypes','publicationProperties']
        requested=[]
        def handler(request):
            path=request.url.path;requested.append(path)
            if path.endswith('versions.json'):return httpx.Response(200,json=[{'version':'123','hash':'abc'}])
            if path.endswith('metadata.json'):return httpx.Response(200,json={'header':header,'chunk_size':2,'chunk_group_size':4,'total_records':8,'sort':['!pub_timestamp']})
            chunk=int(path.rsplit('chunk_',1)[1].split('.')[0])
            rows=[]
            for offset in range(2):
                rows.extend([int(datetime(2026,10,10-2*chunk-offset,tzinfo=timezone.utc).timestamp()),'Other Speaker',[],{}])
            return httpx.Response(200,json=rows)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result=discover_ecb(client,date(2026,9,20),date(2026,10,19),3,pause=lambda _:None)
        self.assertTrue(any('/data/1/chunk_2.json' in path for path in requested))
        self.assertFalse(result['coverage_complete'])

    def test_reposted_old_header_does_not_end_history_scan(self):
        # The oldest displayed original is outside the range, but the index
        # cursor still points inside it. Discovery must fetch the next page.
        from unittest.mock import patch
        with patch('app.history_sources.read_bytes',return_value=b'x') as read, patch('app.history_sources.parse_trump_index',side_effect=[
            ([],['2025-01-01T00:00:00Z'],next_page('2026-10-01 00:00:00')),
            ([],['2026-09-19T00:00:00Z'],next_page('2026-09-19 00:00:00')),
        ]):
            result=discover_trump(None,date(2026,9,20),date(2026,10,19),5,pause=lambda _:None)
        self.assertEqual(read.call_count,2); self.assertTrue(result['coverage_complete'])

    def test_archive_does_not_claim_coverage_at_page_limit(self):
        from unittest.mock import patch
        with patch('app.history_sources.read_bytes',return_value=b'x'), patch('app.history_sources.parse_trump_index',return_value=([],[],next_page('2026-10-01 00:00:00'))):
            result=discover_trump(None,date(2026,9,20),date(2026,10,19),1)
        self.assertFalse(result['coverage_complete']); self.assertIsNotNone(result['next_url'])

    def test_non_advancing_cursor_stops(self):
        from unittest.mock import patch
        url=next_page('2026-10-01 00:00:00')
        with patch('app.history_sources.read_bytes',return_value=b'x'), patch('app.history_sources.parse_trump_index',return_value=([],[],url)):
            with self.assertRaises(ValueError): discover_trump(None,date(2026,9,20),date(2026,10,19),2,start_url=url)

    def test_listing_origin_is_preserved_in_archive_and_evidence(self):
        item = {**candidate(published_raw='2026-10-17T04:00:00Z'), 'metadata_origin':'archive_listing'}
        payload = prepare_extraction(parse_post(page(status('Synthetic history text.')),item,include_full=True),item)
        self.assertTrue(payload['ingestion_eligible'])
        record=RecordDraft.model_validate(payload['record'])
        archive=ArchiveDraft.model_validate(payload['archive_post'])
        self.assertEqual(archive.title_origin,'archive_listing')
        self.assertIn('历史列表',record.speaker_evidence)
        self.assertNotIn('RSS',record.speaker_evidence)

    def test_checkpoint_atomic_write_and_scope_validation(self):
        plan={'version':1,'source':'ecb','since':'2026-09-20','until':'2026-10-19','candidates':[],'outcomes':{}}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'plan.json';write_plan(path,plan)
            self.assertEqual(validate_plan(json.loads(path.read_text('utf-8'))),plan)
            self.assertFalse(path.with_suffix('.json.tmp').exists())
        plan['candidates']=[{'metadata_origin':'archive_listing','published_at':'2026-10-01T00:00:00Z'}]
        with self.assertRaises(ValueError):validate_plan(plan)

    def test_busy_source_does_not_start_a_history_run(self):
        from app.backfill import import_plan
        lease=MagicMock(); lease.acquired=False
        with patch('app.backfill.SourceLease') as factory, patch('app.backfill.execute_recorded') as run:
            factory.return_value.__enter__.return_value=lease
            result=import_plan(None,None,{'source':'ecb'},Path('unused.json'),5)
        self.assertEqual(result['status'],'skipped_busy');run.assert_not_called()

    def test_logging_failure_checkpoints_and_never_runs_next_batch(self):
        from app.backfill import import_plan
        lease=MagicMock();lease.acquired=True
        lease.connection.execute.return_value.scalar_one.return_value=0
        lease.connection.execute.return_value.mappings.return_value.one_or_none.return_value=None
        pending=[{'url':f'https://example.invalid/{n}'} for n in range(8)]
        report={'status':'stopped','run_record_status':'finalization_failed','run_id':str(uuid4()),
                'summary':{'failed':1},'items':[{'url':pending[0]['url'],'status':'failed','write_outcome_unknown':True}]}
        plan={'source':'ecb','outcomes':{},'runs':[]}
        with tempfile.TemporaryDirectory() as directory, patch('app.backfill.SourceLease') as factory, patch('app.backfill.summarize',return_value=({},pending)), patch('app.backfill.execute_recorded',return_value=report) as run:
            factory.return_value.__enter__.return_value=lease
            path=Path(directory)/'plan.json'
            result=import_plan(None,None,plan,path,8,notify=lambda _:None)
            self.assertTrue(json.loads(path.read_text('utf-8'))['outcomes'][pending[0]['url']]['write_outcome_unknown'])
        self.assertEqual(result['status'],'stopped');self.assertEqual(run.call_count,1)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1','Explicit PostgreSQL history checks')
class HistoryDatabaseTests(unittest.TestCase):
    def test_history_run_and_listing_snapshot_roundtrip_without_committing(self):
        from sqlalchemy import text
        from app.collection_runs import engine_for, start_run, finish_run
        from app.core.config import Settings
        from app.import_trump_post import save_post
        from app.repositories.public_records import select_public_record
        from app.api.public_records import PublicRecordDetail
        from test_import_trump_post import extraction, add_fixture, SEEN
        source,person=uuid4(),uuid4()
        engine=engine_for(Settings())
        try:
            with engine.connect() as connection:
                tx=connection.begin()
                try:
                    add_fixture(connection,source,person)
                    payload=extraction(source,person)
                    payload['archive_post']['title_origin']='archive_listing'
                    result=save_post(connection,payload,SEEN,source_id=source,person_id=person)
                    detail=PublicRecordDetail(**select_public_record(connection,result['record_id']))
                    self.assertEqual(detail.archive_post.title_origin,'archive_listing')
                    again=save_post(connection,payload,SEEN,source_id=source,person_id=person)
                    self.assertEqual(again['action'],'already_exists')
                    identity=start_run(connection,'trump',1,mode='manual_history_import')
                    finish_run(connection,identity,{'status':'completed','selection':{'selected':1},
                        'summary':dict(attempted=1,inserted=1,already_exists=0,needs_review=0,failed=0,interrupted=0,not_attempted=0),
                        'items':[{'url':payload['record']['retrieved_url'],'status':'inserted','record_id':result['record_id']}]})
                    self.assertEqual(connection.execute(text('SELECT mode FROM collection_runs WHERE run_id=:id'),{'id':identity}).scalar_one(),'manual_history_import')
                finally:tx.rollback()
        finally:engine.dispose()
