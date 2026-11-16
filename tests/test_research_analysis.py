"""Offline research contracts, not a model-quality benchmark."""
import copy, json, unittest
from uuid import uuid4
from pydantic import ValidationError
from app import qa_prompts,agent_plan
from app.agent_qa import (Prepare,prepare_context,request_payload,validate_answer,raw_answer,abstention)
from app.agent_tools import ToolSession
from app.annotation_model import digest
from app.quality_model import audit
from app.core.config import PROJECT_ROOT
from test_agent_qa import fixture
from test_agent_tools import Reader


def research_fixture():
    context, answer = fixture()
    context['prompt_version']=qa_prompts.RESEARCH
    context['research_task']='impact_analysis'
    context['prompt_contract']=qa_prompts.manifest(qa_prompts.RESEARCH,'research')
    context['plan']['strategy']='research'
    answer.update(analysis=[dict(text_zh='若关税实际增加，依赖进口投入的企业可能面临成本压力。',
        premise_points=[1],mechanism_zh='这是一般成本传导假设，不能据此认定任何具体公司已受影响。',
        assumptions_zh=['相关措施实际落实，且企业存在对应进口敞口。'],counterfactors_zh=['企业可能转移采购或提高售价。'])],
        scenarios=[dict(condition_zh='若措施未落实或存在豁免',implication_zh='上述成本压力可能较小或不发生。',analysis_indices=[1])],
        evidence_gaps_zh=['需核对政策正式文本、落地状态与企业成本结构。'],
        conclusion_zh='仅在政策落实且企业有相应敞口时，才有进一步评估成本压力的依据。',conclusion_basis=[1])
    answer.pop('outcome')
    answer['research_steps']=[]
    return context,answer


