import copy
import json
import os
import unittest
from uuid import uuid4, UUID
from unittest.mock import patch
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from pydantic import ValidationError
from app.annotation_model import digest
from app.quality_model import Feedback, Review, audit, metrics
from app.repositories import quality as repo, chat, qa


def label(packet, **changes):
    value={'expected_revision':0,'independent_human_review':True,
        'relevance':[{'version_id':p['version_id'],'relevant':True,'evidence':[{'start':0,'end':5,'text':p['body'][:5]}]} for p in packet['pool']],
        'supported':True,'person_correct':True,'time_correct':True,'useful':True,'note':'Synthetic human review contract fixture, not a real annotation.'}
    return Review.model_validate({**value,**changes})


class QualityContractTests(unittest.TestCase):
    def setUp(self):
        self.vid=str(uuid4());self.packet={'pool':[{'version_id':self.vid,'body':'Alpha 😀 test'}],'retrieved_version_ids':[self.vid]}

    def test_relevance_requires_exact_unicode_evidence_and_complete_pool(self):
        label(self.packet).validate_packet(self.packet)
        for relevance in ([],[{'version_id':self.vid,'relevant':True}],
                [{'version_id':self.vid,'relevant':True,'evidence':[{'start':6,'end':7,'text':'x'}]}]):
            with self.assertRaises(ValueError):label(self.packet,relevance=relevance).validate_packet(self.packet)

    def test_no_fake_human_attestation_or_coerced_booleans(self):
        for change in ({'independent_human_review':False},{'supported':'true'},{'note':'  '},{'note':'\x00'}):
            with self.assertRaises(ValidationError):label(self.packet,**change)

    def test_duplicate_or_outside_pool_versions_rejected(self):
        one=label(self.packet).relevance[0].model_dump()
        for rows in ([one,one],[{**one,'version_id':uuid4()}]):
            with self.assertRaises(ValueError):label(self.packet,relevance=rows).validate_packet(self.packet)

    def test_empty_denominators_are_unknown_not_perfect_scores(self):
        empty={'pool':[],'retrieved_version_ids':[]};review=label(empty)
        value=metrics(empty,review);self.assertIsNone(value['precision_in_pool']);self.assertIsNone(value['recall_in_pool'])

    def test_closed_pool_recall_counts_missed_relevant_document(self):
        other=str(uuid4());self.packet['pool'].append({'version_id':other,'body':'Other relevant text'})
        value=metrics(self.packet,label(self.packet));self.assertEqual(value['precision_in_pool'],1);self.assertEqual(value['recall_in_pool'],.5)

    def test_feedback_is_bounded_and_does_not_accept_human_truth_field(self):
        Feedback(client_request_id=uuid4(),kind='unsupported',note='需要核对。')
        for values in ({'note':' '},{'kind':'gold_label'},{'human_verified':True}):
            with self.assertRaises(ValidationError):Feedback.model_validate({'client_request_id':uuid4(),'kind':'useful','note':'ok',**values})

    def test_review_helper_does_not_prefill_human_truth_and_rejects_redirect_origins(self):
        from app.quality_review import template,local_base
        value=template({'packet':self.packet,'packet_hash':digest(self.packet)})
        self.assertIsNone(value['supported']);self.assertIsNone(value['relevance'][0]['relevant'])
        with self.assertRaises(ValidationError):Review.model_validate(value)
        for url in ('https://example.org','http://user:secret@localhost:8000','http://127.0.0.1:8000/path','http://localhost/?next=external'):
            with self.assertRaises(ValueError):local_base(url)
        self.assertEqual(local_base('http://127.0.0.1:8000/'),'http://127.0.0.1:8000')


