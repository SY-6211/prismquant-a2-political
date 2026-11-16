"""All synthetic analysis writes are restricted to a temporary PostgreSQL schema."""
import copy
import os
import unittest
from unittest.mock import patch
from uuid import UUID,uuid4
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError,SQLAlchemyError

import test_agent_tools_db as fixtures
from app.agent_analysis import MODEL,PROMPT_VERSION,prepare_context,validate_analysis,create_prepared
from app.agent_tools import ToolSession
from app.repositories.agent_tools import SnapshotReader
from app.repositories import agent_analysis as repo
from app.core.config import Settings
from app.main import create_app


@unittest.skipUnless(os.environ.get('PQ_AGENT_ANALYSIS_DB_TESTS')=='1','Isolated analysis DB tests are opt-in')
class AnalysisDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.AgentToolDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.AgentToolDatabaseTests.drop_schema.__func__)

    save=fixtures.AgentToolDatabaseTests.save

    def setUp(self):
        fixtures.AgentToolDatabaseTests.setUp(self)
        self.context=prepare_context(ToolSession(SnapshotReader(self.c),[self.person]),UUID(self.first['event_id']),UUID(self.first['version_id']))
        self.analysis_run=repo.prepared(self.c,'synthetic:'+uuid4().hex,self.context,MODEL,PROMPT_VERSION)
        self.answer={'relevant':True,'topics':['trade_policy'],'importance':'medium','summary_zh':'合成测试政策内容。',
            'facts':[{'text_zh':'合成原文内容。','citations':[{'source_ref':self.context['target_refs'][0],'text':'Alpha 😀政策'}]}],
            'history_assessment':'not_assessed','comparison_zh':'未比较历史。','comparison_citations':[],'hypotheses':[],'needs_review':True}

    def save_analysis(self):
        return repo.save_result(self.c,self.analysis_run['run_id'],'completed',validate_analysis(self.context,self.answer),
            'a'*64,{'fixture':True},execution_kind='synthetic_fixture')

    def test_prepare_and_result_are_idempotent_without_changing_sources(self):
        before=self.c.execute(text('SELECT snapshot_hash FROM event_versions WHERE version_id=:id'),{'id':self.first['version_id']}).scalar_one()
        again=repo.prepared(self.c,self.analysis_run['request_id'],self.context,MODEL,PROMPT_VERSION)
        self.assertEqual(again['run_id'],self.analysis_run['run_id'])
        one=self.save_analysis();two=self.save_analysis();self.assertEqual(one['result_id'],two['result_id'])
        self.assertEqual(self.c.execute(text('SELECT snapshot_hash FROM event_versions WHERE version_id=:id'),{'id':self.first['version_id']}).scalar_one(),before)

    def test_history_is_append_only_and_reanalysis_keeps_old_answer(self):
        self.save_analysis()
        new=repo.prepared(self.c,'new:'+uuid4().hex,self.context,MODEL,PROMPT_VERSION)
        self.assertNotEqual(new['run_id'],self.analysis_run['run_id'])
        for table,key in (('agent_analysis_runs','run_id'),('agent_analysis_results','run_id')):
            with self.assertRaises(DBAPIError),self.c.begin_nested():
                self.c.execute(text(f'DELETE FROM {table} WHERE {key}=:id'),{'id':self.analysis_run['run_id']})
        self.assertIsNotNone(repo.select_detail(self.c,self.analysis_run['run_id'])['analysis'])

    def test_conflicting_result_or_input_key_cannot_overwrite(self):
        self.save_analysis()
        with self.assertRaises(ValueError):repo.save_result(self.c,self.analysis_run['run_id'],'failed',None,'b'*64,{},failure_code='fixture')
        bad=copy.deepcopy(self.context);bad['person_id']=str(self.other)
        with self.assertRaises(ValueError):repo.prepared(self.c,self.analysis_run['request_id'],bad,MODEL,PROMPT_VERSION)

    def test_list_cursor_and_person_filter_preserve_prepared_state(self):
        for _ in range(2):repo.prepared(self.c,'page:'+uuid4().hex,self.context,MODEL,PROMPT_VERSION)
        page=repo.select_list(self.c,self.person,None,2)
        self.assertEqual(len(page),3)
        rest=repo.select_list(self.c,self.person,None,2,page[1]['run_id'])
        self.assertEqual(len(rest),1);self.assertEqual(rest[0]['status'],'prepared_or_unfinished')
        self.assertEqual(repo.select_list(self.c,self.other),[])

    def test_read_only_http_endpoints_and_bounded_error(self):
        self.save_analysis();client=TestClient(create_app(Settings()))
        with patch('app.api.agent_analysis.repo.read',side_effect=lambda settings,fn,*args:fn(self.c,*args)):
            response=client.get('/api/v1/agent-analyses',params={'person_id':str(self.person)})
            self.assertEqual(response.status_code,200);self.assertEqual(response.json()['items'][0]['status'],'completed')
            detail=client.get('/api/v1/agent-analyses/'+str(self.analysis_run['run_id']))
            self.assertEqual(detail.status_code,200);self.assertEqual(detail.json()['execution_kind'],'synthetic_fixture')
            self.assertEqual(client.get('/api/v1/agent-analyses/'+str(uuid4())).status_code,404)
            self.assertEqual(client.get('/api/v1/agent-analyses?limit=101').status_code,422)
            self.assertEqual(client.post('/api/v1/agent-analyses',json={}).status_code,405)
        with patch('app.api.agent_analysis.repo.read',side_effect=SQLAlchemyError('private-password')):
            error=client.get('/api/v1/agent-analyses');self.assertEqual(error.status_code,503);self.assertNotIn('private-password',error.text)

    def test_existing_request_reuses_frozen_input_and_refuses_wrong_target(self):
        with patch('app.repositories.agent_analysis.read_by_request',return_value=self.analysis_run),patch('app.agent_analysis.open_session') as session:
            same=create_prepared(Settings(),self.person,self.analysis_run['request_id'])
            self.assertEqual(same['run_id'],self.analysis_run['run_id']);session.assert_not_called()
            with self.assertRaises(ValueError):create_prepared(Settings(),self.person,self.analysis_run['request_id'],uuid4(),UUID(self.first['version_id']))

    def test_reserved_public_event_is_excluded_without_reading_labels(self):
        from app.repositories import annotations
        dataset=annotations.create(self.c,'Synthetic reserved event')['dataset_id']
        sample=annotations.add(self.c,dataset,version_id=UUID(self.first['version_id']))['sample_id']
        self.c.execute(text("UPDATE annotation_samples SET split='blind' WHERE sample_id=:id"),{'id':sample})
        self.assertTrue(repo.context_has_reserved(self.c,self.context))
        with self.assertRaises(ValueError):prepare_context(ToolSession(SnapshotReader(self.c),[self.person]),UUID(self.first['event_id']),UUID(self.first['version_id']))
