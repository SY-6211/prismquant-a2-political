import os,unittest
from datetime import datetime,timezone,timedelta
from uuid import uuid4
from unittest.mock import patch,MagicMock
from sqlalchemy import text
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.main import create_app
from app.repositories import monitor_speed as repo
from app.monitor_speed import Dispatcher,html_round
from app.web_http import FetchError
import test_agent_tools_db as fixtures
from test_web_pilot import document
from app.repositories import web_pilot

class SpeedContracts(unittest.TestCase):
 def test_disabled_paused_and_waiting_never_are_due(self):
  base=dict(enabled=True,pause_reason=None,next_attempt_at=None,now=datetime.now(timezone.utc))
  self.assertEqual(repo.due(base),'due')
  self.assertEqual(repo.due({**base,'enabled':False}),'disabled')
  self.assertEqual(repo.due({**base,'pause_reason':'access'}),'paused')
  self.assertEqual(repo.due({**base,'next_attempt_at':base['now']+timedelta(seconds=60)}),'not_due')
 def test_dispatcher_independent_sources_and_retains_one_future_each(self):
  with patch('app.monitor_speed.ThreadPoolExecutor') as pool:
   pool.return_value.submit.return_value.done.return_value=False
   worker=Dispatcher(Settings(_env_file=None));worker.tick();worker.tick()
   self.assertEqual(pool.return_value.submit.call_count,5);worker.close()
   pool.return_value.shutdown.assert_called_once_with(wait=True,cancel_futures=True)
 def test_scheduler_selects_new_runtime_only_with_flag(self):
  from app.scheduler import tick
  with patch('app.monitor_speed.run_source',return_value={'status':'disabled'}) as run:
   result=tick(Settings(_env_file=None,fast_discovery_enabled=True),['ecb','trump'])
   self.assertEqual(len(result['items']),5);self.assertEqual(run.call_count,5)
 def test_receipt_validation_and_cross_origin_reject_without_db(self):
  with TestClient(create_app(Settings(_env_file=None))) as client,patch('app.api.monitor_speed.execute') as execute:
   for ms in (-1,60001,'NaN'):
    self.assertEqual(client.post('/api/v1/monitor-speed/receipts',json={'session_id':str(uuid4()),'version_id':str(uuid4()),'notice_render_ms':ms}).status_code,422)
   body={'session_id':str(uuid4()),'version_id':str(uuid4()),'notice_render_ms':2}
   self.assertEqual(client.post('/api/v1/monitor-speed/receipts',headers={'Origin':'https://other.example'},json=body).status_code,403)
   execute.assert_not_called()
 def test_status_reads_do_not_start_worker_or_fetch(self):
  with TestClient(create_app(Settings(_env_file=None))) as client,patch('app.api.monitor_speed.execute',return_value={'items':[]}),patch('app.monitor_speed.Dispatcher') as dispatcher:
   result=client.get('/api/v1/monitor-speed');self.assertEqual(result.status_code,200);self.assertFalse(result.json()['runtime_configured']);dispatcher.assert_not_called()
 def test_existing_worker_dispatches_new_mode_and_closes_threads(self):
  import tempfile
  from pathlib import Path
  from app.scheduler_service import worker,write_json
  with tempfile.TemporaryDirectory() as folder:
   runtime=Path(folder)
   def tick():write_json(runtime/'stop.json',{'worker_id':'synthetic'});return {'items':[]}
   with patch('app.scheduler_service.Settings',return_value=Settings(_env_file=None,fast_discovery_enabled=True)),patch('app.monitor_speed.Dispatcher') as cls:
    cls.return_value.tick.side_effect=tick;self.assertEqual(worker('synthetic',runtime),0);cls.return_value.close.assert_called_once()

