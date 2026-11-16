"""Source-finding task, preserved checklist and real transport boundaries. Never call paid models."""
import copy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import httpx

from app import agent_memory,agent_intent,qa_prompts,research_sources as nav
from app.agent_qa import Prepare,prepare_context,validate_answer,raw_answer,request_payload
from app.agent_tools import ToolSession
from app.annotation_model import digest
from app.quality_model import audit
from app.web_http import Reader,FetchError
from test_agent_research import ResearchReader

HTML='<html><head><title>Official Data Documentation</title></head><body><main><h1>Official Data Documentation</h1><p>'+('Public documentation about financial statements and survey definitions. '*12)+'</p></main></body></html>'


def history():
    return [dict(position=1,message_id=uuid4(),question='人工智能对金融行业有什么影响？'),
            dict(position=2,message_id=uuid4(),answer_run_id=uuid4(),question='还需要哪些公司或市场数据？',answer_zh='x'*800,
                 research_steps=[dict(data_zh='公司资本支出 CAPEX 与研发投入',purpose_zh='用于验证资本深化假设。',how_to_check_zh='对照期间。'),
                                 dict(data_zh='行业劳动生产率与成本',purpose_zh='研究效率。',how_to_check_zh='核对口径。'),
                                 dict(data_zh='劳动市场工资与岗位数据',purpose_zh='研究岗位变化。',how_to_check_zh='核对地区。')])]


def context(question='这些数据到哪里找？',rows=None,enabled=False):
    reader=ResearchReader();q=dict(person_id=reader.person,message_id=uuid4(),conversation_id=uuid4(),content=question,position=4,history=history() if rows is None else rows)
    body=Prepare(client_request_id=uuid4(),message_id=q['message_id'],retrieval_mode='hybrid',limit=6,read_official_sources=enabled)
    return prepare_context(ToolSession(reader,[reader.person]),q,body)


class NavigationTests(unittest.TestCase):
    def test_find_sources_is_distinct_from_collect_data_and_literal_quote(self):
        h=history()
        for question,task in [('这些数据到哪里找？','source_lookup'),('第二项去哪里找？','source_lookup'),('还需要哪些数据？','method_checklist'),('核对原文出处','evidence')]:
            _,m=agent_memory.select(question,h)
            self.assertEqual(agent_intent.route(question,memory=m)['task'],task)
        self.assertEqual(agent_intent.route('公司财报到哪里找？','evidence')['task'],'evidence')
    def test_complete_checklist_survives_narrative_truncation_and_second_item_is_exact(self):
        c=context('第二项去哪里找？')
        self.assertEqual(c['memory']['research_state']['selection'],'checklist_item_2')
        self.assertEqual([i['number'] for i in c['source_lookup']['items']],[2])
        self.assertEqual(c['source_lookup']['resources'][0]['resource_key'],'bls-productivity')
        self.assertNotIn('sec-filings',[r['resource_key'] for r in c['source_lookup']['resources']])
    def test_missing_item_does_not_fall_back_to_an_unrelated_source(self):
        c=context('第六项去哪里找？');a=nav.answer(c)
        self.assertEqual(c['source_lookup']['stop_reason'],'checklist_item_missing');self.assertFalse(a['source_resources'])
    def test_original_checklist_numbers_survive_a_single_item_navigation_turn(self):
        h=history();c=context('第二项去哪里找？',h)
        h.append(dict(position=3,message_id=uuid4(),question='第二项去哪里找？',research_steps=nav.answer(c)['research_steps']))
        later=context('第一项去哪里找？',h)
        self.assertEqual(later['source_lookup']['items'][0]['number'],1)
        self.assertIn('CAPEX',later['source_lookup']['items'][0]['data_zh'])
    def test_new_topic_or_disabled_history_cannot_borrow_previous_checklist(self):
        c=context('换个话题，通胀数据去哪里找？')
        self.assertEqual(c['memory']['research_state']['item_count'],0);self.assertEqual([s['resource_key'] for s in c['source_lookup']['resources']],['bls-cpi'])
        rows,m=agent_memory.select('这些数据到哪里找？',[]);m['enabled']=False
        self.assertFalse(nav.state('这些数据到哪里找？',history(),m)['items'])
    def test_default_is_no_network_and_zero_model_calls(self):
        with patch('httpx.Client',side_effect=AssertionError('No implicit network')):
            c=context();a=nav.answer(c)
        self.assertEqual(c['source_lookup']['network_requests'],0)
        self.assertTrue(all(s['status']=='suggested' for s in a['source_resources']))
        with self.assertRaises(ValueError):request_payload(c)
        self.assertNotIn(c['prompt_version'],qa_prompts.EXECUTABLE)
    def test_unknown_data_does_not_get_a_generic_irrelevant_entry(self):
        c=context('北极海冰数据去哪里下载？',[])
        self.assertFalse(c['source_lookup']['resources']);self.assertEqual(nav.answer(c)['outcome'],'insufficient_evidence')
    def test_company_financial_ratios_prioritize_company_filings_over_macro_statistics(self):
        lookup=nav.prepare('公司营业利润率及员工薪酬占收入比例（劳动收入份额）数据到哪里找？',{})
        self.assertEqual(lookup['items'][0]['resource_keys'][:2],['sec-filings','sec-api'])
    def test_frozen_answer_urls_status_and_hashes_are_validated(self):
        c=context();a=nav.answer(c)
        self.assertEqual(validate_answer(c,raw_answer(c,a)),a)
        bad=copy.deepcopy(a);bad['source_resources'][0]['url']='https://evil.invalid'
        with self.assertRaises(ValueError):validate_answer(c,bad)
        bad=copy.deepcopy(c);bad['source_lookup']['resources'][0]['data_verified']=True
        with self.assertRaises(ValueError):validate_answer(bad,nav.answer(bad))
        report=audit(dict(context=c,context_hash=digest(c),answer=a,status='completed',execution_kind='local_source_navigation',response_hash=digest(a)))
        self.assertEqual(report['state'],'passed');self.assertEqual(report['semantic_support'],'not_assessed')
    def test_former_prompt_hashes_are_unchanged(self):
        for version,expected in ((qa_prompts.RESEARCH_V1,'ccdff38fb9300b14b8e3337c1ee00e1f3eca571f9b41c47ea265238cb507af3b'),(qa_prompts.RESEARCH_V2,'bb70e5a6613f57c9fbaa131b600943d71685a991a67e99ad90518db72ed45fac'),(qa_prompts.RESEARCH_V3,'c6e7ddd9100f37198873c59b50ef20b410d30838e8e1250527f6b5277a0a3de2')):
            self.assertEqual(qa_prompts.manifest(version,'research')['system_sha256'],expected)


