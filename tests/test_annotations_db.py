import json
import os
import unittest
from uuid import uuid4
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from fastapi.testclient import TestClient
import test_task_queue_db as fixtures
from app.repositories import annotations as repo
from app.repositories import keywords
from app.annotation_model import Label
from app.keyword_rules import Rule
from app.core.config import Settings
from app.main import create_app


@unittest.skipUnless(os.environ.get('PQ_ANNOTATION_DB_TESTS')=='1','Isolated annotation DB checks are opt-in')
class AnnotationDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.TaskDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.TaskDatabaseTests.drop_schema.__func__)

    def setUp(self):
        fixtures.TaskDatabaseTests.setUp(self)
        with self.engine.begin() as c:
            self.dataset=repo.create(c,'Synthetic evaluation')['dataset_id']
            for n,body in enumerate(('apples one','tariffs two','rivers three','tariffs four','letters five','boats six'),1):
                repo.add(c,self.dataset,body=body,person_id=uuid4(),published_at=f'2026-01-{n:02d}T00:00:00Z')
            repo.seal(c,self.dataset)
            self.sample=repo.samples(c,self.dataset,'dev')[0]
            self.rule=keywords.draft(c,Rule(name='Tariffs',topic='trade',groups=[['tariffs']]),'test','baseline')
        self.positive=Label(person_correct=True,relevant=True,topics=['trade'],importance='high',is_new=True,
            should_notify=False,boundary='main_text',evidence=[{'start':0,'end':7,'text':'tariffs'}],note='Synthetic fixture, not human ground truth')
        self.negative=Label(person_correct=True,relevant=False,topics=[],importance='low',is_new=False,should_notify=False,boundary='main_text',note='Synthetic fixture negative')

    def annotate(self,actor,label=None,revision=0,**kwargs):
        with self.engine.begin() as c:return repo.annotate(c,self.dataset,self.sample['sample_id'],actor,label or self.positive,revision,provenance='synthetic_fixture',**kwargs)

    def test_corpus_cannot_change_or_append_after_seal(self):
        with self.assertRaises(repo.Conflict),self.engine.begin() as c:repo.add(c,self.dataset,body='late words')
        for sql in ("UPDATE annotation_samples SET split='train'",'DELETE FROM annotation_samples',"UPDATE annotation_datasets SET state='draft'"):
            with self.assertRaises(DBAPIError),self.engine.begin() as c:c.execute(text(sql))

    def test_duplicate_capture_is_idempotent(self):
        with self.engine.begin() as c:
            d=repo.create(c,'Draft')['dataset_id'];a=repo.add(c,d,body='duplicate');b=repo.add(c,d,body='duplicate')
            self.assertEqual(a['sample_id'],b['sample_id']);self.assertFalse(b['created'])

    def test_label_evidence_must_match_snapshot(self):
        wrong=self.positive.model_copy(update={'evidence':[{'start':1,'end':8,'text':'tariffs'}]})
        with self.assertRaises(ValueError):self.annotate('A',wrong)

    def test_two_agreeing_labels_required_for_scored_baseline(self):
        self.annotate('A')
        with self.engine.begin() as c:
            first=repo.benchmark(c,self.dataset,'dev',[self.rule['version_id']])
            self.assertEqual(first['reviewed_eligible'],0);self.assertIsNone(first['synthetic_diagnostic_metrics']['relevance']['precision'])
        self.annotate('B')
        with self.engine.begin() as c:
            result=repo.benchmark(c,self.dataset,'dev',[self.rule['version_id']])
            self.assertEqual(result['synthetic_diagnostic_metrics']['relevance']['tp'],1)
            self.assertEqual(result['human_source_count'],0);self.assertFalse(result['sufficient_for_final_acceptance'])

    def test_disagreement_requires_third_reviewer_and_label_change_invalidates_it(self):
        self.annotate('A');self.annotate('B',self.negative)
        with self.assertRaises(repo.Conflict):self.annotate('A',self.positive,revision=1,adjudicate=True)
        self.annotate('C',adjudicate=True)
        with self.engine.connect() as c:self.assertEqual(repo.resolved(c,self.sample['sample_id'])['status'],'adjudicated')
        self.annotate('B',self.negative,revision=1)
        with self.engine.connect() as c:self.assertEqual(repo.resolved(c,self.sample['sample_id'])['status'],'disputed')

    def test_label_and_report_append_only(self):
        self.annotate('A');self.annotate('B')
        with self.engine.begin() as c:repo.benchmark(c,self.dataset,'dev',[self.rule['version_id']])
        for table in ('annotation_labels','annotation_runs'):
            with self.assertRaises(DBAPIError),self.engine.begin() as c:c.execute(text('DELETE FROM '+table))

    def test_stale_and_concurrent_reviewer_edits(self):
        def write():
            try:self.annotate('A');return True
            except repo.Conflict:return False
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(lambda _:write(),range(2))),1)

    def test_blind_export_and_benchmark_blocked_in_repository(self):
        with self.assertRaises(repo.Conflict),self.engine.connect() as c:repo.export_rows(c,self.dataset,'blind')
        with self.assertRaises(repo.Conflict),self.engine.begin() as c:repo.benchmark(c,self.dataset,'blind',[self.rule['version_id']])

    def test_sample_and_run_ids_are_scoped_to_dataset(self):
        self.annotate('A');self.annotate('B')
        with self.engine.begin() as c:
            other=repo.create(c,'Other')['dataset_id'];result=repo.benchmark(c,self.dataset,'dev',[self.rule['version_id']])
            with self.assertRaises(repo.Missing):repo.run_report(c,other,result['run_id'])
            with self.assertRaises(repo.Missing):repo.own_labels(c,other,self.sample['sample_id'],'A')

    def test_same_rule_and_labels_reproduce_fingerprint(self):
        self.annotate('A');self.annotate('B')
        with self.engine.begin() as c:
            a=repo.benchmark(c,self.dataset,'dev',[self.rule['version_id']]);b=repo.benchmark(c,self.dataset,'dev',[self.rule['version_id']])
            self.assertEqual(a['run_fingerprint'],b['run_fingerprint'])

    def test_http_role_separation_label_revision_and_saved_report(self):
        settings=Settings(_env_file=None,keyword_admin_token='k'*32,annotation_reviewer_tokens=json.dumps({'alice':'a'*32,'bob':'b'*32}))
        with patch('app.api.keywords.database_url',return_value='test-unused'),patch('app.api.keywords.create_engine',return_value=self.engine),patch.object(self.engine,'dispose'),TestClient(create_app(settings)) as client:
            base=f'/api/v1/annotation-review/{self.dataset}/samples/{self.sample["sample_id"]}/labels'
            for token in ('a','b'):
                response=client.post(base,headers={'Authorization':'Bearer '+token*32},json={'label':self.positive.model_dump(),'expected_revision':0})
                self.assertEqual(response.status_code,200,response.text)
            own=client.get(base,headers={'Authorization':'Bearer '+'a'*32}).json()['items']
            self.assertEqual(len(own),1);self.assertEqual(own[0]['reviewer'],'alice')
            url=f'/api/v1/annotation-datasets/{self.dataset}/benchmark'
            response=client.post(url,headers={'Authorization':'Bearer '+'k'*32},json={'split':'dev','version_ids':[str(self.rule['version_id'])]})
            self.assertEqual(response.status_code,200,response.text)
            # Synthetic origins remain diagnostic even when reviewers are authenticated.
            self.assertEqual(response.json()['human_source_count'],0)
            self.assertEqual(client.post(url,headers={'Authorization':'Bearer '+'a'*32},json={'version_ids':[str(self.rule['version_id'])]}).status_code,401)

    def test_source_capture_keeps_main_text_only_and_never_auto_labels(self):
        from app.collection_rules import SUPPORTED
        from app.event_model import snapshot_from_record,MediaContent
        from app.repositories.events import save_snapshot
        from datetime import datetime,timezone
        with self.engine.begin() as c:
            d=repo.create(c,'Source snapshots')['dataset_id'];rid=uuid4()
            c.execute(text("INSERT INTO public_records(record_id,source_id,person_id,title,body_text,retrieved_url,provenance_type,content_hash) VALUES (:r,:s,:p,'Source','tariffs rise','https://example.invalid/annotation','third_party_archive',:hash)"),{'r':rid,'s':SUPPORTED['trump'][0],'p':SUPPORTED['trump'][1],'hash':'a'*64})
            snap=snapshot_from_record({'title':'Source','content_type':'public_statement','body_text':'tariffs rise','person_id':SUPPORTED['trump'][1],
                'provenance_type':'third_party_archive','verification_status':'archive_only','original_url':'https://truthsocial.com/@test/1234567890',
                'retrieved_url':'https://example.invalid/annotation','speaker_evidence':'Test attribution'})
            snap=snap.model_copy(update={'media_notes':[MediaContent(position=0,kind='archive_image_description',text='Do not merge this note')]})
            event=save_snapshot(c,SUPPORTED['trump'][0],'rss',snap,datetime.now(timezone.utc),record_id=rid)
            a=repo.add(c,d,record_id=rid);b=repo.add(c,d,version_id=event['version_id'])
            rows=list(c.execute(text('SELECT * FROM annotation_samples WHERE dataset_id=:d'),{'d':d}).mappings())
            self.assertEqual({r['snapshot']['body'] for r in rows},{'tariffs rise'})
            self.assertEqual(len({r['event_key'] for r in rows}),1)
            self.assertEqual(c.execute(text('SELECT count(*) FROM annotation_labels')).scalar_one(),0)
            with self.assertRaises(ValueError):repo.annotate(c,self.dataset,self.sample['sample_id'],'fake',self.positive,provenance='bad_provenance')

    def test_synthetic_demo_reports_real_errors_without_claiming_human_quality(self):
        from app.annotation_demo import create_demo
        with self.engine.begin() as c:
            result=create_demo(c);metrics=result['train_report']['synthetic_diagnostic_metrics']['relevance']
            self.assertGreater(metrics['fp'],0);self.assertGreater(metrics['fn'],0)
            self.assertEqual(result['train_report']['human_source_count'],0)
            self.assertFalse(result['blind_evaluated'])
            self.assertGreater(result['manifest']['counts']['blind'],0)

    def test_uncertain_attribution_never_becomes_scored_ground_truth(self):
        value=self.positive.model_copy(update={'boundary':'uncertain'})
        self.annotate('A',value);self.annotate('B',value)
        with self.engine.begin() as c:
            self.assertEqual(repo.resolved(c,self.sample['sample_id'])['status'],'uncertain')
            result=repo.benchmark(c,self.dataset,'dev',[self.rule['version_id']])
            self.assertEqual(result['reviewed_eligible'],0)

    def test_capture_paginates_past_first_batch_without_duplicates(self):
        from app.api.annotations import capture_records
        from app.collection_rules import SUPPORTED
        with self.engine.begin() as c:
            identity=repo.create(c,'Paged capture')['dataset_id']
            for n in range(3):
                c.execute(text("INSERT INTO public_records(source_id,title,body_text,retrieved_url,provenance_type,content_hash) VALUES (:s,'Title',:body,:url,'third_party_archive',:hash)"),
                    {'s':SUPPORTED['trump'][0],'body':'Unique body '+str(n),'url':'https://example.invalid/page/'+str(n),'hash':'a'*64})
            first=capture_records(c,identity,2);second=capture_records(c,identity,2,first['next_record_id'])
            self.assertEqual(first['created'],2);self.assertEqual(second['created'],1);self.assertIsNone(second['next_record_id'])
            self.assertEqual(capture_records(c,identity,2)['created'],0)
