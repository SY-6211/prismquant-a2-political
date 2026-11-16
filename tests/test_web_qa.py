import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import httpx
from sqlalchemy import text
from app.agent_qa import MODEL,prepare
from app.repositories import chat,qa,web_qa as repo
from app.web_qa import execute_worker,recover
from app.model_trial_live import units
import test_agent_qa_db as qa_fixtures

@unittest.skipUnless(os.environ.get('PQ_WEB_QA_DB_TESTS')=='1','Isolated online QA checks are opt-in')
class WebQATests(unittest.TestCase):
    setUpClass=classmethod(qa_fixtures.QADatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(qa_fixtures.QADatabaseTests.drop_schema.__func__)
    def setUp(self):
        qa_fixtures.QADatabaseTests.setUp(self)
        self.run=prepare(None,self.ta,self.conv['conversation_id'],self.spec)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.patch_service=patch('app.web_qa.service',side_effect=lambda settings,fn,*args:fn(self.c,*args));self.patch_service.start();self.addCleanup(self.patch_service.stop)
        self.patch_reserved=patch('app.web_qa.agent_analysis.read',return_value=False);self.patch_reserved.start();self.addCleanup(self.patch_reserved.stop)
        self.patch_key=patch('app.web_qa.private_key',return_value='sk-or-synthetic-not-a-real-key');self.patch_key.start();self.addCleanup(self.patch_key.stop)
        self.posts=0
    def allowance(self,cap='.10',calls=5):return repo.grant(self.c,self.a['user_id'],'synthetic-'+str(uuid4()),units(cap),calls,2)
    def enqueue(self):return repo.enqueue(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])
    def envelope(self):
        ref=self.run['context']['sources'][0]['source_ref']
        answer={'outcome':'answered','points':[{'text_zh':'据来源，作者谈及政策。','citations':[{'source_ref':ref,'text':'政策 tariffs'}]}],'limitations_zh':['合成原文，未作事实核实。'],'needs_review':True}
        return {'id':'synthetic-web','model':MODEL,'provider':'Synthetic','usage':{'cost':.001},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer,ensure_ascii=False)}}]}
    def worker(self,envelope=None,timeout=False,preflight=False):
        value=self.envelope() if envelope is None else envelope
        def handler(request):
            if request.method=='POST':
                self.posts+=1
                if timeout:raise httpx.ReadTimeout('synthetic timeout',request=request)
                return httpx.Response(200,json=value)
            if request.url.path.endswith('/models'):
                return httpx.Response(200,json={'data':[] if preflight else [{'id':MODEL,'supported_parameters':['structured_outputs'],'pricing':{'prompt':'.000001','completion':'.000002'}}]})
            return httpx.Response(200,json={'data':{'limit_remaining':10}})
        factory=lambda:httpx.Client(transport=httpx.MockTransport(handler))
        return execute_worker(None,self.ta,self.conv['conversation_id'],self.run['run_id'],client_factory=factory,receipt_dir=Path(self.tmp.name))
    def test_no_budget_is_off_and_does_not_claim_a_model_execution(self):
        self.assertFalse(repo.summary(self.c,self.a['user_id'])['can_generate'])
        with self.assertRaises(chat.Conflict):self.enqueue()
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM agent_qa_executions')).scalar_one(),0)
    def test_finite_grant_cannot_reuse_an_approval_or_exceed_limits(self):
        grant=self.allowance()
        with self.assertRaises(chat.Conflict):repo.grant(self.c,self.a['user_id'],grant['approval'],units('.1'),5,2)
        for cap,calls,hours in [(0,1,2),(units('11'),1,2),(units('.1'),101,2),(units('.1'),1,25)]:
            with self.assertRaises(chat.Invalid):repo.grant(self.c,self.a['user_id'],'bad',cap,calls,hours)
    def test_enqueue_replay_reserves_once_and_user_scope_is_enforced(self):
        self.allowance();one=self.enqueue();two=self.enqueue();self.assertTrue(one['created']);self.assertFalse(two['created'])
        with self.assertRaises(chat.Missing):repo.enqueue(self.c,self.b['user_id'],self.conv['conversation_id'],self.run['run_id'])
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM agent_web_qa_jobs')).scalar_one(),1)
    def test_completed_answer_is_saved_and_worker_replay_never_sends_again(self):
        self.allowance();self.enqueue();self.assertEqual(self.worker()['status'],'completed');self.worker()
        self.assertEqual(self.posts,1);self.assertEqual(repo.summary(self.c,self.a['user_id'])['used_or_reserved_usd'],'0.001')
        saved=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id']);self.assertTrue(saved['answer']['points'][0]['citations'][0]['valid'])
    def test_research_worker_persists_and_reconciles_new_contract_without_losing_inferences(self):
        from app.agent_qa import Prepare
        from app.chat_model import UserMessage
        from test_research_analysis import research_fixture
        from app import qa_prompts
        message=chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='这段关税表态对股票市场有什么影响？'))['message']
        self.run=prepare(None,self.ta,self.conv['conversation_id'],Prepare(client_request_id=uuid4(),message_id=message['message_id'],query='tariffs'))
        self.assertEqual(self.run['prompt_version'],qa_prompts.RESEARCH)
        self.allowance();self.enqueue();value=self.envelope();_,answer=research_fixture()
        answer['points'][0]['citations'][0]['source_ref']=self.run['context']['sources'][0]['source_ref']
        value['choices'][0]['message']['content']=json.dumps(answer,ensure_ascii=False)
        self.assertEqual(self.worker(value)['status'],'completed');self.assertEqual(self.posts,1)
        result=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])
        self.assertEqual(result['answer']['answer_mode'],'research');self.assertTrue(result['answer']['analysis'])
        with patch('app.web_qa.RUNTIME',Path(self.tmp.name)),patch('app.web_qa.service',side_effect=lambda settings,fn,*args:fn(self.c,*args)):
            receipt=Path(self.tmp.name)/(str(self.run['run_id'])+'.json')
            folder=Path(self.tmp.name)/'receipts';folder.mkdir();receipt.replace(folder/receipt.name)
            self.assertEqual(recover(None,self.run['run_id'])['status'],'completed')
        self.assertEqual(self.posts,1)
    def test_unknown_cost_stops_every_new_web_grant(self):
        self.allowance();self.enqueue();value=self.envelope();del value['usage']['cost'];self.assertEqual(self.worker(value)['status'],'unknown')
        self.allowance();self.assertEqual(repo.summary(self.c,self.a['user_id'])['reason'],'needs_review');self.assertEqual(self.posts,1)
    def test_dual_source_generation_history_scope_and_receipt_replay(self):
        from app.agent_qa import Prepare,raw_answer
        from app.chat_model import UserMessage
        from app import research_sources as nav,qa_prompts
        from test_research_synthesis import resource,synthesis_fixture,select_quotes
        original=nav.prepare
        def lookup(*args,**kw):
            return dict(resources=[resource()],reads=1,network_requests=2,search={'status':'not_configured','requests':0}) if kw.get('enabled') else original(*args,**kw)
        message=chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='人工智能对银行行业有什么影响？'))['message']
        spec=Prepare(client_request_id=uuid4(),message_id=message['message_id'],retrieval_mode='hybrid',read_official_sources=True,query='tariffs')
        with patch('app.research_sources.prepare',side_effect=lookup):self.run=prepare(None,self.ta,self.conv['conversation_id'],spec)
        self.assertEqual(self.run['prompt_version'],qa_prompts.SYNTHESIS);self.assertTrue(self.run['context']['background_sources'])
        self.allowance();self.enqueue();_,answer=synthesis_fixture()
        if self.run['context']['sources']:answer['points'][0]['citations'][0]['source_ref']=self.run['context']['sources'][0]['source_ref']
        else:answer['points']=[];answer['answer_person_basis']=[];answer['analysis'][0]['person_basis']=[]
        answer['background_points'][0]['citations'][0]['source_ref']=self.run['context']['background_sources'][0]['source_ref']
        answer=select_quotes(self.run['context'],answer)
        from test_agent_qa import envelope
        value=envelope(answer)
        self.assertEqual(self.worker(value)['status'],'completed');self.worker(value);self.assertEqual(self.posts,1)
        saved=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])
        self.assertEqual(saved['answer']['answer_mode'],'synthesis');self.assertTrue(raw_answer(saved['context'],saved['answer'])['analysis'])
        with self.assertRaises(chat.Missing):qa.detail(self.c,self.b['user_id'],self.conv['conversation_id'],self.run['run_id'])
        next_message=chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='那风险呢？'))['message']
        history=qa.question(self.c,self.a['user_id'],self.conv['conversation_id'],next_message['message_id'])['history']
        self.assertTrue(history[-1]['source_resources']);self.assertEqual(history[-1]['source_resources'][0]['role'],'background')
        with patch('app.web_qa.RUNTIME',Path(self.tmp.name)):
            receipt=Path(self.tmp.name)/(str(self.run['run_id'])+'.json');folder=Path(self.tmp.name)/'receipts';folder.mkdir();receipt.replace(folder/receipt.name)
            self.assertEqual(recover(None,self.run['run_id'])['status'],'completed')
        self.assertEqual(self.posts,1)
    def test_post_timeout_is_not_retried_and_keeps_reservation(self):
        self.allowance();self.enqueue();self.assertEqual(self.worker(timeout=True)['status'],'unknown');self.worker();self.assertEqual(self.posts,1)
    def test_rejected_citation_is_charged_but_not_shown_as_answer(self):
        self.allowance();self.enqueue();value=self.envelope();answer=json.loads(value['choices'][0]['message']['content']);answer['points'][0]['citations'][0]['text']='invented';value['choices'][0]['message']['content']=json.dumps(answer)
        self.assertEqual(self.worker(value)['status'],'rejected');self.assertEqual(repo.summary(self.c,self.a['user_id'])['used_or_reserved_usd'],'0.001')
        self.assertIsNone(qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])['answer'])
    def test_truncated_answer_records_specific_reason_and_never_retries_or_refunds(self):
        self.allowance();self.enqueue();value=self.envelope();value['choices'][0]['finish_reason']='length'
        self.assertEqual(self.worker(value)['status'],'rejected');self.worker(value)
        saved=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])
        self.assertEqual(saved['failure_code'],'qa_output_truncated');self.assertEqual(self.posts,1)
        self.assertEqual(saved['receipt']['output_check']['finish_reason'],'length')
        self.assertEqual(repo.summary(self.c,self.a['user_id'])['used_or_reserved_usd'],'0.001')
    def test_schema_rejection_has_safe_field_diagnostics_and_private_replay_file(self):
        self.allowance();self.enqueue();value=self.envelope()
        answer=json.loads(value['choices'][0]['message']['content']);answer['points'][0]['text_zh']=''
        value['choices'][0]['message']['content']=json.dumps(answer)
        self.assertEqual(self.worker(value)['status'],'rejected')
        saved=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])
        self.assertEqual(saved['failure_code'],'qa_schema_rejected')
        diagnostic=saved['receipt']['output_check']['details']['fields'][0]
        self.assertEqual(diagnostic['field'],['points',0,'text_zh']);self.assertNotIn('input',diagnostic)
        artifact=Path(self.tmp.name)/'rejected-responses'/(str(self.run['run_id'])+'.json')
        self.assertEqual(json.loads(artifact.read_text(encoding='utf8')),value)
        self.assertEqual(self.posts,1)
    def test_general_methods_without_sources_can_prepare_generate_and_restore(self):
        from app.agent_qa import Prepare
        from app.chat_model import UserMessage
        from test_agent_workflow import method_fixture
        value=self.envelope()
        message=chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='如果研究金融股票，需要核对哪些数据？'))['message']
        self.run=prepare(None,self.ta,self.conv['conversation_id'],Prepare(client_request_id=uuid4(),message_id=message['message_id'],query='NO_SAVED_MATCH_938471'))
        self.assertEqual(self.run['context']['research_task'],'method_checklist');self.assertFalse(self.run['context']['sources'])
        self.assertEqual(qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])['status'],'prepared')
        self.allowance();self.enqueue();_,answer=method_fixture();value['choices'][0]['message']['content']=json.dumps(answer,ensure_ascii=False)
        self.assertEqual(self.worker(value)['status'],'completed')
        saved=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])
        self.assertEqual(saved['answer']['research_steps'][0]['basis_type'],'general_method');self.assertFalse(saved['answer']['points'])
        self.assertTrue(chat.list_conversations(self.c,self.a['user_id'],query='企业AI投入'))
        self.worker(value);self.assertEqual(self.posts,1)
    def test_preflight_failure_does_not_send_or_charge(self):
        self.allowance();self.enqueue();self.assertEqual(self.worker(preflight=True)['status'],'failed');self.assertEqual(self.posts,0);self.assertEqual(repo.summary(self.c,self.a['user_id'])['used_or_reserved_usd'],'0')
    def test_queued_cancellation_or_revoked_identity_never_sends(self):
        self.allowance();self.enqueue();chat.revoke_token(self.c,self.issued_a['token_id'])
        self.assertEqual(self.worker()['status'],'failed');self.assertEqual(self.posts,0)
        self.assertEqual(repo.summary(self.c,self.a['user_id'])['used_or_reserved_usd'],'0')
    def test_parallel_submitters_have_one_committed_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        self.allowance();self.tx.commit()
        def submit():
            with self.engine.begin() as c:return repo.enqueue(c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:submit(),range(2)))
        self.assertEqual(sum(v['created'] for v in results),1)
        self.worker();self.c.commit();self.assertEqual(self.posts,1)
    def test_expired_closed_or_insufficient_grants_do_not_generate(self):
        g=self.allowance();repo.close(self.c,g['grant_id'])
        with self.assertRaises(chat.Conflict):self.enqueue()
        g=self.allowance();self.c.execute(text("UPDATE agent_web_qa_grants SET expires_at=now()-interval '1 second',created_at=now()-interval '2 hours' WHERE grant_id=:id"),{'id':g['grant_id']})
        with self.assertRaises(chat.Conflict):self.enqueue()
        self.allowance(cap='.000000001')
        with self.assertRaises(chat.Conflict):self.enqueue()
    def test_saved_receipt_recovers_database_failure_without_resending(self):
        self.allowance();self.enqueue();real=repo.finish
        with patch('app.repositories.web_qa.finish',side_effect=RuntimeError('synthetic persistence failure')):
            with self.assertRaises(RuntimeError):self.worker()
        self.assertEqual(self.posts,1);self.assertEqual(repo.summary(self.c,self.a['user_id'])['reason'],'busy')
        with patch('app.web_qa.RUNTIME',Path(self.tmp.name).parent/'unused'):
            # Reconcile with the production recovery reader using a private temporary root.
            root=Path(self.tmp.name)/'root';(root/'receipts').mkdir(parents=True)
            (root/'receipts'/f"{self.run['run_id']}.json").write_bytes((Path(self.tmp.name)/f"{self.run['run_id']}.json").read_bytes())
            with patch('app.web_qa.RUNTIME',root):self.assertEqual(recover(None,self.run['run_id'])['status'],'completed')
        self.assertEqual(self.posts,1)
    def test_http_execute_is_authenticated_and_replayed_without_new_background_task(self):
        self.allowance();path=self.url+'/answers/'+str(self.run['run_id'])+'/execute'
        with patch('app.api.web_qa.execute_worker') as worker:
            self.assertEqual(self.client.post(path).status_code,401);self.assertEqual(self.client.post(path,headers=self.hb).status_code,404)
            self.assertEqual(self.client.post(path,headers=self.ha).status_code,202);self.assertEqual(self.client.post(path,headers=self.ha).status_code,202);worker.assert_called_once()
        self.assertEqual(self.client.get('/api/v1/agent-chat/generation-budget',headers=self.ha).json()['reason'],'busy')
