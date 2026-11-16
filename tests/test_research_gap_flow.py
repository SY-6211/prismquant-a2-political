"""Analysis gaps become bounded source tasks, not inherited facts."""
import unittest
from uuid import uuid4
from app import agent_memory,agent_intent,research_sources
from test_research_sources import context


def history():
    return [dict(position=1,message_id=uuid4(),answer_run_id=uuid4(),question='AI对金融行业有什么影响？',research_steps=[],
                 evidence_gaps_zh=['公司资本支出 CAPEX 与研发投入','美国劳动生产率与单位劳动成本'],answer_zh='x'*800)]


class GapFlowTests(unittest.TestCase):
    def test_analysis_without_a_checklist_can_lookup_its_second_gap(self):
        c=context('帮我补查第二项资料',history())
        self.assertEqual(c['research_task'],'source_lookup')
        self.assertEqual(c['memory']['research_state']['origin'],'analysis_evidence_gaps')
        self.assertEqual(c['source_lookup']['items'][0]['number'],2)
        self.assertEqual(c['source_lookup']['items'][0]['resource_keys'][0],'bls-productivity')
        self.assertFalse(c['sources']);self.assertFalse(c['memory']['history_is_evidence'])
    def test_explicit_gap_button_question_works_without_previous_history(self):
        c=context('请查找以下待核对资料的官方入口：公司资本支出 CAPEX 与研发投入',[])
        self.assertEqual(c['source_lookup']['items'][0]['resource_keys'][0],'sec-filings')
    def test_missing_gap_cannot_fall_back_to_another_item(self):
        c=context('帮我补查第五项资料',history())
        self.assertEqual(c['source_lookup']['stop_reason'],'checklist_item_missing')
        self.assertEqual(research_sources.answer(c)['outcome'],'insufficient_evidence')
    def test_newer_analysis_gaps_replace_an_older_checklist(self):
        h=[dict(position=0,question='还需要哪些公司数据？',research_steps=[dict(data_zh='公司财报')])]+history()
        c=context('帮我补查第二项资料',h)
        self.assertIn('劳动生产率',c['source_lookup']['items'][0]['data_zh'])
    def test_explicit_topic_reset_does_not_borrow_prior_gaps(self):
        c=context('换个话题，通胀数据去哪里找？',history())
        self.assertEqual(c['memory']['research_state']['item_count'],0)
        self.assertEqual(c['source_lookup']['resources'][0]['resource_key'],'bls-cpi')
    def test_risk_followup_after_source_lookup_returns_to_analysis(self):
        h=history()+[dict(position=2,question='这些数据到哪里找？')]
        _,memory=agent_memory.select('那风险呢？',h)
        self.assertEqual(agent_intent.route('那风险呢？',memory=memory)['task'],'impact_analysis')
    def test_exact_quote_requests_still_override_lookup(self):
        _,m=agent_memory.select('请补查原文出处',history())
        self.assertEqual(agent_intent.route('请补查原文出处',memory=m)['task'],'evidence')
    def test_regulatory_gap_has_an_applicable_official_entry_without_claiming_policy_facts(self):
        for name in ('Barr','Lagarde','Trump'):
            c=context('请查找以下待核对资料的官方入口：'+name+'分析中缺少美国金融监管政策资料',[])
            self.assertEqual(c['source_lookup']['items'][0]['resource_keys'][0],'fed-supervision')
            self.assertFalse(c['source_lookup']['resources'][0]['data_verified'])

if __name__=='__main__':unittest.main()
