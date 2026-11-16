import json
import unittest
from app.model_trial import Case,validate_quote_response,quote_request_payload,QuoteAnswer
from test_model_trial import cases,envelope,IDS


class QuoteEvidenceTests(unittest.TestCase):
    def response(self,quote):
        value=envelope();answer=json.loads(value['choices'][0]['message']['content']);answer['evidence']=[{'text':quote}]
        value['choices'][0]['message']['content']=json.dumps(answer);return value

    def test_unique_quote_server_computes_raw_offsets(self):
        case=Case.model_validate(cases()[0]);result=validate_quote_response(self.response('tariffs'),case,IDS[0])
        self.assertEqual(result['answer']['evidence'],[{'start':9,'end':16,'text':'tariffs'}])

    def test_missing_ambiguous_and_fabricated_quote_rejected(self):
        for body,quote in [('tariffs tariffs','tariffs'),('real text','invented')]:
            case=Case(case_id='x',body=body,provenance='official',topics=['trade'])
            with self.assertRaises(ValueError):validate_quote_response(self.response(quote),case,IDS[0])

    def test_no_evidence_offset_requested_and_expected_label_excluded(self):
        payload=quote_request_payload(IDS[0],Case.model_validate(cases()[0]))
        self.assertEqual(payload['response_format']['json_schema']['schema']['$defs']['Quote']['required'],['text'])
        self.assertNotIn('expected_relevant',payload['messages'][1]['content'])
        self.assertIn('needs_review must be true',payload['messages'][0]['content'])

    def test_truncated_envelope_remains_rejected(self):
        value=self.response('tariffs');value['choices'][0]['finish_reason']='length'
        with self.assertRaises(ValueError):validate_quote_response(value,Case.model_validate(cases()[0]),IDS[0])

    def test_uncertain_without_review_and_irrelevant_topics_rejected(self):
        for relevant,topics in ((None,[]),(False,['trade'])):
            value=self.response('tariffs');answer=json.loads(value['choices'][0]['message']['content'])
            answer.update(relevant=relevant,topics=topics,needs_review=False)
            value['choices'][0]['message']['content']=json.dumps(answer)
            with self.assertRaises(ValueError):validate_quote_response(value,Case.model_validate(cases()[0]),IDS[0])
