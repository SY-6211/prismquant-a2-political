import copy, hashlib, unittest
from uuid import uuid4
from pydantic import ValidationError
from app.rag import fuse, terms_for
from app.agent_qa import Prepare, prepare_context, validate_answer, request_payload, BASE_SYSTEM, CLOCK_RULES
from app.agent_tools import ToolSession
from test_agent_tools import Reader
from test_agent_qa import fixture


class RAGTests(unittest.TestCase):
    def test_rrf_fuses_versions_without_duplicate_votes(self):
        a={'event_id':'a','version_id':'1','text':'A'};b={'event_id':'b','version_id':'2','text':'B'}
        result=fuse([a,b],[b,b,a],6)
        self.assertEqual(len(result),2)
        self.assertAlmostEqual(result[0]['rrf_score'],1/61+1/62)
        self.assertEqual(result[0]['channel_ranks'],{'lexical':2,'semantic':1})

    def test_explicit_keyword_retains_matching_window(self):
        row={'event_id':'a','version_id':'1','text':'tariff appears far down'}
        other={**row,'text':'unrelated beginning'}
        self.assertEqual(fuse([row],[other],1,'tariff')[0]['text'],row['text'])
        self.assertEqual(terms_for('不作扩写',"%_ ' OR 1=1"),["%_ ' or 1=1"])
        self.assertIn('artificial intelligence',terms_for('人工智能和金融风险'))

    def test_default_legacy_and_hybrid_bounds(self):
        fields={'client_request_id':uuid4(),'message_id':uuid4()}
        self.assertEqual(Prepare(**fields).retrieval_mode,'literal')
        self.assertEqual(Prepare(**fields,retrieval_mode='hybrid',limit=6).limit,6)
        for changes in ({'limit':4},{'retrieval_mode':'hybrid','limit':7},{'retrieval_mode':'external'}):
            with self.assertRaises(ValidationError):Prepare(**fields,**changes)

    def test_long_document_reads_matched_region_and_cites_unicode_offsets(self):
        reader=Reader();reader.body='Filler. '*600+'目标😀人工智能带来金融风险。'+' End.'*50
        start=reader.body.index('目标');end=start+len('目标😀人工智能带来金融风险。')
        def retrieve(args):
            return {'status':'ok','items':[{'event_id':reader.event,'version_id':reader.version,'person_id':reader.person,
                'snapshot_hash':'a'*64,'start_offset':start,'end_offset':end,'text_sha256':hashlib.sha256(reader.body[start:end].encode()).hexdigest()}],
                'retrieval':{'mode':'hybrid_rrf','queries':[],'semantic_vector_search':True}}
        reader.hybrid=retrieve
        question={'person_id':reader.person,'message_id':uuid4(),'conversation_id':uuid4(),'content':'人工智能风险？','position':1,'history':[]}
        body=Prepare(client_request_id=uuid4(),message_id=question['message_id'],retrieval_mode='hybrid')
        context=prepare_context(ToolSession(reader,[reader.person]),question,body)
        self.assertEqual(context['sources'][0]['start'],start);self.assertGreater(start,1500)
        answer={'outcome':'answered','points':[{'text_zh':'原文谈及人工智能带来的金融风险。','citations':[{'source_ref':context['sources'][0]['source_ref'],'text':'😀人工智能'}]}],'limitations_zh':['测试资料。'],'needs_review':True}
        validated=validate_answer(context,answer)
        self.assertEqual(validated['points'][0]['citations'][0]['start'],start+2)
        bad=copy.deepcopy(context);bad['sources'][0]['role']='archive_media_note'
        with self.assertRaises(ValueError):validate_answer(bad,answer)
        saved=reader.hybrid
        def corrupted(args):
            result=saved(args);result['items'][0]['text_sha256']='b'*64;return result
        reader.hybrid=corrupted
        with self.assertRaises(ValueError):prepare_context(ToolSession(reader,[reader.person]),question,body)

    def test_foreign_person_rejected_before_retriever(self):
        reader=Reader();reader.hybrid=lambda args: self.fail('Out of scope reader called')
        response=ToolSession(reader,[reader.person]).invoke('hybrid_search',{'person_id':str(uuid4()),'question':'policy'})
        self.assertEqual(response['code'],'person_out_of_scope')

    def test_frozen_v3_prompt_and_v4_grounding_are_separate(self):
        context,answer=fixture();context['prompt_version']='person_qa_v3'
        self.assertEqual(request_payload(context)['messages'][0]['content'],BASE_SYSTEM+CLOCK_RULES+'person_qa_v3')
        self.assertTrue(validate_answer(context,answer)['points'])
        fresh,_=fixture();self.assertIn('NOT confidence',request_payload(fresh)['messages'][0]['content'])


if __name__=='__main__':unittest.main()
