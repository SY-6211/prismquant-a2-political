"""Dual-source workflow acceptance with synthetic evidence, not a model benchmark."""
import copy,hashlib,json,unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from uuid import uuid4
from app import research_synthesis as syn,research_sources as nav,qa_prompts,agent_memory,agent_intent
from app.agent_qa import Prepare,prepare_context,request_payload,validate_answer,raw_answer,abstention,validate_envelope
from app.agent_tools import ToolSession,encoded
from app.annotation_model import digest
from app.quality_model import audit
from test_agent_qa import fixture,envelope
from test_agent_research import ResearchReader

TEXT='Banks report using artificial intelligence for fraud detection. Adoption may introduce model risk and third-party concentration. These are reported examples, not company investment totals.'

def resource(key='fed-bank-ai-testimony'):
    return dict(resource_key=key,name=nav.BY_KEY[key].name,url=nav.BY_KEY[key].url_value,status='read',content_kind='official_background_document',
        text=TEXT,text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),html_sha256='a'*64,body_sha256='b'*64,
        text_start=120,text_end=120+len(TEXT),read_at=datetime.now(timezone.utc).isoformat(),source_date='2026-03-26',jurisdiction='US')

def synthesis_fixture(task='impact_analysis'):
    c,old=fixture();c.update(prompt_version=qa_prompts.SYNTHESIS_V1,research_task=task,background_sources=[syn.canonical(resource())],
        research_workflow={'steps':[],'web_read_limit':2,'model_generation_limit':1})
    c['prompt_contract']=qa_prompts.manifest(c['prompt_version'],'research',task)
    b=c['background_sources'][0]
    a=dict(direct_answer_zh='若相关技术能够降低成本，金融机构可能受益；实际效果还取决于模型风险与实施成本。',
        answer_person_basis=[1],answer_background_basis=[1],points=old['points'],
        background_points=[dict(text_zh='背景资料报告银行用AI识别欺诈。',citations=[dict(source_ref=b['source_ref'],text='Banks report using artificial intelligence for fraud detection.')])],
        analysis=[dict(text_zh='若自动化节约大于部署与风控成本，盈利能力可能改善。',person_basis=[1],background_basis=[1],
            assumptions_zh=['实际部署有效，且成本可控。'],counterfactors_zh=['模型风险与合规成本可能抵消收益。'])],
        research_steps=[],evidence_gaps_zh=['需取得目标公司和报告期间，核对实际AI投入。'],limitations_zh=['合成数据只测试引用与流程。'],needs_review=True)
    return c,a

def select_quotes(context,answer):
    answer=copy.deepcopy(answer)
    for group in ('points','background_points'):
        for point in answer[group]:
            for quote in point['citations']:
                unit=next(q for q in context['quote_catalog'] if q['source_ref']==quote['source_ref'] and quote['text'] in q['text'])
                quote.pop('text');quote['quote_id']=unit['quote_id']
    return answer


class SelectedQuoteContracts(unittest.TestCase):
    def fixture(self):
        c,a=synthesis_fixture();c['prompt_version']=qa_prompts.SYNTHESIS;c['prompt_contract']=qa_prompts.manifest(c['prompt_version'],'research')
        c['quote_catalog']=syn.quote_catalog(c);return c,select_quotes(c,a)
    def test_model_selects_units_and_host_preserves_exact_words(self):
        c,a=self.fixture();v=validate_answer(c,a)
        self.assertEqual(raw_answer(c,v),a);self.assertEqual(v['background_points'][0]['citations'][0]['text'],'Banks report using artificial intelligence for fraud detection.')
        self.assertEqual(validate_envelope(c,envelope(a)),v)
        self.assertIn('quote_id',json.dumps(request_payload(c)['response_format']))
        self.assertEqual(audit(dict(context=c,context_hash=digest(c),answer=v,response_hash=digest(v),execution_kind='synthetic_fixture',status='completed'))['errors'],[])
    def test_fabricated_selection_or_catalog_edit_fails(self):
        for mutation in ('quote_id','catalog','source_ref'):
            c,a=self.fixture()
            if mutation=='quote_id':a['points'][0]['citations'][0]['quote_id']='q_'+'0'*16
            elif mutation=='source_ref':a['points'][0]['citations'][0]['source_ref']='src_'+'0'*32
            else:c['quote_catalog'][0]['text']='edited quotation'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):validate_answer(c,a)
    def test_footnote_number_remains_in_host_inserted_quote(self):
        c,_=self.fixture();s=c['background_sources'][0];s['text']='Concentration may increase.29 AI depends on computing power and access to data resources.'
        s['text_sha256']=hashlib.sha256(s['text'].encode()).hexdigest();s['end']=s['start']+len(s['text']);s['source_ref']=syn.canonical(s)['source_ref']
        units=syn.quote_catalog(c);q=next(q for q in units if q['source_ref']==s['source_ref']);self.assertIn('.29',q['text'])

