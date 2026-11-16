"""Offline regression for intent inheritance, methods, expanded evidence and frozen history."""
import copy,hashlib,json,unittest
from uuid import uuid4
from pydantic import ValidationError
from app import agent_memory,agent_intent,qa_prompts
from app.agent_qa import Prepare,prepare_context,validate_answer,raw_answer,request_payload
from app.agent_tools import ToolSession
from app.source_context import align
from app.annotation_model import digest
from app.quality_model import audit
from app.core.config import PROJECT_ROOT
from test_research_analysis import research_fixture
from test_agent_research import ResearchReader


def method_fixture(with_sources=False):
    context,answer=research_fixture();context['research_task']='method_checklist'
    context['prompt_contract']=qa_prompts.manifest(qa_prompts.RESEARCH,'research','method_checklist')
    answer.update(research_steps=[dict(data_zh='企业AI投入与运营效率数据',purpose_zh='用于检验效率改善假设，而非认定其已经发生。',
                                     how_to_check_zh='后续核对企业正式披露的投入、成本口径与可比期间。',basis_type='general_method',premise_points=[])])
    if not with_sources:
        context['sources']=[];answer.update(points=[],analysis=[],scenarios=[],conclusion_basis=[])
    return context,answer


class IntentWorkflowTests(unittest.TestCase):
    def test_fixed_workflow_development_cases(self):
        data=json.loads((PROJECT_ROOT/'app/data/research_workflow_cases.json').read_text(encoding='utf8'))
        self.assertIsNone(data['quality_score'])
        for case in data['cases']:
            _,memory=self.selected(case['question'],case['history'])
            selected=agent_intent.route(case['question'],case['requested'],memory)
            with self.subTest(case=case['id']):self.assertEqual((selected['strategy'],selected['task']),(case['strategy'],case['task']))
    def selected(self,question,history):
        _,memory=agent_memory.select(question,[dict(position=i+1,question=q) for i,q in enumerate(history)])
        return agent_intent.route(question,memory=memory),memory
    def test_short_followups_inherit_impact_then_method_task(self):
        history=['人工智能对金融行业有什么影响？']
        for question,task in [('那风险呢？','impact_analysis'),('还需要哪些数据？','method_checklist'),('这些数据到哪里找？','source_lookup'),('请继续','source_lookup')]:
            route,_=self.selected(question,history);self.assertEqual((route['strategy'],route['task']),('research',task));history.append(question)
    def test_explicit_fact_check_and_topic_reset_do_not_inherit_research(self):
        history=['人工智能对金融行业有什么影响？']
        route,_=self.selected('请核对原文中的准确句子',history);self.assertEqual(route['strategy'],'evidence')
        route,memory=self.selected('换个话题，请概括利率讲话',history);self.assertEqual(route['strategy'],'summary');self.assertIsNone(memory['routing_seed'])
        route,_=self.selected('请继续',history+['换个话题，请概括利率讲话']);self.assertEqual(route['strategy'],'summary')
    def test_no_history_or_disabled_history_does_not_invent_subject(self):
        route,memory=self.selected('那风险呢？',[]);self.assertNotEqual(route['strategy'],'research')
        self.assertFalse(memory['query_expanded_from_user_history'])
    def test_prior_model_answer_is_not_used_to_choose_intent(self):
        history=[dict(question='请概括利率讲话',answer_zh='研究股票需要哪些数据？',position=1)]
        _,memory=agent_memory.select('请继续',history)
        self.assertEqual(agent_intent.route('请继续',memory=memory)['strategy'],'summary')
        self.assertNotIn('研究股票',str(memory['routing_seed']))


