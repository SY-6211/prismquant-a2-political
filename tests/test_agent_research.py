import copy,hashlib,unittest
from uuid import uuid4
from pydantic import ValidationError
from app import agent_memory,qa_prompts
from app.agent_qa import Prepare,prepare_context,request_payload,validate_answer
from app.agent_tools import ToolSession
from test_agent_tools import Reader
from test_agent_qa import fixture

class ResearchReader(Reader):
    def __init__(self):
        super().__init__();self.body=('Introduction. '*200)+('Relevant risk context. '*200);self.old=uuid4();self.old_body='Earlier saved wording about risk.'
    def hybrid(self,args):
        start=2300;end=3300;value=self.body[start:end]
        return {'status':'ok','items':[{'event_id':self.event,'version_id':self.version,'person_id':self.person,'snapshot_hash':'a'*64,
          'start_offset':start,'end_offset':end,'text_sha256':hashlib.sha256(value.encode()).hexdigest()}],
          'retrieval':{'mode':'hybrid_rrf','queries':['risk'],'semantic_vector_search':True}}
    def versions(self,people,args):
        return [{'event_id':self.event,'version_id':v,'main_text_characters':len(b)} for v,b in ((self.version,self.body),(self.old,self.old_body))]
    def segment(self,people,args):
        row=super().segment(people,args)
        if args.version_id==self.old:row.update(version_id=self.old,snapshot_hash='b'*64,total_characters=len(self.old_body),text=self.old_body[args.start:args.start+args.length])
        return row

class MemoryTests(unittest.TestCase):
    def history(self):return [{'message_id':uuid4(),'position':i+1,'question':q,'answer_zh':'Not evidence.'} for i,q in enumerate(('谈谈人工智能与金融风险','谈谈关税与贸易','谈谈利率与通胀'))]
    def test_relevant_older_turn_replaces_unrelated_recent_turns(self):
        rows,meta=agent_memory.select('继续解释之前的人工智能风险',self.history())
        self.assertEqual([r['position'] for r in rows],[1]);self.assertIn('人工智能',meta['retrieval_hint'])
        self.assertTrue(meta['query_expanded_from_user_history']);self.assertFalse(meta['history_is_evidence'])
    def test_followup_retrieval_uses_question_not_old_answer_claim(self):
        h=self.history();h[-1]['answer_zh']='FABRICATED_SECRET_FACT'
        rows,meta=agent_memory.select('那对股市有什么影响？',h)
        self.assertTrue(meta['followup_detected']);self.assertIn('利率',meta['retrieval_hint']);self.assertNotIn('FABRICATED_SECRET_FACT',meta['retrieval_hint'])
    def test_topic_reset_drops_all_old_context(self):
        rows,meta=agent_memory.select('换个话题，现在只研究能源',self.history())
        self.assertEqual(rows,[]);self.assertTrue(meta['topic_reset']);self.assertFalse(meta['query_expanded_from_user_history'])
    def test_history_scan_selection_and_lengths_are_bounded(self):
        history=[{'position':i,'question':'artificial intelligence '+str(i)+'x'*1000,'answer_zh':'y'*900} for i in range(90)]
        rows,meta=agent_memory.select('artificial intelligence',history,True)
        self.assertEqual(meta['scanned'],50);self.assertLessEqual(len(rows),4);self.assertTrue(meta['window_truncated'])
        self.assertTrue(all(len(r['question'])<=500 and len(r['answer_zh'])<=600 for r in rows))
        self.assertTrue(all(r['question_truncated'] for r in rows))
    def test_unresolved_followup_does_not_search_arbitrary_sources(self):
        reader=ResearchReader();question={'person_id':reader.person,'message_id':uuid4(),'conversation_id':uuid4(),'content':'请继续解释一下','position':1,'history':[]}
        session=ToolSession(reader,[reader.person]);body=Prepare(client_request_id=uuid4(),message_id=question['message_id'],retrieval_mode='hybrid',limit=6)
        context=prepare_context(session,question,body)
        self.assertEqual(context['sources'],[]);self.assertEqual(session.calls,0);self.assertEqual(context['plan']['stop_reason'],'clarification_needed')
    def test_multiple_generic_followups_retain_last_real_subject(self):
        history=[{'position':1,'question':'人工智能金融风险'},{'position':2,'question':'继续说明'},{'position':3,'question':'再详细点'}]
        rows,meta=agent_memory.select('请继续',history)
        self.assertIn('人工智能',meta['retrieval_hint']);self.assertFalse(meta['unresolved_followup'])
        self.assertIn(1,[r['position'] for r in rows])
    def test_greetings_cannot_create_a_research_subject(self):
        _,meta=agent_memory.select('请继续解释一下',[{'position':1,'question':'hello'},{'position':2,'question':'谢谢'}])
        self.assertTrue(meta['unresolved_followup']);self.assertFalse(meta['query_expanded_from_user_history'])