class SynthesisContracts(unittest.TestCase):
    def test_both_roles_feed_payload_and_validate_separately(self):
        c,a=synthesis_fixture();value=validate_answer(c,a);p=request_payload(c)
        self.assertEqual(value['outcome'],'answered');self.assertEqual(value['answer_mode'],'synthesis')
        self.assertEqual(value['background_points'][0]['citations'][0]['role'],'background')
        self.assertEqual(value['points'][0]['citations'][0]['role'],'main_text')
        self.assertIn(TEXT,p['messages'][-1]['content']);self.assertEqual(p['max_tokens'],6144)
        self.assertNotIn('tools',p);self.assertNotIn('outcome',p['response_format']['json_schema']['schema']['properties'])
        self.assertEqual(validate_answer(c,raw_answer(c,value)),value)
        self.assertEqual(validate_envelope(c,envelope(a)),value)
        run=dict(context=c,context_hash=digest(c),answer=value,response_hash=digest(value),execution_kind='synthetic_fixture',status='completed')
        self.assertEqual(audit(run)['errors'],[]);self.assertEqual(audit(run)['citations'],2)
        self.assertEqual(audit(run)['semantic_support'],'not_assessed')
    def test_neither_source_role_can_impersonate_the_other(self):
        for change in ('person_quote','background_quote','background_person','source_hash','source_url','source_ref','offset'):
            c,a=synthesis_fixture()
            if change=='person_quote':a['points'][0]['citations']=a['background_points'][0]['citations']
            elif change=='background_quote':a['background_points'][0]['citations']=a['points'][0]['citations']
            elif change=='background_person':c['background_sources'][0]['subject_person_id']=c['person_id']
            elif change=='source_hash':c['background_sources'][0]['text_sha256']='0'*64
            elif change=='source_url':c['background_sources'][0]['url']='https://example.invalid'
            elif change=='source_ref':c['background_sources'][0]['source_ref']='made-up'
            else:c['background_sources'][0]['end']+=1
            with self.subTest(change=change),self.assertRaises(ValueError):validate_answer(c,a)
    def test_absent_ambiguous_and_clipped_background_quotes_fail(self):
        for text in ('Invented fact','anks report','Banks report using artificia'):
            c,a=synthesis_fixture();a['background_points'][0]['citations'][0]['text']=text
            with self.subTest(text=text),self.assertRaises(ValueError):validate_answer(c,a)
    def test_premise_indices_and_blank_fields_are_strict(self):
        for key,value in [('background_basis',[]),('background_basis',[2]),('background_basis',[True]),('background_basis',[1,1])]:
            c,a=synthesis_fixture();a['analysis'][0]['person_basis']=[];a['analysis'][0][key]=value
            with self.subTest(value=value),self.assertRaises(ValueError):validate_answer(c,a)
        c,a=synthesis_fixture();a['direct_answer_zh']=' '
        with self.assertRaises(ValueError):validate_answer(c,a)
    def test_background_only_is_not_attributed_to_person(self):
        c,a=synthesis_fixture();c['sources']=[];a['points']=[];a['answer_person_basis']=[];a['analysis'][0]['person_basis']=[]
        value=validate_answer(c,a);self.assertEqual(value['outcome'],'answered');self.assertEqual(value['points'],[])
        self.assertTrue(value['background_points'][0]['citations'][0]['not_person_statement'])
    def test_no_sources_can_give_methods_but_not_manufacture_impact(self):
        c,_=synthesis_fixture('method_checklist');c['sources']=[];c['background_sources']=[]
        a=raw_answer(c,abstention(c,'缺少公司和期间。'));a['research_steps']=[dict(data_zh='年度资本支出',purpose_zh='比较投入',how_to_check_zh='在公司10-K中核对期间、单位与口径。')]
        self.assertEqual(validate_answer(c,a)['outcome'],'answered')
        c['research_task']='impact_analysis';c['prompt_contract']=qa_prompts.manifest(c['prompt_version'],'research')
        with self.assertRaises(ValueError):validate_answer(c,a)
    def test_entry_page_and_search_snippet_are_not_body_evidence(self):
        for kind in ('suggested','unavailable'):
            r=resource();r['status']=kind;self.assertIsNone(syn.canonical(r))
        r=resource();r.pop('content_kind');self.assertIsNone(syn.canonical(r))
    def test_canonical_reuse_preserves_absolute_unicode_offsets(self):
        r=resource();a=syn.canonical(r);b=syn.canonical(a,cached=True)
        self.assertEqual(a['source_ref'],b['source_ref']);self.assertEqual(b['start'],120)