class NavigationTransportTests(unittest.TestCase):
    def factory(self,handler):
        def make(spec,**kw):
            kw.pop('cache_dir',None)
            client=httpx.Client(transport=httpx.MockTransport(handler));self.addCleanup(client.close)
            return Reader(spec,client=client,resolver=lambda _:None,sleeper=lambda _:None,**kw)
        return make
    def test_actual_transport_and_extraction_keep_background_separate_from_person(self):
        calls=[]
        def handler(r):
            calls.append(str(r.url));return httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(200,headers={'Content-Type':'text/html'},text=HTML)
        lookup=nav.prepare('公司资本支出数据去哪里找？',{},enabled=True,reader_factory=self.factory(handler))
        self.assertTrue(any(s['status']=='read' for s in lookup['resources']))
        for s in lookup['resources']:
            self.assertEqual(s['role'],'background_navigation_not_person_statement');self.assertFalse(s['data_verified'])
            self.assertEqual(s['text_sha256'],hashlib.sha256(s['text'].encode()).hexdigest())
        self.assertFalse(lookup['company_data_fetched']);self.assertLessEqual(lookup['reads'],3)
    def test_robots_403_redirect_and_unavailable_sites_are_honest_without_retry(self):
        for case in ('robots','403','redirect','offline'):
            calls=[]
            def handler(r):
                calls.append(str(r.url))
                if r.url.path=='/robots.txt':return httpx.Response(200,headers={'Content-Type':'text/plain'},text='User-agent: *\nDisallow: /') if case=='robots' else httpx.Response(404)
                if case=='offline':raise httpx.ConnectError('private transport detail')
                return httpx.Response(403) if case=='403' else httpx.Response(302,headers={'Location':'https://127.0.0.1/private'})
            lookup=nav.prepare('工资数据去哪里找？',{},enabled=True,reader_factory=self.factory(handler))
            self.assertTrue(all(s['status']=='unavailable' for s in lookup['resources']));self.assertLessEqual(len(calls),2)
            self.assertNotIn('private transport detail',json.dumps(lookup))
    def test_page_read_ceiling_keeps_remaining_candidates_as_unread(self):
        _,m=agent_memory.select('这些数据到哪里找？',history());m['enabled']=True
        def handler(r):return httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(200,headers={'Content-Type':'text/html'},text=HTML)
        lookup=nav.prepare('这些数据到哪里找？',m,enabled=True,reader_factory=self.factory(handler))
        self.assertEqual(lookup['reads'],3);self.assertTrue(any(s['status']=='suggested' for s in lookup['resources']))
    def test_input_and_page_cannot_change_allowlist(self):
        for resource in nav.RESOURCES:
            for url in ('http://'+resource.host+'/',resource.url_value+'?key=x','https://127.0.0.1/',resource.url_value+'#x'):
                with self.assertRaises(ValueError):resource.url(url)
    def test_deadline_stops_new_page_reads(self):
        times=iter([0,0,46,46,46,46,46,46,46,46])
        lookup=nav.prepare('公司资本支出数据去哪里找？',{},enabled=True,clock=lambda:next(times,46),reader_factory=lambda *_a,**_k:(_ for _ in ()).throw(AssertionError('deadline should prevent read')))
        self.assertFalse(any(s['status']=='read' for s in lookup['resources']))


if __name__=='__main__':unittest.main()
