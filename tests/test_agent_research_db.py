"""Conversation memory and research guards in disposable PostgreSQL schemas."""
import json,os,unittest,tempfile
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
from uuid import UUID,uuid4
from sqlalchemy import text
from app.agent_qa import Prepare,prepare,validate_answer
from app.agent_tools import ToolSession,Read
from app.annotation_model import digest
from app.chat_model import CreateConversation,UserMessage
from app.repositories import chat,qa,web_qa
from app.web_qa import execute_worker
from app.model_trial_live import units
from app.repositories.agent_tools import SnapshotReader
import test_rag_db as fixture

@unittest.skipUnless(os.environ.get('PQ_RESEARCH_DB_TESTS')=='1','Isolated research checks are opt-in')
class ResearchDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixture.RAGDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixture.RAGDatabaseTests.drop_schema.__func__)
    save=fixture.RAGDatabaseTests.save
    build=fixture.RAGDatabaseTests.build
    def setUp(self):fixture.RAGDatabaseTests.setUp(self)
    def ask(self,content):return chat.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],UserMessage(client_request_id=uuid4(),content=content))['message']
    def prepared(self,message,**changes):
        self.build();body=Prepare(client_request_id=uuid4(),message_id=message['message_id'],retrieval_mode='hybrid',limit=6,**changes)
        return prepare(None,self.ta,self.conv['conversation_id'],body)
    def synthetic_answer(self,run):
        source=next(s for s in run['context']['sources'] if 'financial risk' in s['text'])
        value={'outcome':'answered','points':[{'text_zh':'SYNTHETIC_PRIOR_SUMMARY','citations':[{'source_ref':source['source_ref'],'text':'financial risk'}]}],'limitations_zh':['Synthetic fixture only.'],'needs_review':True}
        if run['prompt_version']=='person_qa_research_v4':
            from test_research_analysis import research_fixture
            _,value=research_fixture();value['points']=[{'text_zh':'SYNTHETIC_PRIOR_SUMMARY','citations':[{'source_ref':source['source_ref'],'text':'financial risk'}]}]
        answer=validate_answer(run['context'],value)
        return qa.save_result(self.c,run['run_id'],'completed',answer,digest(answer),{'fixture':True},kind='synthetic_fixture')
    def test_relevant_history_can_be_older_than_two_turns(self):
        earlier=self.ask('讨论人工智能与金融风险');self.ask('讨论关税贸易');self.ask('讨论利率通胀')
        run=self.prepared(self.ask('继续解释之前的人工智能风险'))
        self.assertEqual([h['message_id'] for h in run['context']['prior_conversation']],[str(earlier['message_id'])])
        self.assertTrue(run['context']['memory']['query_expanded_from_user_history']);self.assertTrue(run['context']['sources'])
    def test_no_memory_reads_from_other_owner_or_conversation(self):
        ids=[]
        for owner in (self.a['user_id'],self.b['user_id']):
            conv=chat.create_conversation(self.c,owner,CreateConversation(client_request_id=uuid4(),title='Foreign memory',person_id=self.person))['conversation']
            ids.append(chat.append_user(self.c,owner,conv['conversation_id'],UserMessage(client_request_id=uuid4(),content='PRIVATE_OTHER 人工智能与金融风险'))['message']['message_id'])
        run=self.prepared(self.ask('人工智能风险'))
        self.assertFalse(any(h.get('message_id') in {str(x) for x in ids} for h in run['context']['prior_conversation']))
        self.assertNotIn('PRIVATE_OTHER',json.dumps(run['context']))
    def test_history_disable_skips_history_query_and_keeps_saved_messages(self):
        self.ask('人工智能风险');current=self.ask('人工智能金融风险')
        with patch.object(self.c,'execute',wraps=self.c.execute) as execute:
            value=qa.question(self.c,self.a['user_id'],self.conv['conversation_id'],current['message_id'],False)
        self.assertEqual(value['history'],[])
        self.assertFalse(any('ORDER BY m.position DESC' in str(call.args[0]) for call in execute.call_args_list))
        run=self.prepared(current,use_history=False)
        self.assertFalse(run['context']['memory']['enabled']);self.assertEqual(run['context']['prior_conversation'],[])
        self.assertEqual(chat.conversation(self.c,self.a['user_id'],self.conv['conversation_id'])['message_count'],3)
    def test_explicit_topic_reset_does_not_carry_old_subject(self):
        self.ask('人工智能风险');run=self.prepared(self.ask('换个话题，只讨论利率与通胀'))
        self.assertTrue(run['context']['memory']['topic_reset']);self.assertEqual(run['context']['prior_conversation'],[])
        self.assertNotIn('人工智能',run['context']['memory']['retrieval_hint'])
    def test_scan_window_is_fifty_and_excludes_future_messages(self):
        for i in range(55):self.ask('利率问题 '+str(i))
        current=self.ask('利率概括');self.ask('FUTURE_MESSAGE')
        value=qa.question(self.c,self.a['user_id'],self.conv['conversation_id'],current['message_id'])
        self.assertEqual(len(value['history']),50);self.assertTrue(value['history_window_truncated'])
        self.assertTrue(all(h['position']<current['position'] for h in value['history']))
        self.assertNotIn('FUTURE_MESSAGE',json.dumps(value,default=str))
    def test_heldout_answer_is_not_loaded_as_memory(self):
        prior=self.ask('人工智能金融风险');run=self.prepared(prior);self.synthetic_answer(run)
        dataset=uuid4();self.c.execute(text("INSERT INTO annotation_datasets(dataset_id,name) VALUES(:id,'Reserved memory test')"),{'id':dataset})
        self.c.execute(text("INSERT INTO annotation_samples(sample_id,dataset_id,identity,origin,snapshot,split) VALUES(:id,:d,'heldout','source',CAST(:s AS jsonb),'blind')"),{'id':uuid4(),'d':dataset,'s':json.dumps({'event_version_id':self.target['version_id']})})
        current=self.ask('继续解释这些风险');value=qa.question(self.c,self.a['user_id'],self.conv['conversation_id'],current['message_id'])
        self.assertNotIn('SYNTHETIC_PRIOR_SUMMARY',json.dumps(value,default=str))
    def test_unresolved_followup_is_local_clarification_without_generation(self):
        other=chat.create_conversation(self.c,self.a['user_id'],CreateConversation(client_request_id=uuid4(),title='Empty followup',person_id=self.person))['conversation']
        message=chat.append_user(self.c,self.a['user_id'],other['conversation_id'],UserMessage(client_request_id=uuid4(),content='请继续解释一下'))['message']
        run=prepare(None,self.ta,other['conversation_id'],Prepare(client_request_id=uuid4(),message_id=message['message_id'],retrieval_mode='hybrid',limit=6))
        detail=qa.detail(self.c,self.a['user_id'],other['conversation_id'],run['run_id'])
        self.assertEqual(detail['status'],'abstained');self.assertEqual(detail['receipt']['model_requests'],0)
        self.assertIn('主题',detail['answer']['answer_zh']);self.assertEqual(detail['context']['tool_audit'],[])
    def test_research_read_rejects_pending_quoted_reserved_or_outside_dates(self):
        pending=self.save(self.snap.model_copy(update={'review_status':'needs_review'}),1)
        quoted=self.save(self.snap.model_copy(update={'post_type':'quote'}),2)
        reader=SnapshotReader(self.c)
        for version in (pending,quoted):
            args=Read(event_id=version['event_id'],version_id=version['version_id'],research_only=True)
            self.assertFalse(reader.research_version_eligible([self.person],args))
        args=Read(event_id=self.target['event_id'],version_id=self.target['version_id'],research_only=True,since=datetime(2030,1,1,tzinfo=timezone.utc))
        self.assertFalse(reader.research_version_eligible([self.person],args))
    def test_revision_research_marks_same_event_as_historical_and_keeps_budget(self):
        self.save(self.snap.model_copy(update={'main_text':self.snap.main_text+' Updated.'}),1)
        run=self.prepared(self.ask('核对人工智能风险原文版本有无修订'),research_mode='evidence')
        sources=run['context']['sources'];old=[s for s in sources if s.get('historical_version')]
        self.assertTrue(old);self.assertEqual(old[0]['event_id'],str(self.target['event_id']))
        self.assertLessEqual(len(run['context']['tool_audit']),8)
        self.assertTrue(run['context']['retrieval']['includes_historical_versions'])
    def test_old_v4_request_retry_is_not_reprepared_or_reprompted(self):
        original=self.prepared(self.ask('人工智能风险'));context=dict(original['context']);context['prompt_version']='person_qa_v4'
        body=Prepare(client_request_id=uuid4(),message_id=original['message_id'],retrieval_mode='hybrid',limit=6)
        spec=json.loads(body.model_dump_json(exclude={'client_request_id','research_mode','use_history','read_official_sources'}))
        old=qa.save_prepared(self.c,self.a['user_id'],self.conv['conversation_id'],body.client_request_id,spec,digest(spec),context,'deepseek/deepseek-v3.2','person_qa_v4')
        with patch('app.agent_qa.open_session',side_effect=AssertionError('Must reuse frozen old context')):again=prepare(None,self.ta,self.conv['conversation_id'],body)
        self.assertEqual(old['context_hash'],again['context_hash']);self.assertEqual(again['prompt_version'],'person_qa_v4')
    def test_changed_research_policy_under_same_request_id_conflicts(self):
        message=self.ask('人工智能风险');self.build()
        body=Prepare(client_request_id=uuid4(),message_id=message['message_id'],retrieval_mode='hybrid',limit=6)
        prepare(None,self.ta,self.conv['conversation_id'],body)
        for change in ({'research_mode':'summary'},{'use_history':False}):
            with self.assertRaises(chat.Conflict):prepare(None,self.ta,self.conv['conversation_id'],body.model_copy(update=change))

    def test_prompt_contract_failure_finishes_not_sent_without_key_or_client(self):
        run=self.prepared(self.ask('人工智能风险'))
        web_qa.grant(self.c,self.a['user_id'],'synthetic-manifest-fail',units('.1'),2,1)
        web_qa.enqueue(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])
        def service(settings,fn,*args):return fn(self.c,*args)
        with tempfile.TemporaryDirectory() as folder,patch('app.web_qa.service',side_effect=service),patch('app.web_qa.request_payload',side_effect=ValueError('manifest changed')),patch('app.web_qa.private_key',side_effect=AssertionError('No key read')):
            execute_worker(None,self.ta,self.conv['conversation_id'],run['run_id'],client_factory=lambda:self.fail('No client'),receipt_dir=Path(folder))
        detail=qa.detail(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])
        self.assertEqual(detail['status'],'failed');self.assertEqual(detail['receipt']['model_requests'],0)
        self.assertTrue(web_qa.summary(self.c,self.a['user_id'])['can_generate'])

if __name__=='__main__':unittest.main()
