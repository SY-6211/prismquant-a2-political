import os
import unittest
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
import test_task_queue_db as fixtures
from app.keyword_rules import Rule
from app.repositories import keywords as repo
from app.collection_rules import SUPPORTED
from fastapi.testclient import TestClient
from unittest.mock import patch
from app.main import create_app
from app.core.config import Settings


@unittest.skipUnless(os.environ.get('PQ_KEYWORD_DB_TESTS')=='1','Isolated keyword schema tests are opt-in')
class KeywordDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.TaskDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.TaskDatabaseTests.drop_schema.__func__)

    def setUp(self):
        fixtures.TaskDatabaseTests.setUp(self)
        self.person=SUPPORTED['trump'][1]
        self.definition=Rule(name='Trade',topic='trade',groups=[['tariffs','关税']],person_ids=[self.person])
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO public_records(source_id,person_id,title,body_text,retrieved_url,provenance_type,content_hash) VALUES (:s,:p,'Synthetic','We raise tariffs','https://example.invalid/kw','third_party_archive',:hash)"),{'s':SUPPORTED['trump'][0],'p':self.person,'hash':'a'*64})
            self.first=repo.draft(c,self.definition,'test-admin','initial')

    def receipt(self,version=None):
        with self.engine.begin() as c:return repo.preview(c,self.first['rule_id'],version or self.first['version_id'],'test-admin')

    def test_draft_preview_publish_and_rollback_preserve_old_versions(self):
        a=self.first;receipt=self.receipt()
        with self.engine.begin() as c:
            pub=repo.publish(c,a['rule_id'],a['version_id'],receipt['preview_id'],1,'test-admin','publish')
            second=repo.draft(c,self.definition.model_copy(update={'exclude_any':['raise']}),'test-admin','edit',a['rule_id'],pub['revision'])
        other=self.receipt(second['version_id'])
        with self.engine.begin() as c:
            pub=repo.publish(c,a['rule_id'],second['version_id'],other['preview_id'],second['revision'],'test-admin','second')
            old=repo.publish(c,a['rule_id'],a['version_id'],receipt['preview_id'],pub['revision'],'test-admin','rollback')
            self.assertEqual(old['published_version'],a['version_id'])
            self.assertEqual(len(repo.history(c,a['rule_id'])),2)
            self.assertEqual(c.execute(text('SELECT count(*) FROM keyword_publications')).scalar_one(),3)

    def test_stale_editor_rejected(self):
        with self.engine.begin() as c:repo.draft(c,self.definition,'admin','edit',self.first['rule_id'],1)
        with self.assertRaises(repo.Conflict),self.engine.begin() as c:repo.draft(c,self.definition,'admin','stale',self.first['rule_id'],1)

    def test_publish_requires_matching_preview(self):
        receipt=self.receipt()
        with self.engine.begin() as c:other=repo.draft(c,self.definition,'admin','another')
        with self.assertRaises(repo.Conflict),self.engine.begin() as c:
            repo.publish(c,other['rule_id'],other['version_id'],receipt['preview_id'],1,'admin','wrong receipt')

    def test_wrong_rule_cannot_read_version(self):
        with self.assertRaises(repo.Missing),self.engine.connect() as c:repo.version(c,uuid4(),self.first['version_id'])

    def test_immutable_versions_previews_and_audit(self):
        receipt=self.receipt()
        with self.engine.begin() as c:repo.publish(c,self.first['rule_id'],self.first['version_id'],receipt['preview_id'],1,'admin','publish')
        for table in ('keyword_versions','keyword_previews','keyword_publications'):
            with self.assertRaises(DBAPIError),self.engine.begin() as c:c.execute(text('DELETE FROM '+table))

    def test_preview_does_not_modify_records_or_send_notifications(self):
        self.assertEqual(self.receipt()['counts'],{'matched':1})
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text('SELECT count(*) FROM content_changes')).scalar_one(),0)
            self.assertEqual(c.execute(text('SELECT body_text FROM public_records')).scalar_one(),'We raise tariffs')

    def test_concurrent_edits_have_one_winner(self):
        def edit():
            try:
                with self.engine.begin() as c:repo.draft(c,self.definition,'admin','race',self.first['rule_id'],1)
                return True
            except repo.Conflict:return False
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(lambda _:edit(),range(2))),1)

    def test_empty_preview_cannot_authorize_publish(self):
        with self.engine.begin() as c:
            c.execute(text('DELETE FROM public_records'))
        receipt=self.receipt()
        with self.assertRaises(repo.Conflict),self.engine.begin() as c:repo.publish(c,self.first['rule_id'],self.first['version_id'],receipt['preview_id'],1,'admin','empty')

    def test_authenticated_http_workflow_against_isolated_database(self):
        with patch('app.api.keywords.database_url',return_value='unused-test-url'),patch('app.api.keywords.create_engine',return_value=self.engine),patch.object(self.engine,'dispose'),TestClient(create_app(Settings(_env_file=None,keyword_admin_token='k'*32))) as client:
            headers={'Authorization':'Bearer '+'k'*32};base='/api/v1/keyword-rules'
            response=client.post(base,headers=headers,json={'definition':self.definition.model_dump(mode='json'),'note':'API draft'})
            self.assertEqual(response.status_code,200,response.text);a=response.json()
            response=client.post(f"{base}/{a['rule_id']}/versions/{a['version_id']}/preview",headers=headers,json={'limit':10})
            self.assertEqual(response.status_code,200,response.text);receipt=response.json()
            response=client.post(f"{base}/{a['rule_id']}/publish",headers=headers,json={'version_id':a['version_id'],'preview_id':receipt['preview_id'],'expected_revision':a['revision'],'note':'Test release'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(client.get(f"{base}/{a['rule_id']}/previews/{receipt['preview_id']}",headers=headers).status_code,200)
            self.assertEqual(len(client.get(f"{base}/{a['rule_id']}/publications",headers=headers).json()['items']),1)
            self.assertEqual(client.get(base).status_code,401)