@unittest.skipUnless(os.environ.get('PQ_SPEED_DB_TESTS')=='1','Isolated discovery database checks are opt-in')
class SpeedDatabase(unittest.TestCase):
 setUpClass=classmethod(fixtures.AgentToolDatabaseTests.setUpClass.__func__)
 drop_schema=classmethod(fixtures.AgentToolDatabaseTests.drop_schema.__func__)
 def setUp(self):
  self.c=self.engine.connect();self.tx=self.c.begin();self.addCleanup(self.c.close);self.addCleanup(lambda:self.tx.rollback() if self.tx.is_active else None)
 def test_controls_default_disabled_and_enable_does_not_clear_pause(self):
  self.assertTrue(all(not r['enabled'] for r in repo.overview(self.c)['items']))
  repo.control(self.c,'fed',True);self.c.execute(text("UPDATE speed_sources SET pause_reason='denied' WHERE source='fed'"))
  repo.control(self.c,'fed',True);self.assertEqual(repo.state(self.c,'fed')['pause_reason'],'denied')
  repo.control(self.c,'fed',True,resume_note='Reviewed access evidence');self.assertIsNone(repo.state(self.c,'fed')['pause_reason'])
 def test_first_snapshot_is_baseline_then_new_url_is_pending(self):
  one=[{'url':'https://example.invalid/old','title':'Old speech'}]
  first=repo.frontier(self.c,'fed',one);self.assertTrue(first['baseline']);self.assertFalse(repo.pending(self.c,'fed'))
  again=repo.frontier(self.c,'fed',[{'url':'https://example.invalid/new','title':'New speech'},*one]);self.assertFalse(again['baseline']);self.assertFalse(again['coverage_gap']);self.assertEqual(len(repo.pending(self.c,'fed')),1)
 def test_frontier_without_overlap_is_not_treated_as_verified_live_coverage(self):
  repo.frontier(self.c,'fed',[{'url':'https://example.invalid/a','title':'First'}]);second=repo.frontier(self.c,'fed',[{'url':'https://example.invalid/b','title':'Second'}])
  self.assertTrue(second['coverage_gap']);self.assertFalse(repo.pending(self.c,'fed'))
 def test_round_failure_backs_off_and_three_failures_pause(self):
  repo.control(self.c,'fed',True);start=repo.begin(self.c,'fed')['started_at']
  for _ in range(3):repo.finish(self.c,'fed',start,{'status':'failed'})
  row=repo.state(self.c,'fed');self.assertEqual(row['failures'],3);self.assertIsNotNone(row['pause_reason']);self.assertGreater(row['next_attempt_at'],row['now'])
 def test_retry_after_is_not_shortened(self):
  repo.control(self.c,'fed',True);start=repo.begin(self.c,'fed')['started_at'];repo.finish(self.c,'fed',start,{'status':'failed','retry_seconds':3600})
  row=repo.state(self.c,'fed');self.assertGreater((row['next_attempt_at']-row['now']).total_seconds(),3500)
 def test_enable_and_begin_persist_due_across_rounds(self):
  repo.control(self.c,'eia',True);self.assertEqual(repo.begin(self.c,'eia')['status'],'started');self.assertEqual(repo.begin(self.c,'eia')['status'],'not_due')
 def test_adapter_due_clock_does_not_add_a_second_outer_interval(self):
  repo.control(self.c,'ecb',True);start=repo.begin(self.c,'ecb')['started_at'];next_at=start+timedelta(seconds=12)
  repo.finish(self.c,'ecb',start,{'status':'not_due','next_attempt_at':next_at})
  self.assertEqual(repo.state(self.c,'ecb')['next_attempt_at'],next_at)
 def new_delivery(self,catchup=False):
  doc=document();saved=web_pilot.save_document(self.c,'fed',doc,datetime.now(timezone.utc));result=web_pilot.import_person(self.c,saved['document_id'])
  repo.delivery(self.c,'fed',result['version_id'],datetime.now(timezone.utc),13,catchup=catchup);return result['version_id']
 def test_receipt_has_no_publication_claim_and_deduplicates(self):
  vid=self.new_delivery();session=uuid4();value=repo.receipt(self.c,session,vid,2)
  self.assertTrue(value['recorded']);self.assertFalse(value['publication_to_display_verified']);self.assertEqual(value['evidence_kind'],'client_reported_notice')
  self.assertFalse(repo.receipt(self.c,session,vid,3)['recorded'])
  summary=next(r for r in repo.overview(self.c)['items'] if r['source']=='fed');self.assertEqual(summary['browser_notices'],1);self.assertIsNone(summary['publication_to_display_ms'])
 def test_catchup_unknown_and_expired_receipts_are_excluded(self):
  self.assertFalse(repo.receipt(self.c,uuid4(),uuid4(),0)['recorded'])
  vid=self.new_delivery(catchup=True);self.assertFalse(repo.receipt(self.c,uuid4(),vid,2)['recorded'])
  self.c.execute(text("UPDATE speed_deliveries SET is_catchup=false,commit_returned_at=clock_timestamp()-interval '20 minutes' WHERE version_id=:v"),{'v':vid})
  self.assertFalse(repo.receipt(self.c,uuid4(),vid,2)['recorded'])
 def test_disabled_person_is_not_auto_imported(self):
  from app.monitor_speed import allowed_person
  doc=document();saved=web_pilot.save_document(self.c,'fed',doc,datetime.now(timezone.utc));web_pilot.import_person(self.c,saved['document_id'])
  self.assertFalse(allowed_person(self.c,doc))
  self.c.execute(text('UPDATE people SET monitoring_enabled=true'));self.c.execute(text('UPDATE sources SET enabled=true'));self.c.execute(text('UPDATE person_sources SET enabled=true'))
  self.assertTrue(allowed_person(self.c,doc))
 def test_html_round_baselines_without_fetching_old_details(self):
  from types import SimpleNamespace
  from app.web_http import Page
  from test_web_pilot import FED_URL,fed_html
  from app.web_sources import SOURCES
  repo.control(self.c,'fed',True);calls=[];index_html=f'<a href="{FED_URL}">Synthetic monetary speech</a>'.encode()
  class Connection:
   def begin(_):return self.c.begin_nested()
   def begin_nested(_):return self.c.begin_nested()
   def execute(_,statement,parameters=None):return self.c.execute(statement,parameters or {})
  lease=SimpleNamespace(c=Connection(),key='fed',require=lambda:repo.state(self.c,'fed'))
  class Reader:
   def __init__(_,source,**kw):_.audit=[]
   def __enter__(_):return _
   def __exit__(_, *args):pass
   def fetch(_,url):
    calls.append(url);return Page(url,index_html if url==SOURCES['fed'].index else fed_html(),'a'*64,datetime.now(timezone.utc),'fetched')
  result=html_round(lease,lambda:None,reader_factory=Reader)
  self.assertEqual(result['status'],'baseline_created');self.assertEqual(calls,[SOURCES['fed'].index]);self.assertEqual(self.c.execute(text('SELECT count(*) FROM web_documents')).scalar_one(),0)
  original=web_pilot.save_document(self.c,'fed',document(),datetime.now(timezone.utc));web_pilot.import_person(self.c,original['document_id'])
  self.c.execute(text('UPDATE people SET monitoring_enabled=true'));self.c.execute(text('UPDATE sources SET enabled=true'));self.c.execute(text('UPDATE person_sources SET enabled=true'))
  new_url=FED_URL.replace('20261112','20261113');index_html=f'<a href="{new_url}">New synthetic speech</a><a href="{FED_URL}">Known speech</a>'.encode()
  result=html_round(lease,lambda:None,reader_factory=Reader)
  self.assertEqual(result['items'][0]['status'],'saved');self.assertEqual(self.c.execute(text('SELECT count(*) FROM public_records')).scalar_one(),2)
  self.assertTrue(self.c.execute(text('SELECT is_catchup FROM speed_deliveries')).scalar_one())
  self.assertFalse(repo.pending(self.c,'fed'))
 def test_pending_capacity_is_bounded(self):
  self.c.execute(text("UPDATE speed_sources SET baseline_ready=true WHERE source='fed'"))
  self.c.execute(text("INSERT INTO speed_candidates(source,url,title,is_baseline,status) SELECT 'fed','https://example.invalid/'||x,'Synthetic',false,'pending' FROM generate_series(1,500) x"))
  with self.assertRaisesRegex(ValueError,'pending_capacity'):repo.frontier(self.c,'fed',[{'url':'https://example.invalid/new','title':'Extra'}])
 def test_source_lock_is_exclusive_across_sessions(self):
  from app.monitor_speed import Lease
  with patch('app.monitor_speed.create_engine',return_value=self.engine):
   with Lease(Settings(),'eia') as first:
    self.assertTrue(first.acquired)
    with Lease(Settings(),'eia') as second:self.assertFalse(second.acquired)