class ResearchAnalysisTests(unittest.TestCase):
    def test_conditional_inference_not_literal_quote_is_preserved_and_labeled(self):
        context,answer=research_fixture();value=validate_answer(context,answer)
        self.assertIn('条件分析',value['answer_zh']);self.assertIn('反向因素',value['answer_zh'])
        self.assertEqual(value['answer_mode'],'research');self.assertTrue(value['needs_review'])
        self.assertEqual(value['inference_support'],'not_independently_assessed')
        self.assertTrue(value['points'][0]['citations'][0]['valid'])

    def test_missing_or_out_of_range_trace_links_are_rejected(self):
        context,answer=research_fixture()
        for path,value in [('premise_points',[]),('premise_points',[2]),('premise_points',[True]),('premise_points',[1,1])]:
            changed=copy.deepcopy(answer);changed['analysis'][0][path]=value
            with self.assertRaises(ValidationError):validate_answer(context,changed)
        for key in ('analysis','scenarios','conclusion_basis'):
            changed=copy.deepcopy(answer);changed[key]=[]
            with self.assertRaises(ValidationError):validate_answer(context,changed)
        changed=copy.deepcopy(answer);changed['scenarios'][0]['analysis_indices']=[2]
        with self.assertRaises(ValidationError):validate_answer(context,changed)

    def test_fabricated_quote_and_wrong_person_still_rejected(self):
        context,answer=research_fixture();answer['points'][0]['citations'][0]['text']='invented words'
        with self.assertRaises(ValueError):validate_answer(context,answer)
        context,answer=research_fixture();context['sources'][0]['person_id']=str(uuid4())
        with self.assertRaises(ValueError):validate_answer(context,answer)

    def test_missing_assumptions_counterfactors_and_blank_mechanism_rejected(self):
        context,answer=research_fixture()
        for key,value in [('assumptions_zh',[]),('counterfactors_zh',[' ']),('mechanism_zh',' ')]:
            changed=copy.deepcopy(answer);changed['analysis'][0][key]=value
            with self.assertRaises(ValidationError):validate_answer(context,changed)

    def test_no_evidence_abstains_without_model_or_inference(self):
        context,_=research_fixture();context['sources']=[]
        value=abstention(context,'缺少可用原文。')
        self.assertEqual(value['outcome'],'insufficient_evidence');self.assertEqual(value['analysis'],[])

    def test_old_mode_does_not_accept_research_fields(self):
        context,_=fixture();_,answer=research_fixture()
        with self.assertRaises(ValidationError):validate_answer(context,answer)

    def test_request_freezes_research_schema_without_relaxing_legacy_prompt(self):
        context,_=research_fixture();payload=request_payload(context)
        self.assertIn('analysis',payload['response_format']['json_schema']['schema']['properties'])
        self.assertIn('CONDITIONAL INFERENCE',payload['messages'][0]['content'])
        self.assertEqual(payload['max_tokens'],4096);self.assertNotIn('tools',payload)
        old,_=fixture();self.assertNotIn('analysis',request_payload(old)['response_format']['json_schema']['schema']['properties'])
        self.assertIn('Do not infer unstated systemic effects',request_payload(old)['messages'][0]['content'])
        context['prompt_contract']['answer_schema']='person_qa_answer_v1'
        with self.assertRaises(ValueError):request_payload(context)
    def test_legacy_research_prompt_hash_and_output_limit_remain_frozen(self):
        context,answer=research_fixture();context['prompt_version']=qa_prompts.RESEARCH_V1
        answer.pop('research_steps')
        answer['outcome']='answered'
        context['prompt_contract']=qa_prompts.manifest(qa_prompts.RESEARCH_V1,'research')
        self.assertEqual(context['prompt_contract']['system_sha256'],'ccdff38fb9300b14b8e3337c1ee00e1f3eca571f9b41c47ea265238cb507af3b')
        self.assertEqual(request_payload(context)['max_tokens'],1800)
        self.assertEqual(validate_answer(context,answer)['answer_mode'],'research')
    def test_v2_prompt_remains_frozen_while_new_schema_derives_status(self):
        context,answer=research_fixture()
        self.assertNotIn('outcome',request_payload(context)['response_format']['json_schema']['schema']['properties'])
        checked=validate_answer(context,answer);self.assertEqual(checked['outcome'],'answered')
        self.assertEqual(checked['outcome_classified_by'],'host_validated_structure_not_model_label')
        context['prompt_version']=qa_prompts.RESEARCH_V2
        answer.pop('research_steps')
        context['prompt_contract']=qa_prompts.manifest(qa_prompts.RESEARCH_V2,'research')
        self.assertEqual(context['prompt_contract']['system_sha256'],'bb70e5a6613f57c9fbaa131b600943d71685a991a67e99ad90518db72ed45fac')
        answer['outcome']='answered';self.assertTrue(validate_answer(context,answer)['points'])
    def test_new_schema_does_not_accept_a_contradictory_model_outcome_label(self):
        context,answer=research_fixture();answer['outcome']='insufficient_evidence'
        with self.assertRaises(ValidationError):validate_answer(context,answer)
    def test_truncation_has_specific_failure_and_cannot_be_accepted_as_partial_json(self):
        from app.agent_qa import validate_envelope,OutputRejected,MODEL
        context,answer=research_fixture()
        envelope={'model':MODEL,'choices':[{'finish_reason':'length','message':{'content':json.dumps(answer)}}]}
        with self.assertRaises(OutputRejected) as raised:validate_envelope(context,envelope)
        self.assertEqual(raised.exception.code,'qa_output_truncated')
    def test_budget_reservation_includes_larger_output_allowance(self):
        from app.repositories.web_qa import cost_limit
        context,_=research_fixture();new=cost_limit(context)
        context['prompt_version']=qa_prompts.RESEARCH_V1
        context['prompt_contract']=qa_prompts.manifest(qa_prompts.RESEARCH_V1,'research')
        self.assertGreaterEqual(new-cost_limit(context),4592000)

    def test_receipt_reconstruction_and_quality_audit_keep_all_research_fields(self):
        context,answer=research_fixture();enriched=validate_answer(context,answer)
        self.assertEqual(validate_answer(context,raw_answer(context,enriched)),enriched)
        run=dict(context=context,context_hash=digest(context),answer=enriched,status='completed',
                 execution_kind='synthetic_fixture',response_hash=digest(enriched))
        self.assertEqual(audit(run)['state'],'passed')
        run['answer']['analysis'][0]['premise_points']=[9]
        self.assertIn('research_answer_contract',audit(run)['errors'])

    def test_market_questions_select_research_but_explicit_evidence_is_preserved(self):
        for question in ('这段关税表态对汽车行业有什么影响？','分析利率变化对股市的影响','市场风险如何传导？'):
            self.assertEqual(agent_plan.strategy_for(question)[0],'research')
        self.assertEqual(agent_plan.strategy_for('股票会涨吗？','evidence')[0],'evidence')
        self.assertEqual(agent_plan.strategy_for('请核对原文上下文')[0],'evidence')

    def test_prepare_pins_selected_prompt_instead_of_global_default(self):
        reader=Reader();question=dict(conversation_id=uuid4(),message_id=uuid4(),person_id=reader.person,
                                    content='这段言论对股票市场有什么影响？',position=1,history=[])
        body=Prepare(client_request_id=uuid4(),message_id=question['message_id'])
        context=prepare_context(ToolSession(reader,[reader.person]),question,body)
        self.assertEqual(context['prompt_version'],qa_prompts.RESEARCH)
        self.assertEqual(context['prompt_contract']['strategy'],'research')
    def test_fixed_development_questions_have_expected_routes_without_claiming_model_quality(self):
        cases=json.loads((PROJECT_ROOT/'app/data/research_review_cases.json').read_text(encoding='utf8'))
        self.assertIsNone(cases['quality_score']);self.assertEqual(len(cases['cases']),12)
        for case in cases['cases']:
            with self.subTest(case=case['id']):self.assertEqual(agent_plan.strategy_for(case['question'])[0],case['expected_strategy'])
    def test_investment_data_checklist_does_not_switch_to_literal_evidence_skill(self):
        questions=('接着刚才的分析，如果我要研究相关股票，还需要核对哪些公司或市场数据？请说明每类数据有什么用，并区分已有依据和待验证信息。',
                   '做投资研究需要核对哪些数据？','Which company or market data should I verify next for investment research?')
        for question in questions:
            with self.subTest(question=question):self.assertEqual(agent_plan.strategy_for(question)[0],'research')
        for question in ('请核对原文中提到哪些银行数据。','为了投资研究，请核对这句原文是否准确。'):
            self.assertEqual(agent_plan.strategy_for(question)[0],'evidence')
        self.assertEqual(agent_plan.strategy_for(questions[0],'evidence')[0],'evidence')


if __name__=='__main__':unittest.main()
