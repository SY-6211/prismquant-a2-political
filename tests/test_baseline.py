import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.baseline import RunTiming, ItemTiming, build_baseline, clean_timing, distribution
from app.core.config import Settings
from app.main import create_app


STAMP='2026-11-03T04:00:00Z'


def run_timing():
    return dict(input_kind='rss_network',list_started_at=STAMP,list_finished_at=STAMP,
                list_duration_ms=200,list_succeeded=True)


def item_timing():
    return dict(input_kind='rss_network',list_observed_at=STAMP,detail_started_at=STAMP,
                detail_finished_at=STAMP,detail_duration_ms=400,detail_succeeded=True,
                save_started_at=STAMP,save_returned_at=STAMP,save_duration_ms=10,observed_to_save_ms=1500)


def data():
    return {'sources':[{'source':'ecb','source_name':'ECB','configured_poll_interval_seconds':300}],
            'runs':[], 'items':[], 'truncated':False}


class BaselineTests(unittest.TestCase):
    def test_unknown_is_not_zero_and_config_does_not_prove_realtime(self):
        result=build_baseline(data(),24)
        source=result['sources'][0]
        self.assertEqual(result['target_status'],'not_measured')
        self.assertIsNone(source['public_to_page']['p95_ms'])
        self.assertIsNone(source['detail_fetch_and_parse']['p95_ms'])
        self.assertEqual(source['configured_poll_interval_seconds'],300)

    def test_nearest_rank_and_sample_count(self):
        result=distribution(list(range(1,101)))
        self.assertEqual((result['p50_ms'],result['p95_ms'],result['p99_ms']),(50,95,99))
        self.assertTrue(result['sufficient_for_percentiles'])
        self.assertFalse(distribution([12])['sufficient_for_percentiles'])

    def test_history_provided_items_existing_and_unknown_writes_excluded(self):
        values=data()
        base=dict(source='ecb',mode='automatic_batch_import',status='inserted',write_outcome_unknown=False,
                  timing=item_timing(),published_at='2099-01-01T00:00:00Z')
        values['items']=[base,{**base,'mode':'manual_history_import'},
                         {**base,'timing':{**item_timing(),'input_kind':'provided_items'}},
                         {**base,'status':'already_exists'},{**base,'write_outcome_unknown':True},
                         {**base,'timing':{}}]
        result=build_baseline(values,24)['sources'][0]
        self.assertEqual(result['observed_in_run_to_save_return']['samples'],1)
        self.assertEqual(result['observed_in_run_to_save_return']['p95_ms'],1500)
        self.assertEqual(result['failures']['unknown_write'],1)
        self.assertIsNone(result['public_to_page']['p95_ms'])

    def test_list_and_detail_failures_remain_visible(self):
        values=data()
        values['runs']=[{'source':'ecb','mode':'automatic_batch_import','timing':run_timing()},
                        {'source':'ecb','mode':'automatic_batch_import','timing':{**run_timing(),'list_succeeded':False}}]
        values['items']=[dict(source='ecb',mode='automatic_batch_import',status='failed',
                             timing={**item_timing(),'detail_succeeded':False},write_outcome_unknown=False)]
        result=build_baseline(values,24)['sources'][0]
        self.assertEqual(result['list_fetch_and_parse']['samples'],1)
        self.assertEqual(result['detail_fetch_and_parse']['samples'],0)
        self.assertEqual(result['failures']['list'],1)
        self.assertEqual(result['failures']['detail'],1)

    def test_whitelist_nonfinite_naive_and_invalid_metadata(self):
        value=clean_timing({**run_timing(),'body_text':'DO_NOT_STORE','password':'SECRET'},RunTiming)
        self.assertNotIn('SECRET',json.dumps(value));self.assertNotIn('DO_NOT_STORE',json.dumps(value))
        for change in ({'list_started_at':'2026-11-03T00:00:00'}, {'list_duration_ms':float('nan')},
                       {'list_duration_ms':-1},{'input_kind':'SECRET'}):
            self.assertEqual(clean_timing({**run_timing(),**change},RunTiming),{'measurement_error':'invalid_timing'})

    def test_invalid_automatic_run_and_item_counted_without_manual_pollution(self):
        values=data()
        invalid={'measurement_error':'invalid_timing'}
        values['runs']=[dict(source='ecb',mode=mode,timing=invalid)
                        for mode in ('automatic_batch_import','manual_history_import')]
        values['items']=[dict(source='ecb',mode=mode,status='interrupted',
                             timing=invalid,write_outcome_unknown=True)
                         for mode in ('automatic_batch_import','manual_history_import')]
        result=build_baseline(values,24)['sources'][0]
        self.assertEqual(result['failures']['invalid_timing'],2)
        self.assertEqual(result['failures']['unknown_write'],1)
        self.assertEqual(result['unmeasured_item_rows'],2)
        self.assertEqual(result['automatic_item_rows'],1)
        self.assertEqual(result['observed_in_run_to_save_return']['samples'],0)

    def test_api_validation_empty_and_redaction(self):
        with TestClient(create_app(Settings(_env_file=None))) as client:
            with patch('app.api.baseline.read_baseline',return_value=data()) as read:
                result=client.get('/api/v1/monitoring-baseline?hours=48&source=ecb')
                self.assertEqual(result.status_code,200)
                self.assertEqual(read.call_args.args[1:],(48,'ecb'))
                self.assertEqual(result.json()['target_status'],'not_measured')
                for query in ('hours=0','hours=169','source=other'):
                    self.assertEqual(client.get('/api/v1/monitoring-baseline?'+query).status_code,422)
            with patch('app.api.baseline.read_baseline',side_effect=OperationalError('PRIVATE SQL',{},Exception('PASSWORD'))):
                result=client.get('/api/v1/monitoring-baseline')
                self.assertEqual(result.status_code,503)
                self.assertNotIn('PASSWORD',result.text)
                self.assertNotIn('PRIVATE',result.text)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS')=='1','Explicit timing database checks only')