class ExpandedContextTests(unittest.TestCase):
    def test_research_reads_neighbors_and_preserves_unicode_offsets_and_hash(self):
        reader=ResearchReader();session=ToolSession(reader,[reader.person]);question=dict(person_id=reader.person,message_id=uuid4(),conversation_id=uuid4(),content='AI对金融行业有什么影响？',position=1,history=[])
        body=Prepare(client_request_id=uuid4(),message_id=question['message_id'],retrieval_mode='hybrid',research_mode='research',limit=6)
        context=prepare_context(session,question,body);source=context['sources'][0]
        self.assertLess(source['start'],2300);self.assertGreater(source['end'],3300)
        self.assertEqual(source['text'],reader.body[source['start']:source['end']]);self.assertEqual(source['text_sha256'],hashlib.sha256(source['text'].encode()).hexdigest())
        self.assertEqual(context['plan']['extra_reads'],1);self.assertLessEqual(session.calls,8)
        self.assertIn('expand_research_context',[s['purpose'] for s in context['plan']['steps']])
    def test_alignment_keeps_match_and_does_not_start_in_partial_word(self):
        text='clippedword.\nIf AI access is limited, large firms may benefit.\nOther scenarios differ. trailingpart'
        start=100;source=dict(text=text,start=start,end=start+len(text),total_characters=9999)
        anchor=dict(start=start+text.index('arge'),end=start+text.index('Other'))
        value=align(source,anchor)
        self.assertTrue(value['text'].startswith('If AI'))
        self.assertLessEqual(value['start'],anchor['start']);self.assertGreaterEqual(value['end'],anchor['end'])
        self.assertEqual(value['text'],text[value['start']-start:value['end']-start])
    def test_research_reserves_tool_budget_for_two_expansions(self):
        reader=ResearchReader();one=reader.hybrid(None);reader.hybrid=lambda args:{**one,'items':one['items']*6}
        session=ToolSession(reader,[reader.person]);question=dict(person_id=reader.person,message_id=uuid4(),conversation_id=uuid4(),content='金融行业影响',position=1,history=[])
        body=Prepare(client_request_id=uuid4(),message_id=question['message_id'],retrieval_mode='hybrid',research_mode='research',limit=6)
        context=prepare_context(session,question,body)
        self.assertLessEqual(session.calls,8);self.assertEqual(context['plan']['omitted_primary_candidates'],1)
        self.assertEqual(context['plan']['extra_reads'],2)


class ResearchMethodsTests(unittest.TestCase):
    def test_new_quotes_cannot_start_inside_an_english_word(self):
        context,answer=research_fixture();answer['points'][0]['citations'][0]['text']='ariffs'
        with self.assertRaises(ValueError):validate_answer(context,answer)
    def test_general_method_is_allowed_without_fabricated_speaker_quotes(self):
        context,answer=method_fixture();value=validate_answer(context,answer)
        self.assertEqual(value['outcome'],'answered');self.assertFalse(value['points']);self.assertTrue(value['research_steps'])
        self.assertIn('一般研究方法',value['answer_zh']);self.assertTrue(value['needs_review'])
        self.assertEqual(validate_answer(context,raw_answer(context,value)),value)
        self.assertEqual(audit(dict(context=context,context_hash=digest(context),answer=value,status='completed',execution_kind='synthetic_fixture',response_hash=digest(value)))['state'],'passed')
    def test_impact_cannot_use_methods_as_substitute_for_missing_evidence(self):
        context,answer=method_fixture();context['research_task']='impact_analysis';context['prompt_contract']=qa_prompts.manifest(qa_prompts.RESEARCH,'research')
        with self.assertRaises(ValueError):validate_answer(context,answer)
    def test_method_source_links_must_point_to_actual_cited_facts(self):
        context,answer=method_fixture()
        answer['research_steps'][0].update(basis_type='source_linked',premise_points=[1])
        with self.assertRaises(ValidationError):validate_answer(context,answer)
        context,answer=method_fixture(True);answer['research_steps'][0].update(basis_type='source_linked',premise_points=[1])
        value=validate_answer(context,answer)
        self.assertEqual(value['research_steps'][0]['evidence_citations'],value['points'][0]['citations'])
        self.assertEqual(value['analysis'][0]['evidence_citations'],value['points'][0]['citations'])
        self.assertEqual(validate_answer(context,raw_answer(context,value)),value)
    def test_method_task_manifest_cannot_be_substituted(self):
        context,_=method_fixture();context['research_task']='impact_analysis'
        with self.assertRaises(ValueError):request_payload(context)
    def test_legacy_v3_prompt_schema_and_hash_remain_unchanged(self):
        context,answer=research_fixture();context['prompt_version']=qa_prompts.RESEARCH_V3;context['prompt_contract']=qa_prompts.manifest(qa_prompts.RESEARCH_V3,'research');answer.pop('research_steps')
        self.assertEqual(context['prompt_contract']['system_sha256'],'c6e7ddd9100f37198873c59b50ef20b410d30838e8e1250527f6b5277a0a3de2')
        self.assertNotIn('research_steps',request_payload(context)['response_format']['json_schema']['schema']['properties'])
        self.assertTrue(validate_answer(context,answer)['points'])


if __name__=='__main__':unittest.main()
