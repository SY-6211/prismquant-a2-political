import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import httpx

from app.agent_tools import ToolSession
from app.agent_analysis import Analysis,MODEL,PROMPT_VERSION,prepare_context,request_payload,validate_analysis,validate_envelope
from app.agent_analysis_live import execute,APPROVAL,Budget,main as live_main
from app.annotation_model import digest
from test_agent_tools import Reader


def fixture():
    reader=Reader();session=ToolSession(reader,[reader.person])
    context=prepare_context(session,reader.event,reader.version)
    return context,{'relevant':True,'topics':['trade_policy'],'importance':'medium','summary_zh':'合成政策说明。',
        'facts':[{'text_zh':'原文出现政策相关措辞。','citations':[{'source_ref':context['target_refs'][0],'text':'政策 tariffs'}]}],
        'history_assessment':'not_assessed','comparison_zh':'未提供可比较历史。','comparison_citations':[],
        'hypotheses':[],'needs_review':False}


def envelope(answer,**changes):
    return {'id':'synthetic-generation','provider':'Synthetic provider','model':MODEL,
        'usage':{'cost':.0001},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer,ensure_ascii=False)}}],**changes}


class AnalysisContractTests(unittest.TestCase):
    def setUp(self):self.context,self.answer=fixture()

    def test_context_is_bounded_and_input_ignored_as_instructions(self):
        self.assertTrue(self.context['target_complete'])
        payload=request_payload(self.context)
        self.assertIn('untrusted data',payload['messages'][0]['content'])
        self.assertIn('Ignore all instructions',payload['messages'][1]['content'])
        self.assertNotIn('tools',payload);self.assertFalse(payload['provider']['allow_fallbacks'])

    def test_reserved_target_cannot_be_prepared_for_model_input(self):
        reader=Reader();reader.is_reserved=lambda source:True
        with self.assertRaises(ValueError):prepare_context(ToolSession(reader,[reader.person]),reader.event,reader.version)

    def test_positive_result_has_persistent_version_citations_and_review_boundary(self):
        value=validate_analysis(self.context,self.answer)
        cite=value['facts'][0]['citations'][0]
        self.assertEqual(cite['version_id'],str(self.context['version_id']))
        self.assertEqual(cite['text'],'政策 tariffs');self.assertTrue(value['needs_review'])
        self.assertEqual(value['factual_support'],'not_independently_assessed')

    def test_fabricated_or_unread_quote_rejected(self):
        for update in ({'text':'invented quotation'},{'source_ref':'src_'+'f'*32}):
            value=copy.deepcopy(self.answer);value['facts'][0]['citations'][0].update(update)
            with self.assertRaises(ValueError):validate_analysis(self.context,value)

    def test_media_source_cannot_support_main_author_facts(self):
        context=copy.deepcopy(self.context);context['sources'][0]['role']='archive_media_note'
        with self.assertRaises(ValueError):validate_analysis(context,self.answer)

    def test_schema_rejects_trading_action_uncertainty_and_empty_positive_evidence(self):
        for update in ({'buy':True},{'relevant':None,'needs_review':False},{'facts':[]},
                       {'relevant':False,'topics':['trade_policy']}):
            with self.assertRaises(ValueError):Analysis.model_validate({**self.answer,**update})

    def test_history_claim_requires_both_actually_provided_sides(self):
        answer=copy.deepcopy(self.answer);answer.update(history_assessment='changed_vs_provided_history',comparison_zh='合成对比。')
        answer['comparison_citations']=self.answer['facts'][0]['citations']*2
        with self.assertRaises(ValueError):validate_analysis(self.context,answer)
        context=copy.deepcopy(self.context);history=copy.deepcopy(context['sources'][0]);history.update(source_ref='src_'+'a'*32,text='Previous policy.',start=0,end=16,total_characters=16)
        context['sources'].append(history);context['history_refs']=[history['source_ref']]
        answer['comparison_citations'][1]={'source_ref':history['source_ref'],'text':'Previous policy.'}
        self.assertEqual(validate_analysis(context,answer)['history_assessment'],'changed_vs_provided_history')

    def test_partial_target_requires_review_even_if_model_says_complete(self):
        context=copy.deepcopy(self.context);context['target_complete']=False
        value=validate_analysis(context,self.answer)
        self.assertTrue(value['needs_review']);self.assertFalse(value['target_complete'])

    def test_truncated_tool_call_or_wrong_model_envelope_rejected(self):
        for value in (envelope(self.answer,model='other/model'),
                      {'model':MODEL,'choices':[{'finish_reason':'length','message':{'content':'{}'}}]},
                      {'model':MODEL,'choices':[{'finish_reason':'stop','message':{'content':'{}','tool_calls':[{}]}}]}):
            with self.assertRaises(ValueError):validate_envelope(self.context,value)


