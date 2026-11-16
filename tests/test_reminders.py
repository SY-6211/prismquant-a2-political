"""Real rule engine + isolated DB. No synthetic content reaches public schema."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import copy,os,unittest
from unittest.mock import patch
from uuid import UUID,uuid4
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.keyword_rules import Rule
from app.reminder_model import Feedback,body_hash
from app.repositories import reminders as repo,keywords
from app.repositories.events import save_snapshot
from app.event_model import snapshot_from_record,MediaContent
from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
import test_agent_qa_db as fixtures
from test_event_model import record

@unittest.skipUnless(os.environ.get('PQ_REMINDER_DB_TESTS')=='1','Isolated reminder tests are opt-in')
class ReminderTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.QADatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.QADatabaseTests.drop_schema.__func__)
    def setUp(self):
        fixtures.QADatabaseTests.setUp(self)
        self.source=self.c.execute(text('SELECT source_id FROM source_channels c JOIN event_versions v USING(channel_id) WHERE v.version_id=:id'),{'id':self.version['version_id']}).scalar_one()
        self.c.execute(text("INSERT INTO public_records(record_id,source_id,person_id,title,body_text,retrieved_url,provenance_type,content_hash) VALUES (:id,:s,:p,'Synthetic preview','We raise tariffs','https://example.invalid/reminders','third_party_archive',:hash)"),{'id':uuid4(),'s':self.source,'p':self.person,'hash':'b'*64})
        self.rule=self.publish();repo.configure(self.c,self.rule['rule_id']);repo.enable(self.c)
    def publish(self,**changes):
        definition=Rule(**{'name':'Synthetic tariffs','topic':'trade','groups':[['tariffs']],'person_ids':[self.person],'notification':'in_app_literal_hint',**changes})
        row=keywords.draft(self.c,definition,'synthetic-admin','isolated test')
        preview=keywords.preview(self.c,row['rule_id'],row['version_id'],'synthetic-admin')
        keywords.publish(self.c,row['rule_id'],row['version_id'],preview['preview_id'],row['revision'],'synthetic-admin','isolated publish')
        return row
    def new(self,body='We raise tariffs today.',review='accepted',original=None,**changes):
        snapshot=snapshot_from_record(record(person_id=self.person,body_text=body,original_url=original or 'https://truthsocial.com/@qa/'+str(uuid4().int)),{'archive_post_type':'original'},review_status=review)
        if changes:snapshot=snapshot.model_copy(update=changes)
        return save_snapshot(self.c,self.source,'rss',snapshot,datetime.now(timezone.utc))
    def alert(self):
        self.target=self.new();repo.process(self.c)
        return repo.listing(self.c)[0]
    def test_activation_does_not_replay_existing_content(self):
        self.assertEqual(repo.process(self.c)['created'],0);self.assertEqual(repo.listing(self.c),[])
    def test_live_main_text_creates_version_bound_literal_hint(self):
        row=self.alert();self.assertEqual(str(row['version_id']),self.target['version_id']);self.assertEqual(row['status'],'active');self.assertEqual(row['evidence']['claim_support'],'not_assessed');self.assertEqual(row['evidence']['engine_result']['hits'][0]['term'],'tariffs')
    def test_repeated_poll_does_not_repeat_reminder(self):
        self.alert();self.assertEqual(repo.process(self.c)['created'],0);self.assertEqual(len(repo.listing(self.c)),1)
    def test_identical_text_new_native_event_is_grouped_as_duplicate(self):
        first=self.alert();self.new();repo.process(self.c);rows=repo.listing(self.c,status='duplicate');self.assertEqual(len(rows),1);self.assertEqual(rows[0]['group_id'],first['reminder_id']);self.assertEqual(repo.detail(self.c,first['reminder_id'])['grouped_count'],1)
    def test_distinct_text_in_cooldown_is_retained_as_grouped(self):
        first=self.alert();self.new('A new tariffs announcement on imported goods.');repo.process(self.c);rows=repo.listing(self.c,status='grouped');self.assertEqual(len(rows),1);self.assertEqual(rows[0]['group_id'],first['reminder_id'])
    def test_zero_cooldown_allows_distinct_hints(self):
        repo.configure(self.c,self.rule['rule_id'],0);self.alert();self.new('Another tariffs policy statement.');repo.process(self.c);self.assertEqual(len(repo.listing(self.c)),2)
    def test_negation_and_quotes_wait_for_review_without_active_badge(self):
        self.new('We will not raise tariffs.');repo.process(self.c);self.assertEqual(repo.listing(self.c),[]);self.assertEqual(len(repo.listing(self.c,status='pending_review')),1)
    def test_after_review_mode_never_immediately_notifies(self):
        other=self.publish(notification='in_app_after_review');repo.configure(self.c,other['rule_id']);self.new();repo.process(self.c)
        pending=repo.listing(self.c,status='pending_review');self.assertEqual(len(pending),1);row=repo.review(self.c,pending[0]['reminder_id'],'approve','checked literal context',1);self.assertEqual(row['status'],'active')
    def test_other_person_or_needs_review_source_does_not_notify(self):
        self.new(review='needs_review');other=uuid4();self.c.execute(text("INSERT INTO people(person_id,name_original) VALUES (:id,'Other synthetic person')"),{'id':other});self.new(person_id=other)
        repo.process(self.c);self.assertEqual(repo.listing(self.c),[])
    def test_media_notes_and_quoted_posts_do_not_become_main_author_hits(self):
        self.new('Unrelated author statement.',media_notes=[MediaContent(position=0,kind='archive_transcript',text='tariffs',label='Transcript')]);repo.process(self.c);self.assertEqual(repo.listing(self.c),[])
        self.new(post_type='quote');repo.process(self.c);self.assertEqual(repo.listing(self.c),[])
    def test_source_revision_without_keyword_supersedes_old_alert(self):
        first=self.alert();original=self.c.execute(text("SELECT payload->>'original_url' FROM event_versions WHERE version_id=:id"),{'id':self.target['version_id']}).scalar_one()
        self.new('Revised statement about a different subject.',original=original);repo.process(self.c);old=repo.detail(self.c,first['reminder_id']);self.assertEqual(old['status'],'superseded');self.assertEqual(old['evidence'],first['evidence']);self.assertEqual(repo.listing(self.c),[])
    def test_manual_correction_and_restore_keep_audit_and_evidence(self):
        first=self.alert();corrected=repo.review(self.c,first['reminder_id'],'false_positive','Not relevant in context',1);self.assertEqual(corrected['status'],'corrected');self.assertEqual(repo.listing(self.c),[])
        with self.assertRaises(Exception):repo.review(self.c,first['reminder_id'],'restore','stale editor',1)
        restored=repo.review(self.c,first['reminder_id'],'restore','Rechecked the source',2);self.assertEqual(restored['status'],'active');self.assertEqual(len(repo.detail(self.c,first['reminder_id'])['actions']),2);self.assertEqual(restored['evidence'],first['evidence'])
    def test_feedback_is_user_owned_idempotent_and_does_not_remove_global_hint(self):
        first=self.alert();body=Feedback(reminder_id=first['reminder_id'],client_request_id=uuid4(),verdict='false_positive',note='Potential false positive')
        self.assertTrue(repo.feedback(self.c,self.a['user_id'],body)['created']);self.assertFalse(repo.feedback(self.c,self.a['user_id'],body)['created']);self.assertEqual(repo.one(self.c,first['reminder_id'])['status'],'active')
        with self.assertRaises(Exception):repo.feedback(self.c,self.a['user_id'],body.model_copy(update={'note':'different request'}))
        self.assertTrue(repo.feedback(self.c,self.b['user_id'],body)['created'])
    def test_audit_and_source_evidence_cannot_be_overwritten(self):
        first=self.alert();repo.review(self.c,first['reminder_id'],'false_positive','test correction',1)
        for query in ("UPDATE reminders SET evidence='{}'::jsonb","DELETE FROM reminders","DELETE FROM reminder_actions"):
            with self.assertRaises(DBAPIError),self.c.begin_nested():self.c.execute(text(query))
    def test_gap_pauses_without_advancing_cursor(self):
        position=repo.summary(self.c)['cursor_position'];self.new();self.c.execute(text('DELETE FROM content_changes WHERE position>:position'),{'position':position});self.assertEqual(repo.process(self.c)['status'],'paused_gap');self.assertEqual(repo.summary(self.c)['cursor_position'],position)
    def test_priority_pagination_has_no_duplicates(self):
        high=self.publish(name='High',severity='high',groups=[['raise']]);repo.configure(self.c,high['rule_id'],0);low=self.publish(name='Low',severity='low',groups=[['today']]);repo.configure(self.c,low['rule_id'],0);self.new();repo.process(self.c)
        page=repo.listing(self.c,limit=1);self.assertEqual(page[0]['priority'],'high');second=repo.listing(self.c,limit=1,cursor=page[0]['reminder_id']);self.assertEqual(second[0]['priority'],'medium');self.assertNotEqual(page[0]['reminder_id'],second[0]['reminder_id'])
    def test_disabled_rule_cannot_be_connected(self):
        rule=self.publish(notification='none')
        with self.assertRaises(ValueError):repo.configure(self.c,rule['rule_id'])
    def test_private_feedback_requires_existing_development_identity(self):
        first=self.alert()
        with patch('app.api.reminders.agent_analysis.read',side_effect=lambda settings,fn,*args:fn(self.c,*args)):
            self.assertEqual(self.client.get('/api/v1/reminders').status_code,200)
            self.assertEqual(self.client.post('/api/v1/agent-chat/reminder-feedback',json={'reminder_id':str(first['reminder_id']),'client_request_id':str(uuid4()),'verdict':'false_positive','note':'test'}).status_code,401)
            self.assertEqual(self.client.post('/api/v1/reminders').status_code,405)
            result=self.client.post('/api/v1/agent-chat/reminder-feedback',headers=self.ha,json={'reminder_id':str(first['reminder_id']),'client_request_id':str(uuid4()),'verdict':'false_positive','note':'test'})
            self.assertEqual(result.status_code,200)
    def test_zz_concurrent_processors_create_one_hint(self):
        self.new();self.tx.commit()
        def run():
            with self.engine.begin() as c:return repo.process(c)
        with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(sum(row['created'] for row in rows),1);self.assertEqual(len(repo.listing(self.c)),1);repo.disable(self.c);self.c.commit()

class ReminderContracts(unittest.TestCase):
    def test_normalized_fingerprint_and_feedback_bounds(self):
        self.assertEqual(body_hash('ＴＡＲＩＦＦＳ  increase'),body_hash('tariffs increase'))
        for note in ('','\x00','a'*501):
            with self.assertRaises(ValueError):Feedback(reminder_id=uuid4(),client_request_id=uuid4(),verdict='false_positive',note=note)
    def test_default_startup_has_no_processing(self):
        self.assertFalse(Settings(_env_file=None).reminder_processing_enabled)