class SynthesisPreparation(unittest.TestCase):
    def context(self,text='人工智能对金融行业有什么影响？',history=None,**updates):
        reader=ResearchReader();q=dict(person_id=reader.person,message_id=uuid4(),conversation_id=uuid4(),content=text,position=3,history=history or [])
        body=Prepare(client_request_id=uuid4(),message_id=q['message_id'],retrieval_mode='hybrid',read_official_sources=True,limit=6,**updates)
        return reader,q,body
    def lookup(self,*args,**kwargs):
        if not kwargs.get('enabled'):return nav.prepare(*args,**kwargs)
        self.assertEqual(kwargs['max_reads'],2);self.assertLessEqual(kwargs['max_seconds'],25)
        return dict(resources=[resource()],reads=1,network_requests=2,search={'status':'not_configured','requests':0})
    def test_free_background_is_frozen_before_one_generation(self):
        reader,q,body=self.context()
        original=nav.prepare
        def lookup(*args,**kw):return self.lookup(*args,**kw) if kw.get('enabled') else original(*args,**kw)
        with patch('app.research_sources.prepare',side_effect=lookup):c=prepare_context(ToolSession(reader,[reader.person]),q,body)
        self.assertEqual(c['prompt_version'],qa_prompts.SYNTHESIS);self.assertTrue(c['sources']);self.assertTrue(c['background_sources'])
        self.assertLessEqual(len(encoded(c)),32768);self.assertEqual(c['research_workflow']['model_generation_limit'],1)
    def test_recent_raw_snapshots_reused_without_reading_or_old_answer_as_evidence(self):
        history=[dict(position=1,message_id=uuid4(),question='人工智能对金融行业的影响',answer_zh='INVENTED_PRIOR_MODEL_ASSERTION',
            source_resources=[resource('fed-bank-ai-testimony'),resource('treasury-financial-ai-report')],answer_run_id=uuid4())]
        reader,q,body=self.context('结合已找到的官方背景资料，继续分析前文讨论的行业影响',history)
        c,_=synthesis_fixture();c['memory']=agent_memory.select(q['content'],history)[1]
        with patch('app.research_sources.prepare',wraps=nav.prepare) as lookup:syn.prepare(c,q,body)
        self.assertEqual(len(c['background_sources']),2);self.assertTrue(all(s['cached_snapshot'] for s in c['background_sources']))
        self.assertEqual(lookup.call_count,1);self.assertTrue(all('INVENTED_PRIOR_MODEL_ASSERTION' not in s['text'] for s in c['background_sources']))
    def test_person_summary_and_comparison_do_not_fetch_other_officials(self):
        for text in ('总结这篇讲话','比较两个时期的说法'):
            r,q,b=self.context(text)
            with patch('app.research_sources.prepare',side_effect=AssertionError('Unexpected background fetch')):c=prepare_context(ToolSession(r,[r.person]),q,b)
            self.assertEqual(c['background_sources'],[]);self.assertEqual(c['research_workflow']['stop_reason'],'person_evidence_only')
    def test_three_people_and_question_types_use_same_reusable_contract(self):
        for name in ('Barr','Trump','Lagarde'):
            for text in ('人工智能对金融行业有什么影响？','研究公司利润率需要哪些数据？','总结这篇讲话','比较两个时期的说法'):
                with self.subTest(person=name,question=text):
                    r,q,b=self.context(name+' '+text)
                    original=nav.prepare
                    with patch('app.research_sources.prepare',side_effect=lambda *a,**k: self.lookup(*a,**k) if k.get('enabled') else original(*a,**k)):
                        c=prepare_context(ToolSession(r,[r.person]),q,b)
                    self.assertEqual(c['person_id'],str(r.person));self.assertEqual(c['prompt_version'],qa_prompts.SYNTHESIS)
                    self.assertLessEqual(c['plan']['tool_call_limit'],8)
    def test_topic_reset_or_history_off_never_reuses_old_source_snapshots(self):
        for reset,enabled in ((True,True),(False,False)):
            c,_=synthesis_fixture();c['memory']={'topic_reset':reset,'retrieval_hint':'人工智能金融行业影响'}
            q={'content':'人工智能金融行业影响','history':[{'source_resources':[resource()]}]}
            b=Prepare(client_request_id=uuid4(),message_id=uuid4(),retrieval_mode='hybrid',use_history=enabled)
            syn.prepare(c,q,b);self.assertFalse(c['background_sources'])
    def test_expired_or_out_of_date_range_snapshots_not_used(self):
        r=resource();r['read_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        c,_=synthesis_fixture();c['memory']={'retrieval_hint':'人工智能金融行业影响'}
        q={'content':'人工智能金融行业影响','history':[{'source_resources':[r]}]}
        b=Prepare(client_request_id=uuid4(),message_id=uuid4(),retrieval_mode='hybrid')
        syn.prepare(c,q,b);self.assertFalse(c['background_sources'])
        b=b.model_copy(update={'since':datetime(2026,11,1,tzinfo=timezone.utc)})
        self.assertFalse(syn.in_dates(resource(),b))
    def test_reanalysis_after_source_lookup_is_impact_not_another_directory(self):
        memory={'followup_detected':True,'routing_seed':{'strategy':'research','task':'source_lookup','question':'银行AI应用资料在哪里找'}}
        self.assertEqual(agent_intent.route('结合已找到的官方背景资料，重新分析行业影响',memory=memory)['task'],'impact_analysis')
    def test_just_discussed_follows_latest_task_not_old_similar_question(self):
        old=dict(position=1,message_id=uuid4(),question='研究股票需要哪些公司数据，说明用途和核对步骤。')
        latest=dict(position=2,message_id=uuid4(),question='结合官方背景，重新分析 Barr 关于人工智能对金融行业的影响。')
        _,memory=agent_memory.select('接着刚才的分析，研究股票先核对哪些公司数据？说明用途和步骤。',[old,latest])
        self.assertEqual(memory['hint_message_id'],str(latest['message_id']));self.assertIn('人工智能',memory['retrieval_hint'])
    def test_default_off_preserves_old_prompt_and_network_free_path(self):
        r,q,b=self.context();b=b.model_copy(update={'read_official_sources':False})
        with patch('app.research_sources.prepare',side_effect=AssertionError('Unexpected fetch')):c=prepare_context(ToolSession(r,[r.person]),q,b)
        self.assertEqual(c['prompt_version'],qa_prompts.RESEARCH)

if __name__=='__main__':unittest.main()