@unittest.skipUnless(os.environ.get('PQ_QUALITY_DB_TESTS')=='1','Isolated quality DB tests are opt-in')
class QualityDatabaseTests(unittest.TestCase):
    # Reuse isolated fixtures, never the production schema or a real model.
    import test_agent_qa_db as fixtures
    setUpClass=classmethod(fixtures.QADatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.QADatabaseTests.drop_schema.__func__)
    prepared=fixtures.QADatabaseTests.prepared
    result=fixtures.QADatabaseTests.result

    def setUp(self):
        self.fixtures.QADatabaseTests.setUp(self)
        from app.event_model import EventSnapshot
        from app.repositories.events import save_snapshot
        from datetime import datetime,timezone
        old=self.c.execute(text('SELECT v.payload,s.source_id FROM event_versions v JOIN source_channels s USING(channel_id) WHERE v.version_id=:v'),{'v':self.version['version_id']}).mappings().one()
        original=EventSnapshot.model_validate(old['payload']).model_copy(update={'post_type':'original'})
        self.version=save_snapshot(self.c,old['source_id'],'rss',original,datetime.now(timezone.utc))
        self.spec=self.spec.model_copy(update={'event_id':UUID(self.version['event_id']),'version_id':UUID(self.version['version_id'])})
        self.run=self.prepared();self.result(self.run)
        self.path=self.url+'/answers/'+str(self.run['run_id'])

    def case(self):return repo.create_case(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])

    def test_feedback_is_idempotent_owner_scoped_and_append_only(self):
        body=Feedback(client_request_id=uuid4(),kind='unsupported',note='合成反馈，不代表真人评价。')
        first=repo.feedback(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'],body)
        self.assertFalse(repo.feedback(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'],body)['created'])
        with self.assertRaises(chat.Conflict):repo.feedback(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'],body.model_copy(update={'note':'different'}))
        self.assertEqual(self.client.get(self.path+'/feedback',headers=self.hb).status_code,404)
        self.assertEqual(self.client.get(self.path+'/feedback',headers=self.ha).json()['items'][0]['kind'],'unsupported')
        with self.assertRaises(DBAPIError),self.c.begin_nested():self.c.execute(text('DELETE FROM agent_answer_feedback WHERE feedback_id=:id'),{'id':first['feedback_id']})

    def test_report_never_treats_feedback_or_single_review_as_truth(self):
        self.client.post(self.path+'/feedback',headers=self.ha,json={'client_request_id':str(uuid4()),'kind':'useful','note':'合成测试。'})
        value=self.case();repo.review(self.c,'A',value['case_id'],label(value['packet']))
        report=repo.report(self.c,self.a['user_id'])
        self.assertEqual(report['personal_feedback']['distinct_answers'],1)
        self.assertFalse(report['personal_feedback']['is_ground_truth']);self.assertEqual(report['human_consensus_metrics']['cases'],0)
        self.assertEqual(report['cases'][0]['state'],'provisional');self.assertIsNone(report['final_agent_score'])

    def test_audit_detects_changed_hash_quote_and_identity(self):
        run=repo.answer(self.c,self.a['user_id'],self.conv['conversation_id'],self.run['run_id'])
        self.assertEqual(audit(run)['state'],'passed')
        bad=copy.deepcopy(run);bad['answer']['points'][0]['citations'][0]['start']+=1
        self.assertIn('citation_text',audit(bad)['errors']);self.assertIn('answer_hash',audit(bad)['errors'])
        bad=copy.deepcopy(run);bad['context']['sources'][0]['person_id']=str(uuid4())
        self.assertIn('source_identity',audit(bad)['errors']);self.assertIn('context_hash',audit(bad)['errors'])
        provider=copy.deepcopy(run);provider['execution_kind']='openrouter';provider['response_hash']=digest({'envelope':'fixture'})
        check=audit(provider);self.assertEqual(check['state'],'partial');self.assertEqual(check['errors'],[])

    def test_case_freezes_hash_and_is_owner_scoped_idempotent(self):
        value=self.case();self.assertEqual(digest(value['packet']),value['packet_hash'])
        self.assertEqual(value['case_id'],self.case()['case_id'])
        self.assertEqual(self.client.get('/api/v1/agent-chat/quality-cases/'+str(value['case_id']),headers=self.hb).status_code,404)
        with self.assertRaises(DBAPIError),self.c.begin_nested():self.c.execute(text("UPDATE agent_quality_cases SET packet='{}'::jsonb WHERE case_id=:id"),{'id':value['case_id']})

    def test_partial_retrieved_segment_is_not_compared_with_full_body_hash(self):
        import hashlib
        context=copy.deepcopy(self.run['context']);source=context['sources'][0]
        source.update(text=source['text'][6:20],start=6,end=20,truncated=True)
        source['text_sha256']=hashlib.sha256(source['text'].encode()).hexdigest()
        run=qa.save_prepared(self.c,self.a['user_id'],self.conv['conversation_id'],uuid4(),self.run['request_spec'],self.run['request_hash'],context,self.run['model_id'],self.run['prompt_version'])
        self.result(run)
        value=repo.create_case(self.c,self.a['user_id'],self.conv['conversation_id'],run['run_id'])
        self.assertNotEqual(value['packet']['pool'][0]['body_sha256'],source['text_sha256'])
        self.assertEqual(value['packet']['automatic_audit']['state'],'passed')

    def test_consensus_and_disagreement_and_revisions_do_not_mix_fixtures(self):
        value=self.case();cid=value['case_id'];review=label(value['packet'])
        repo.review(self.c,'A',cid,review);repo.review(self.c,'B',cid,review)
        result=repo.report(self.c,self.a['user_id']);self.assertEqual(result['synthetic_diagnostics']['cases'],1)
        self.assertEqual(result['human_consensus_metrics']['cases'],0)
        repo.review(self.c,'B',cid,review.model_copy(update={'expected_revision':1,'supported':False}))
        self.assertEqual(repo.resolution(self.c,value)['state'],'disputed')
        with self.assertRaises(chat.Conflict):repo.review(self.c,'B',cid,review)
        self.assertEqual(repo.reviewer_case(self.c,'A',cid)['own_review']['reviewer'],'A')
        self.assertNotIn('other_reviews',repo.reviewer_case(self.c,'A',cid))

    def test_case_denies_unconsented_prepared_and_blind_content(self):
        self.assertEqual(self.client.post(self.path+'/quality-case',headers=self.ha,json={'share_with_reviewers':False}).status_code,422)
        value=self.case()
        from app.repositories import annotations
        data=annotations.create(self.c,'Synthetic holdout reservation')
        annotations.add(self.c,data['dataset_id'],version_id=UUID(self.version['version_id']))
        self.c.execute(text("UPDATE annotation_samples SET split='blind' WHERE dataset_id=:d"),{'d':data['dataset_id']})
        with self.assertRaises(chat.Missing):repo.case(self.c,value['case_id'],self.a['user_id'])
        self.assertEqual(repo.report(self.c,self.a['user_id'])['cases'],[])

    def test_read_only_report_has_no_model_and_is_private_no_store(self):
        path='/api/v1/agent-chat/quality-report'
        response=self.client.get(path,headers=self.ha);self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store');self.assertEqual(response.json()['model_requests'],0)
        self.assertEqual(self.client.get(path,headers=self.hb).json()['sampled_answers'],0)
        self.assertEqual(self.client.get(path).status_code,401)
        self.assertEqual(self.client.post(self.path+'/feedback',headers=self.ha,json={'client_request_id':str(uuid4()),'kind':'useful','note':'secret','unexpected':True}).status_code,422)

    def test_review_auth_disabled_by_default(self):
        cid=self.case()['case_id'];response=self.client.get('/api/v1/quality-review/'+str(cid),headers=self.ha)
        self.assertEqual(response.status_code,503);self.assertEqual(response.headers['cache-control'],'no-store')

    def test_authenticated_review_http_commits_and_keeps_other_labels_hidden(self):
        from pydantic import SecretStr
        from contextlib import contextmanager
        value=self.case();cid=value['case_id'];path='/api/v1/quality-review/'+str(cid)
        self.client.app.state.settings.annotation_reviewer_tokens=SecretStr(json.dumps({'A':'a'*40,'B':'b'*40}))
        class Engine:
            @contextmanager
            def begin(other):
                with self.c.begin_nested():yield self.c
            def dispose(other):pass
        with patch('app.api.quality.chat.engine_for',return_value=Engine()):
            for who in ('A','B'):
                h={'Authorization':'Bearer '+who.lower()*40}
                result=self.client.get(path,headers=h);self.assertEqual(result.status_code,200,result.text)
                self.assertIsNone(result.json()['own_review'])
                result=self.client.post(path,headers=h,json=label(value['packet']).model_dump(mode='json'))
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(self.client.get(path,headers=h).json()['own_review']['reviewer'],who)
            self.assertEqual(repo.resolution(self.c,value)['state'],'consensus')
            wrong=self.client.post(path,headers={'Authorization':'Bearer '+'a'*40},json=label(value['packet']).model_dump(mode='json'))
            self.assertEqual(wrong.status_code,409)
            bad=self.client.post(path,headers={'Authorization':'Bearer '+'a'*40},json={'note':'sensitive','extra':'private'})
            self.assertEqual(bad.status_code,422);self.assertNotIn('sensitive',bad.text)