class BaselineDatabaseTests(unittest.TestCase):
    def test_recorded_timings_roundtrip_and_old_rows_are_not_invented(self):
        from app.collection_runs import engine_for,start_run,finish_run
        from app.repositories.collection_runs import select_run
        from app.api.collection_runs import RunDetail
        engine=engine_for(Settings())
        try:
            with engine.connect() as connection:
                tx=connection.begin()
                try:
                    identity=start_run(connection,'ecb',1,mode='automatic_batch_import')
                    payload=dict(status='completed',selection={'selected':1},
                        summary=dict(attempted=1,inserted=1,already_exists=0,needs_review=0,failed=0,interrupted=0,not_attempted=0),
                        timing={**run_timing(),'body_text':'DO_NOT_STORE'},
                        items=[dict(url='https://example.invalid/timing',status='inserted',timing={**item_timing(),'password':'SECRET'})])
                    finish_run(connection,identity,payload)
                    row=select_run(connection,identity)
                    self.assertNotIn('SECRET',json.dumps(row['items'][0]['timing']))
                    self.assertNotIn('DO_NOT_STORE',json.dumps(row['timing']))
                    api=RunDetail(**row)
                    self.assertEqual(api.timing.list_duration_ms,200)
                    self.assertEqual(api.items[0].timing.observed_to_save_ms,1500)
                    legacy=start_run(connection,'ecb',1)
                    self.assertEqual(select_run(connection,legacy)['timing'],{})
                    bad=start_run(connection,'ecb',1)
                    finish_run(connection,bad,{**payload,'timing':{'list_duration_ms':-1}})
                    self.assertEqual(select_run(connection,bad)['timing'],{'measurement_error':'invalid_timing'})
                finally:tx.rollback()
        finally:engine.dispose()
