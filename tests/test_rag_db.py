"""Hybrid selection, frozen evidence and QA execution in disposable schemas."""
import json, os, tempfile, unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from uuid import UUID,uuid4
import httpx
from sqlalchemy import text
from app.agent_qa import Prepare,prepare,MODEL
from app.annotation_model import digest
from app.agent_tools import Hybrid,ToolSession
from app.event_model import snapshot_from_record
from app.local_embedding import chunks
from app.model_trial_live import units
from app.repositories import knowledge as index,chat,qa,web_qa
from app.repositories.agent_tools import SnapshotReader
from app.repositories.events import save_snapshot
from app.web_qa import execute_worker
import test_agent_qa_db as fixtures
from test_event_model import record

A=[1.]+[0.]*383;B=[0.,1.]+[0.]*382
class LocalFixtureEncoder:
    def token_count(self,value,role='passage'):return len(value)+4
    def embed(self,texts,role='query'):return [A for _ in texts]

@unittest.skipUnless(os.environ.get('PQ_RAG_DB_TESTS')=='1','Isolated RAG DB tests are opt-in')
class RAGDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.QADatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.QADatabaseTests.drop_schema.__func__)
    def setUp(self):
        fixtures.QADatabaseTests.setUp(self)
        self.source=self.c.execute(text('SELECT ch.source_id FROM source_channels ch JOIN event_versions v USING(channel_id) WHERE v.version_id=:v'),{'v':self.version['version_id']}).scalar_one()
        self.at=datetime.now(timezone.utc)
        self.snap=snapshot_from_record(record(person_id=self.person,body_text='Filler sentence. '*320+'Artificial intelligence increases financial risk. 😀 Later detail.',original_url=f'https://truthsocial.com/@test/{uuid4().int}'),{'archive_post_type':'original'})
        self.target=self.save(self.snap)
        p=patch('app.knowledge.encoder',return_value=LocalFixtureEncoder());p.start();self.addCleanup(p.stop)

    def save(self,snap,seconds=0):return save_snapshot(self.c,self.source,'rss',snap,self.at+timedelta(seconds=seconds))
    def build(self):
        index.enqueue(self.c)
        while target:=index.claim(self.c):
            if target.get('skipped'):continue
            parts=chunks(target['body'],LocalFixtureEncoder().token_count)
            vectors=[A if 'financial risk' in p['text'].lower() else B for p in parts]
            index.publish(self.c,target,parts,vectors)

    def args(self,**changes):return Hybrid(person_id=self.person,question='人工智能和金融风险',**changes)
    def retrieve(self,**changes):return SnapshotReader(self.c).hybrid(self.args(**changes))
    def prepared_rag(self):
        self.build()
        return prepare(None,self.ta,self.conv['conversation_id'],Prepare(client_request_id=uuid4(),message_id=self.message['message_id'],retrieval_mode='hybrid',limit=6))

    def test_semantic_and_keyword_retrieval_agree_on_deep_region(self):
        self.build();value=self.retrieve();self.assertTrue(value['retrieval']['semantic_vector_search'])
        self.assertEqual(value['items'][0]['version_id'],UUID(self.target['version_id']))
        self.assertGreater(value['items'][0]['start_offset'],1500)
        self.assertIn('financial risk',value['items'][0]['text'].lower())
        self.assertEqual(value['retrieval']['eligible_versions_in_scope'],1)
        self.assertEqual(value['retrieval']['ready_versions_in_scope'],1)

    def test_missing_vectors_still_find_lexical_content_and_report_coverage(self):
        value=self.retrieve();self.assertEqual(value['retrieval']['semantic_state'],'no_ready_index_in_scope')
        self.assertFalse(value['retrieval']['index_coverage_complete']);self.assertTrue(value['items'])

    def test_busy_and_missing_model_are_explicit_lexical_fallbacks(self):
        self.build()
        with patch('app.rag.knowledge.CPU') as cpu:
            cpu.acquire.return_value=False;value=self.retrieve()
        self.assertEqual(value['retrieval']['semantic_state'],'local_cpu_busy');self.assertTrue(value['items'])
        with patch('app.knowledge.encoder',side_effect=ImportError('not installed')):value=self.retrieve()
        self.assertEqual(value['retrieval']['semantic_state'],'local_vector_unavailable');self.assertTrue(value['items'])

    def test_person_dates_and_explicit_literal_filter_both_channels(self):
        self.build();reader=SnapshotReader(self.c)
        for args in (self.args(query="%_ ' OR 1=1 --"),self.args(since=datetime(2030,1,1,tzinfo=timezone.utc)),self.args(until=datetime(2020,1,1,tzinfo=timezone.utc))):
            self.assertEqual(reader.hybrid(args)['items'],[])
        other=uuid4();self.c.execute(text("INSERT INTO people(person_id,name_original) VALUES(:id,'Other person')"),{'id':other})
        secret=self.snap.model_copy(update={'person_id':other,'original_url':f'https://truthsocial.com/@other/{uuid4().int}','main_text':'Other person financial risk secret'})
        self.save(secret,1);self.build()
        self.assertTrue(all(r['person_id']==self.person for r in self.retrieve()['items']))

    def test_new_version_excludes_old_vectors_but_raw_keyword_finds_new_version(self):
        self.build();new=self.save(self.snap.model_copy(update={'main_text':'New artificial intelligence financial risk wording.'}),1)
        value=self.retrieve();self.assertEqual(str(value['items'][0]['version_id']),new['version_id'])
        self.assertEqual(value['retrieval']['ready_versions_in_scope'],0)

    def test_observation_head_is_shared_by_both_retrieval_and_exact_read(self):
        self.build();reader=SnapshotReader(self.c)
        self.save(self.snap.model_copy(update={'main_text':'Newer financial risk statement.'}),1)
        value=reader.hybrid(self.args());self.assertEqual(str(value['items'][0]['version_id']),self.target['version_id'])
        item=value['items'][0];session=ToolSession(reader,[self.person]);part=session.invoke('read_version',{'event_id':str(item['event_id']),'version_id':str(item['version_id']),'start':item['start_offset'],'length':item['end_offset']-item['start_offset']})
        self.assertEqual(part['source']['text'],item['text'])

    def test_holdout_post_indexing_is_excluded_from_both_channels(self):
        self.build();dataset=uuid4()
        self.c.execute(text("INSERT INTO annotation_datasets(dataset_id,name) VALUES(:id,'Reserved test')"),{'id':dataset})
        self.c.execute(text("INSERT INTO annotation_samples(sample_id,dataset_id,identity,origin,snapshot,event_key,split) VALUES(:id,:d,'reserved','source','{}',:e,'blind')"),{'id':uuid4(),'d':dataset,'e':self.target['event_id']})
        self.assertEqual(self.retrieve()['items'],[])

    def test_missing_optional_index_table_falls_back_without_aborting_snapshot(self):
        self.c.execute(text('ALTER TABLE kb_chunks RENAME TO kb_chunks_temporarily_missing'))
        value=self.retrieve()
        self.assertEqual(value['retrieval']['semantic_state'],'local_vector_unavailable')
        self.assertTrue(value['items']);self.assertEqual(self.c.execute(text('SELECT 1')).scalar_one(),1)

    def test_six_sources_are_distinct_versions_and_fit_tool_limits(self):
        for n in range(7):
            self.save(self.snap.model_copy(update={'original_url':f'https://truthsocial.com/@test/{uuid4().int}','main_text':f'Statement {n}: Artificial intelligence financial risk. Distinct evidence {n}.'}),n+1)
        run=self.prepared_rag();sources=run['context']['sources']
        self.assertEqual(len(sources),6)
        self.assertEqual(len({s['event_id'] for s in sources}),6)
        self.assertEqual(len(run['context']['tool_audit']),7)

    def test_pre_rag_literal_request_fingerprint_reopens_unchanged_v3_context(self):
        original=fixtures.QADatabaseTests.prepared(self);context=dict(original['context']);context['prompt_version']='person_qa_v3'
        body=self.spec.model_copy(update={'client_request_id':uuid4()})
        spec=json.loads(body.model_dump_json(exclude={'client_request_id','retrieval_mode','research_mode','use_history'}))
        old=qa.save_prepared(self.c,self.a['user_id'],self.conv['conversation_id'],body.client_request_id,spec,digest(spec),context,MODEL,'person_qa_v3')
        with patch('app.agent_qa.open_session',side_effect=AssertionError('Do not recompute old retry')):again=prepare(None,self.ta,self.conv['conversation_id'],body)
        self.assertEqual(again['run_id'],old['run_id']);self.assertEqual(again['context_hash'],old['context_hash'])

    def test_older_v2_history_is_readable_but_not_newly_executable(self):
        original=fixtures.QADatabaseTests.prepared(self);context=dict(original['context']);context['prompt_version']='person_qa_v2'
        body=self.spec.model_copy(update={'client_request_id':uuid4()})
        spec=json.loads(body.model_dump_json(exclude={'client_request_id','retrieval_mode','research_mode','use_history'}))
        old=qa.save_prepared(self.c,self.a['user_id'],self.conv['conversation_id'],body.client_request_id,spec,digest(spec),context,MODEL,'person_qa_v2')
        self.assertEqual(qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],old['run_id'])['prompt_version'],'person_qa_v2')
        web_qa.grant(self.c,self.a['user_id'],'synthetic-old-prompt',units('.1'),2,1)
        with self.assertRaises(chat.Invalid):web_qa.enqueue(self.c,self.a['user_id'],self.conv['conversation_id'],old['run_id'])

    def test_user_cannot_prepare_another_users_hybrid_question(self):
        body=Prepare(client_request_id=uuid4(),message_id=self.message['message_id'],retrieval_mode='hybrid')
        response=self.client.post(self.url+'/answers',headers=self.hb,json=body.model_dump(mode='json'))
        self.assertEqual(response.status_code,404)

    def test_hybrid_preparation_freezes_retrieval_and_retry_does_not_recompute(self):
        run=self.prepared_rag();body=Prepare(client_request_id=run['client_request_id'],message_id=self.message['message_id'],retrieval_mode='hybrid',limit=6)
        with patch('app.knowledge.encoder',side_effect=AssertionError('Idempotent retry must not embed')):again=prepare(None,self.ta,self.conv['conversation_id'],body)
        self.assertEqual(run['context_hash'],again['context_hash']);self.assertEqual(run['run_id'],again['run_id'])
        self.assertFalse(run['context']['history_is_evidence']);self.assertIn('version',run['context']['retrieval'])

    def test_mocked_generation_cites_rag_sources_and_never_repeats_request(self):
        run=self.prepared_rag();context=run['context'];source=context['sources'][0]
        answer={'outcome':'answered','points':[{'text_zh':'据保存的原文，作者提到金融风险。','citations':[{'source_ref':source['source_ref'],'text':'financial risk'}]}],'limitations_zh':['合成测试，不代表模型能力。'],'needs_review':True}
        web_qa.grant(self.c,self.a['user_id'],'synthetic-rag',units('.1'),2,1)
        web_qa.enqueue(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])
        posts=[]
        def handler(request):
            if request.method=='POST':
                posts.append(request)
                return httpx.Response(200,json={'model':MODEL,'usage':{'cost':.001},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer,ensure_ascii=False)}}]})
            if request.url.path.endswith('/models'):return httpx.Response(200,json={'data':[{'id':MODEL,'supported_parameters':['structured_outputs'],'pricing':{'prompt':'.000001','completion':'.000002'}}]})
            return httpx.Response(200,json={'data':{'limit_remaining':10}})
        def service(settings,fn,*args):return fn(self.c,*args)
        with tempfile.TemporaryDirectory() as folder,patch('app.web_qa.service',side_effect=service),patch('app.web_qa.private_key',return_value='synthetic-key'),patch('app.web_qa.agent_analysis.read',return_value=False):
            for _ in range(2):execute_worker(None,self.ta,self.conv['conversation_id'],run['run_id'],client_factory=lambda:httpx.Client(transport=httpx.MockTransport(handler)),receipt_dir=Path(folder))
        saved=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])
        self.assertEqual(len(posts),1);self.assertEqual(saved['status'],'completed')
        self.assertTrue(saved['answer']['points'][0]['citations'][0]['valid'])


if __name__=='__main__':unittest.main()