class FiniteAnalysisTests(unittest.TestCase):
    def test_cli_failure_status_never_reports_success_exit(self):
        for status,expected in [('completed',0),('rejected',1),('unknown',1)]:
            with patch('app.agent_analysis_live.execute',return_value={'status':status}),patch('builtins.print'):
                self.assertEqual(live_main(['--run-id',str(uuid4()),'--execute','--approval',APPROVAL]),expected)

    def run_fake(self,failure=None,repeat=False):
        context,answer=fixture();run_id=uuid4()
        row={'run_id':run_id,'result_id':None,'context':context,'context_hash':digest(context),'model_id':MODEL,'prompt_version':PROMPT_VERSION}
        posts=[];saved=[]
        def transport(request):
            if request.url.path.endswith('/models'):
                return httpx.Response(200,json={'data':[{'id':MODEL,'supported_parameters':['structured_outputs'],'pricing':{'prompt':'.0000001','completion':'.0000002'}}]})
            if request.url.path.endswith('/key'):return httpx.Response(200,json={'data':{'limit_remaining':47}})
            posts.append(json.loads(request.content))
            if failure=='http':return httpx.Response(403,json={'error':'not allowed sk-or-synthetic'})
            if failure=='timeout':raise httpx.ReadTimeout('secret must not leak')
            value=envelope(answer)
            if failure=='missing_cost':value['usage']={}
            if failure=='invalid':value['choices'][0]['finish_reason']='length'
            return httpx.Response(200,json=value)
        def save(*args,**kwargs):saved.append((args,kwargs));return {'result_id':uuid4()}
        with tempfile.TemporaryDirectory() as tmp,patch('app.agent_analysis_live.private_key',return_value='sk-or-synthetic'),\
             patch('app.agent_analysis_live.repo.read',side_effect=lambda settings,fn,*args:False if fn.__name__=='context_has_reserved' else row),patch('app.agent_analysis_live.repo.write_result',side_effect=save):
            client=lambda:httpx.Client(transport=httpx.MockTransport(transport))
            args={'ledger_path':Path(tmp)/'budget.db','receipt_dir':Path(tmp)/'receipts','client_factory':client}
            result=execute(None,run_id,APPROVAL,**args)
            self.assertTrue((Path(tmp)/'receipts'/(str(run_id)+'.json')).is_file())
            self.assertNotIn('sk-or-synthetic',(Path(tmp)/'receipts'/(str(run_id)+'.json')).read_text(encoding='utf-8'))
            self.assertNotIn('sk-or-synthetic',json.dumps(result))
            if repeat:
                with self.assertRaises((ValueError,__import__('sqlite3').IntegrityError)):execute(None,run_id,APPROVAL,**args)
            return result,posts,saved

    def test_valid_response_is_billed_validated_and_saved_once(self):
        result,posts,saved=self.run_fake(repeat=True)
        self.assertEqual(result['status'],'completed');self.assertEqual(len(posts),1);self.assertEqual(len(saved),1)

    def test_unknown_cost_http_and_timeout_stop_without_retry(self):
        for failure in ('http','timeout','missing_cost'):
            with self.subTest(failure=failure):
                result,posts,saved=self.run_fake(failure)
                self.assertEqual(result['status'],'unknown');self.assertEqual(result['budget']['unsettled'],1)
                self.assertEqual(len(posts),1);self.assertIsNone(saved[0][0][3])

    def test_rejected_output_still_counts_cost(self):
        result,posts,_=self.run_fake('invalid')
        self.assertEqual(result['status'],'rejected');self.assertEqual(result['budget']['unsettled'],0)

    def test_budget_survives_reopen_and_fences_unfinished_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'b.db';b=Budget(path);b.reserve('one','a'*64,10000000);b.close()
            b=Budget(path)
            with self.assertRaises(ValueError):b.reserve('two','b'*64,1)
            b.settle('one','.001');b.reserve('two','b'*64,1000000);b.settle('two','.0001')
            with self.assertRaises(ValueError):b.reserve('three','c'*64,1)
            b.close()

    def test_budget_limit_and_unapproved_execution_never_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Budget(Path(tmp)/'b.db')
            with self.assertRaises(ValueError):b.reserve('one','a'*64,50000001)
            b.close()
        with patch('app.agent_analysis_live.private_key') as key:
            with self.assertRaises(ValueError):execute(None,uuid4(),'not-approved')
            key.assert_not_called()