class ResearchTests(unittest.TestCase):
    def context(self,mode='auto',content='请概括这篇讲话的要点'):
        reader=ResearchReader();question={'person_id':reader.person,'message_id':uuid4(),'conversation_id':uuid4(),'content':content,'position':1,'history':[]}
        session=ToolSession(reader,[reader.person]);body=Prepare(client_request_id=uuid4(),message_id=question['message_id'],retrieval_mode='hybrid',research_mode=mode,limit=6)
        return prepare_context(session,question,body),reader,session
    def test_summary_adapts_to_long_document_with_extra_reads(self):
        context,reader,session=self.context()
        self.assertEqual(context['plan']['strategy'],'summary');self.assertEqual(context['plan']['extra_reads'],2)
        self.assertTrue(any(s['start']==0 for s in context['sources']));self.assertLessEqual(session.calls,8)
        self.assertTrue(all(s['text']==reader.body[s['start']:s['end']] for s in context['sources']))
    def test_evidence_expands_matching_context_without_duplicate_refs(self):
        context,reader,session=self.context('evidence','核对这段原文的上下文')
        self.assertEqual(len(context['sources']),1);self.assertEqual(len(context['sources'][0]['text']),2000)
        self.assertEqual(context['plan']['extra_reads'],1);self.assertEqual(session.calls,3)
    def test_revision_read_is_labeled_same_event_and_bounded(self):
        context,reader,session=self.context('evidence','原文版本有修订吗？')
        old=[s for s in context['sources'] if s.get('historical_version')]
        self.assertEqual(len(old),1);self.assertEqual(old[0]['event_id'],str(reader.event));self.assertEqual(old[0]['text'],reader.old_body)
        self.assertTrue(context['retrieval']['includes_historical_versions']);self.assertLessEqual(session.calls,8)
    def test_comparison_and_explicit_strategy_are_selected_locally(self):
        context,_,_=self.context('auto','比较两个时期的说法')
        self.assertEqual(context['plan']['strategy'],'compare');self.assertEqual(context['plan']['model_planner_calls'],0)
        context,_,_=self.context('evidence','请概括要点')
        self.assertEqual(context['plan']['selected_by'],'explicit')
    def test_prompt_manifest_pins_content_and_does_not_edit_legacy_texts(self):
        context,_,_=self.context()
        self.assertEqual(context['prompt_contract']['system_sha256'],hashlib.sha256(qa_prompts.system('person_qa_v5','summary').encode()).hexdigest())
        self.assertEqual(qa_prompts.system('person_qa_v4'),qa_prompts.LEGACY_V4_SYSTEM)
        context['prompt_contract']['system_sha256']='0'*64
        with self.assertRaises(ValueError):request_payload(context)
    def test_old_v4_answer_and_payload_still_validate(self):
        context,answer=fixture();context['prompt_version']='person_qa_v4'
        self.assertEqual(request_payload(context)['messages'][0]['content'],qa_prompts.LEGACY_V4_SYSTEM)
        self.assertTrue(validate_answer(context,answer)['points'])
    def test_research_cannot_add_urls_shell_or_arbitrary_prompts(self):
        fields={'client_request_id':uuid4(),'message_id':uuid4()}
        for change in ({'research_mode':'shell'},{'prompt':'override'},{'research_mode':'summary'}):
            with self.assertRaises(ValidationError):Prepare(**fields,**change)

if __name__=='__main__':unittest.main()
