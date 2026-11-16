import os,unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.core.config import Settings
from app.main import create_app
from app.monitoring_status import visible_state,summary
from app.repositories import monitor_speed as speed,web_pilot
import test_agent_tools_db as fixtures
from test_web_pilot import document


class SourceStatusTests(unittest.TestCase):
    def row(self):
        return dict(source_enabled=True,person_enabled=True,link_enabled=True,enabled=True,
                    pause_reason=None,last_result={'status':'completed'},failures=0,
                    interval_seconds=60,last_success_at=datetime.now(timezone.utc))
    def state(self,row,collector=None,configured=True):
        return visible_state(row,collector or dict(status='running',active=True),configured,datetime.now(timezone.utc))
    def test_inner_pause_never_appears_as_healthy_outer_wait(self):
        row=self.row();row['last_result']={'status':'paused'}
        self.assertEqual(self.state(row),'paused')
    def test_offline_stale_disabled_and_backoff_are_distinct(self):
        self.assertEqual(self.state(self.row()),'watching')
        self.assertEqual(self.state(self.row(),dict(status='stopped',active=False)),'offline')
        row=self.row();row['last_success_at']-=timedelta(hours=1);self.assertEqual(self.state(row),'stale')
        row=self.row();row['source_enabled']=False;self.assertEqual(self.state(row),'disabled')
        row=self.row();row['failures']=1;self.assertEqual(self.state(row),'backoff')
        row=self.row();row['last_success_at']=None;self.assertEqual(self.state(row),'awaiting_check')
    def test_public_projection_available_in_production_without_operations_access(self):
        with TestClient(create_app(Settings(_env_file=None,environment='production'))) as client,\
             patch('app.api.monitoring_status.collector_status',return_value=dict(status='running',active=True,pid=123,private_path='secret')),\
             patch('app.api.monitoring_status.execute',return_value=dict(items=[],model_requests=0)) as execute:
            response=client.get('/api/v1/monitoring-status',params={'person_id':str(uuid4())})
            self.assertEqual(response.status_code,200);self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(execute.call_args.args[3],dict(status='running',active=True))
            self.assertTrue(execute.call_args.kwargs['readonly'])
            self.assertEqual(client.get('/api/v1/monitor-speed').status_code,403)
            self.assertEqual(client.post('/api/v1/monitoring-status').status_code,405)
            self.assertEqual(client.get('/api/v1/monitoring-status?person_id=invalid').status_code,422)


@unittest.skipUnless(os.environ.get('PQ_SPEED_DB_TESTS')=='1','Disposable source status database tests')
class SourceStatusDatabase(unittest.TestCase):
    setUpClass=classmethod(fixtures.AgentToolDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.AgentToolDatabaseTests.drop_schema.__func__)
    def setUp(self):
        self.c=self.engine.connect();self.tx=self.c.begin()
        self.addCleanup(self.c.close);self.addCleanup(lambda:self.tx.rollback() if self.tx.is_active else None)
    def test_real_sql_projection_filters_person_and_keeps_saved_record_when_offline(self):
        doc=document();saved=web_pilot.save_document(self.c,'fed',doc,datetime.now(timezone.utc))
        linked=web_pilot.import_person(self.c,saved['document_id'])
        person=self.c.execute(text('SELECT person_id FROM public_records WHERE record_id=:id'),{'id':linked['record_id']}).scalar_one()
        self.c.execute(text('UPDATE people SET monitoring_enabled=true'))
        self.c.execute(text('UPDATE sources SET enabled=true'))
        self.c.execute(text('UPDATE person_sources SET enabled=true'))
        speed.control(self.c,'fed',True);start=speed.begin(self.c,'fed')['started_at']
        speed.finish(self.c,'fed',start,dict(status='completed',items=[]))
        value=summary(self.c,person,dict(status='running',active=True),True)
        self.assertEqual(len(value['items']),1);row=value['items'][0]
        self.assertEqual(row['status'],'watching');self.assertIsNotNone(row['last_checked_at'])
        self.assertEqual(str(row['latest_saved']['record_id']),linked['record_id'])
        self.assertEqual(summary(self.c,uuid4(),{},True)['items'],[])
        self.assertEqual(summary(self.c,person,dict(status='stopped',active=False),True)['items'][0]['status'],'offline')
        self.assertNotIn('last_result',row);self.assertNotIn('pause_reason',row)


if __name__=='__main__':unittest.main()
