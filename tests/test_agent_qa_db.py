"""Private QA synthetic cases, confined to a disposable PostgreSQL schema."""
from contextlib import contextmanager
import copy,os,unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from unittest.mock import patch
from uuid import UUID,uuid4
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.agent_qa import MODEL,PROMPT_VERSION,Prepare,prepare,validate_answer
from app.agent_tools import ToolSession
from app.annotation_model import digest
from app.chat_model import CreateConversation,EditConversation,UserMessage,new_token
from app.event_model import snapshot_from_record
from app.repositories import chat,qa
from app.repositories.agent_tools import SnapshotReader
from app.repositories.events import save_snapshot
import test_chat_db as fixtures
from test_event_model import record
from test_agent_qa import fixture


@unittest.skipUnless(os.environ.get('PQ_QA_DB_TESTS')=='1','Isolated QA DB tests are opt-in')
class QADatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.ChatDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.ChatDatabaseTests.drop_schema.__func__)

    def setUp(self):
        fixtures.ChatDatabaseTests.setUp(self)
        self.person=uuid4();source=uuid4()
        self.c.execute(text("INSERT INTO people(person_id,name_original) VALUES (:id,'Synthetic QA person')"),{'id':self.person})
        self.c.execute(text("INSERT INTO sources(source_id,name,publisher_name,provenance_type,feed_url) VALUES (:id,'QA source','Synthetic','third_party_archive','https://example.invalid/qa')"),{'id':source})
        self.c.execute(text('UPDATE agent_chat_conversations SET person_id=:person WHERE conversation_id=:id'),{'person':self.person,'id':self.conv['conversation_id']})
        self.version=save_snapshot(self.c,source,'rss',snapshot_from_record(record(person_id=self.person,body_text='Alpha 😀政策 tariffs are discussed.',original_url='https://truthsocial.com/@qa/'+str(uuid4().int))),datetime.now(timezone.utc))
        self.message=chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='请解释政策原文。'))['message']
        self.spec=Prepare(client_request_id=uuid4(),message_id=self.message['message_id'],event_id=UUID(self.version['event_id']),version_id=UUID(self.version['version_id']))
        @contextmanager
        def session(settings,people):yield ToolSession(SnapshotReader(self.c),people)
        self.session_patch=patch('app.agent_qa.open_session',side_effect=session);self.session_patch.start();self.addCleanup(self.session_patch.stop)

    def prepared(self,body=None):return prepare(None,self.ta,self.conv['conversation_id'],body or self.spec)

    def result(self,run):
        value={'outcome':'answered','points':[{'text_zh':'据归档，作者谈及政策。','citations':[{'source_ref':run['context']['sources'][0]['source_ref'],'text':'政策 tariffs'}]}],
            'limitations_zh':['第三方归档，未独立核实。'],'needs_review':True}
        answer=validate_answer(run['context'],value)
        return qa.save_result(self.c,run['run_id'],'completed',answer,digest(answer),{'fixture':True},kind='synthetic_fixture')

    def test_preparation_freezes_inputs_and_is_idempotent_without_model(self):
        run=self.prepared();again=self.prepared();self.assertEqual(run['run_id'],again['run_id'])
        self.assertEqual(run['context_hash'],again['context_hash']);self.assertFalse(run['context']['retrieval']['semantic_vector_search'])
        bad=self.spec.model_copy(update={'limit':2})
        with self.assertRaises(chat.Conflict):self.prepared(bad)
        self.assertEqual(qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])['status'],'prepared')

    def test_official_source_navigation_is_saved_without_generation_and_is_owner_scoped(self):
        message=chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='公司资本支出数据到哪里找？'))['message']
        body=Prepare(client_request_id=uuid4(),message_id=message['message_id'],retrieval_mode='hybrid')
        run=self.prepared(body);same=self.prepared(body)
        self.assertEqual(run['run_id'],same['run_id'])
        result=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])
        self.assertEqual(result['status'],'completed');self.assertEqual(result['execution_kind'],'local_source_navigation')
        self.assertEqual(result['receipt']['model_requests'],0);self.assertEqual(result['answer']['answer_mode'],'source_navigation')
        with self.assertRaises(chat.Missing):qa.detail(self.c,self.b['user_id'],self.conv['conversation_id'],run['run_id'])
        with self.assertRaises(chat.Conflict):qa.claim(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'],'a'*64)
        next_message=chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='第一项去哪里找？'))['message']
        following=self.prepared(Prepare(client_request_id=uuid4(),message_id=next_message['message_id'],retrieval_mode='hybrid'))
        self.assertEqual(following['context']['memory']['research_state']['item_count'],1)
        self.assertEqual(following['context']['source_lookup']['items'][0]['number'],1)

    def test_saved_analysis_gap_flows_into_a_free_owner_scoped_followup(self):
        run=self.prepared()
        value={'outcome':'answered','points':[{'text_zh':'归档讨论政策。','citations':[{'source_ref':run['context']['sources'][0]['source_ref'],'text':'政策 tariffs'}]}],
               'limitations_zh':['隔离数据库合成样本。'],'needs_review':True}
        answer=validate_answer(run['context'],value)
        answer['evidence_gaps_zh']=['公司资本支出 CAPEX 与研发投入','美国劳动生产率与成本']
        qa.save_result(self.c,run['run_id'],'completed',answer,digest(answer),{'model_requests':0},kind='synthetic_fixture')
        message=chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='帮我补查第二项资料'))['message']
        follow=self.prepared(Prepare(client_request_id=uuid4(),message_id=message['message_id'],retrieval_mode='hybrid'))
        result=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],follow['run_id'])
        self.assertEqual(result['status'],'completed');self.assertEqual(result['receipt']['model_requests'],0)
        self.assertEqual(result['context']['memory']['research_state']['from_answer_run_id'],str(run['run_id']))
        self.assertIn('劳动生产率',result['answer']['source_items'][0]['data_zh'])
        with self.assertRaises(chat.Missing):qa.detail(self.c,self.b['user_id'],self.conv['conversation_id'],follow['run_id'])

    def test_no_sources_produces_honest_local_abstention_without_execution(self):
        run=self.prepared(Prepare(client_request_id=uuid4(),message_id=self.message['message_id'],query='no_such_saved_text'))
        value=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])
        self.assertEqual(value['status'],'abstained');self.assertEqual(value['execution_kind'],'local_abstention')
        self.assertEqual(value['receipt']['model_requests'],0);self.assertEqual(value['answer']['points'],[])
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM agent_qa_executions WHERE run_id=:id'),{'id':run['run_id']}).scalar_one(),0)

    def test_other_user_cannot_prepare_read_list_or_claim(self):
        run=self.prepared();self.result(run);url=self.url+'/answers'
        for method,path,body in [('GET',url,None),('GET',url+'/'+str(run['run_id']),None),('POST',url,self.spec.model_dump(mode='json'))]:
            self.assertEqual(self.client.request(method,path,headers=self.hb,json=body).status_code,404)
        with self.assertRaises(chat.Missing):qa.claim(self.c,self.b['user_id'],self.conv['conversation_id'],run['run_id'],'a'*64)

    def test_history_reanswers_keep_old_results_and_citations(self):
        one=self.prepared();result=self.result(one)
        two=self.prepared(self.spec.model_copy(update={'client_request_id':uuid4()}));self.result(two)
        page=qa.listing(self.c,self.a['user_id'],self.conv['conversation_id'],limit=1)
        rest=qa.listing(self.c,self.a['user_id'],self.conv['conversation_id'],limit=1,cursor=page[0]['run_id'])
        self.assertEqual(len(rest),1);self.assertNotEqual(page[0]['run_id'],rest[0]['run_id'])
        detail=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],one['run_id'])
        self.assertEqual(detail['result_id'],result['result_id']);self.assertEqual(detail['answer']['points'][0]['citations'][0]['version_id'],self.version['version_id'])

    def test_conversation_search_includes_answers_and_prior_turns_are_not_evidence(self):
        run=self.prepared();self.result(run)
        self.assertEqual(len(chat.list_conversations(self.c,self.a['user_id'],query='据归档')),1)
        self.assertEqual(chat.list_conversations(self.c,self.b['user_id'],query='据归档'),[])
        message=chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='上一条说了什么？'))['message']
        value=self.prepared(self.spec.model_copy(update={'client_request_id':uuid4(),'message_id':message['message_id']}))
        self.assertEqual(value['context']['prior_conversation'][0]['answer_zh'],'据归档，作者谈及政策。')
        self.assertFalse(value['context']['history_is_evidence'])

    def test_archive_revocation_foreign_message_and_foreign_cursor_rejected(self):
        run=self.prepared()
        other=chat.create_conversation(self.c,self.a['user_id'],CreateConversation(client_request_id=uuid4(),title='another',person_id=self.person))['conversation']
        with self.assertRaises(chat.Missing):qa.detail(self.c,self.a['user_id'],other['conversation_id'],run['run_id'])
        with self.assertRaises(chat.Missing):qa.listing(self.c,self.a['user_id'],other['conversation_id'],cursor=run['run_id'])
        with self.assertRaises(chat.Missing):qa.question(self.c,self.a['user_id'],other['conversation_id'],self.message['message_id'])
        chat.edit_conversation(self.c,self.a['user_id'],self.conv['conversation_id'],EditConversation(expected_revision=2,archived=True))
        with self.assertRaises(chat.Conflict):qa.claim(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'],'b'*64)
        chat.revoke_token(self.c,self.issued_a['token_id'])
        self.assertEqual(self.client.get(self.url+'/answers',headers=self.ha).status_code,401)

    def test_executions_and_results_are_append_only_and_repeat_claim_fenced(self):
        run=self.prepared();qa.claim(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'],'a'*64)
        self.assertEqual(qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])['status'],'executing_or_unfinished')
        with self.assertRaises(chat.Conflict):qa.claim(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'],'a'*64)
        self.result(run)
        for table in ('agent_qa_runs','agent_qa_executions','agent_qa_results'):
            with self.assertRaises(DBAPIError),self.c.begin_nested():self.c.execute(text('DELETE FROM '+table+' WHERE run_id=:id'),{'id':run['run_id']})
        with self.assertRaises(chat.Conflict):qa.save_result(self.c,run['run_id'],'failed',None,'b'*64,{},failure='changed')

    def test_missing_person_scope_and_cross_conversation_question_fail(self):
        conv=chat.create_conversation(self.c,self.a['user_id'],CreateConversation(client_request_id=uuid4(),title='unscoped'))['conversation']
        with self.assertRaises(chat.Invalid):qa.question(self.c,self.a['user_id'],conv['conversation_id'],self.message['message_id'])
        wrong=self.spec.model_copy(update={'message_id':uuid4(),'client_request_id':uuid4()})
        with self.assertRaises(chat.Missing):self.prepared(wrong)

    def test_reserved_blind_source_excluded_from_model_context(self):
        from app.repositories import annotations
        dataset=annotations.create(self.c,'Synthetic QA holdout')['dataset_id']
        sample=annotations.add(self.c,dataset,version_id=UUID(self.version['version_id']))['sample_id']
        self.c.execute(text("UPDATE annotation_samples SET split='blind' WHERE sample_id=:id"),{'id':sample})
        run=self.prepared();self.assertEqual(run['context']['sources'],[])
        self.assertEqual(qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])['status'],'abstained')

    def test_http_prepare_history_detail_are_private_and_never_generate(self):
        response=self.client.post(self.url+'/answers',headers=self.ha,json=self.spec.model_dump(mode='json'))
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['status'],'prepared')
        self.assertEqual(response.headers['cache-control'],'no-store')
        run=self.prepared();self.result(run)
        self.assertEqual(self.client.get(self.url+'/answers',headers=self.ha).json()['items'][0]['status'],'completed')
        detail=self.client.get(self.url+'/answers/'+str(run['run_id']),headers=self.ha)
        self.assertEqual(detail.json()['execution_kind'],'synthetic_fixture')
        self.assertEqual(self.client.post(self.url+'/answers/'+str(run['run_id'])+'/execute',headers=self.ha,json={}).status_code,409)

    def test_concurrent_claim_has_one_winner_across_independent_transactions(self):
        context,_=fixture()
        with self.engine.begin() as c:
            person=uuid4();c.execute(text("INSERT INTO people(person_id,name_original) VALUES (:id,'Synthetic concurrent QA')"),{'id':person})
            user=chat.create_user(c,'Synthetic concurrent QA');token=new_token();chat.issue_token(c,user['user_id'],token,1)
            conv=chat.create_conversation(c,user['user_id'],CreateConversation(client_request_id=uuid4(),title='race',person_id=person))['conversation']
            message=chat.append_user(c,user['user_id'],conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='Synthetic question'))['message']
            context.update(person_id=str(person),conversation_id=str(conv['conversation_id']),message_id=str(message['message_id']))
            for source in context['sources']:source['person_id']=str(person)
            spec={'message_id':str(message['message_id'])}
            run=qa.save_prepared(c,user['user_id'],conv['conversation_id'],uuid4(),spec,digest(spec),context,MODEL,PROMPT_VERSION)
        def claim_once(_):
            try:
                with self.engine.begin() as c:qa.claim(c,chat.authenticate(c,token)['user_id'],conv['conversation_id'],run['run_id'],'a'*64)
                return True
            except chat.Conflict:return False
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(claim_once,[1,2])),1)
