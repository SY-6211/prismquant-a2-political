import json
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.annotation_model import Label,grouped_splits,digest,confusion,summarize,canonical
from app.core.config import Settings
from app.main import create_app


def label(**changes):
    return Label(person_correct=True,relevant=True,topics=['trade'],importance='high',is_new=True,should_notify=False,
        boundary='main_text',evidence=[{'start':0,'end':7,'text':'tariffs'}],note='Manual fixture judgment',**changes)


def sample(n,body,event=None,hint=None):
    return {'sample_id':str(uuid4()),'snapshot':{'body':body,'body_sha256':digest(body),'published_at':f'2026-01-{n:02d}T00:00:00+00:00'},'event_key':event,'group_hint':hint}


class AnnotationTests(unittest.TestCase):
    def test_positive_requires_exact_evidence_and_topics(self):
        value=label();value.validate_evidence('tariffs rise')
        with self.assertRaises(ValueError):value.validate_evidence('a different statement')
        with self.assertRaises(ValidationError):Label(person_correct=False,relevant=True,topics=[],importance='high',is_new=True,should_notify=False,boundary='main_text',note='wrong')

    def test_unicode_raw_offsets_not_normalized_offsets(self):
        value=Label(person_correct=True,relevant=True,topics=['policy'],importance='low',is_new=None,should_notify=False,boundary='main_text',note='exact',evidence=[{'start':1,'end':3,'text':'关税'}])
        value.validate_evidence('新关税措施')
        with self.assertRaises(ValueError):value.validate_evidence('关税措施')

    def test_judgment_ignores_different_valid_evidence_notes(self):
        a=label();b=a.model_copy(update={'note':'another reviewer','evidence':[]})
        self.assertEqual(a.judgment(),b.judgment())

    def test_uncertain_cannot_label_notify(self):
        with self.assertRaises(ValidationError):Label(person_correct=True,relevant=None,topics=[],importance='medium',is_new=None,should_notify=True,boundary='uncertain',note='unknown')

    def test_event_exact_and_manual_groups_never_cross_splits(self):
        values=[sample(1,'first native version','native'),sample(6,'changed native version','native'),
            sample(2,'ＦＵＬＬ width!'),sample(7,'full width'),sample(3,'lineage old',hint='chain'),sample(8,'lineage rewritten',hint='chain'),
            sample(4,'different policy'),sample(9,'unrelated orchard')]
        assignment,meta=grouped_splits(values);where={a['sample_id']:a for a in assignment}
        for left,right in ((0,1),(2,3),(4,5)):
            self.assertEqual(where[values[left]['sample_id']]['group_id'],where[values[right]['sample_id']]['group_id'])
            self.assertEqual(where[values[left]['sample_id']]['split'],where[values[right]['sample_id']]['split'])
        self.assertEqual(set(meta['counts']),{'train','dev','blind'})

    def test_near_duplicates_are_grouped_transitively(self):
        body=' '.join('distinctword'+str(n) for n in range(90))
        values=[sample(1,body),sample(2,body+' a short extra sentence'),sample(3,'unrelated A'),sample(4,'unrelated B')]
        assignment,meta=grouped_splits(values);mapping={a['sample_id']:a['group_id'] for a in assignment}
        self.assertEqual(mapping[values[0]['sample_id']],mapping[values[1]['sample_id']]);self.assertGreater(meta['near_duplicate_pairs'],0)

    def test_split_manifest_deterministic_for_reordered_input(self):
        values=[sample(n,'unique '+str(n)) for n in range(1,10)]
        self.assertEqual(grouped_splits(values)[1]['manifest_sha256'],grouped_splits(list(reversed(values)))[1]['manifest_sha256'])

    def test_too_few_groups_rejected(self):
        with self.assertRaises(ValueError):grouped_splits([sample(1,'same'),sample(2,'same')])

    def test_metrics_hand_calculated_with_abstention(self):
        metrics=confusion([(True,True),(True,False),(False,True),(False,False),(True,None),(False,None)])
        self.assertEqual((metrics['tp'],metrics['fp'],metrics['fn'],metrics['tn'],metrics['abstained']),(1,1,2,1,2))
        self.assertEqual(metrics['precision'],.5);self.assertAlmostEqual(metrics['recall'],1/3)

    def test_empty_metrics_undefined_not_perfect(self):
        value=confusion([]);self.assertIsNone(value['precision']);self.assertIsNone(value['recall']);self.assertIsNone(value['f1'])

    def test_multilabel_topic_and_person_metrics(self):
        rows=[{'label':{'relevant':True,'topics':['trade','rates']},'predicted_topics':['trade'],'person_id':'A','unassessed':False},
            {'label':{'relevant':False,'topics':[]},'predicted_topics':['rates'],'person_id':'B','unassessed':False}]
        value=summarize(rows,['trade','rates'])
        self.assertEqual(value['by_topic']['rates']['fn'],1);self.assertEqual(value['by_topic']['rates']['fp'],1)
        self.assertEqual(value['by_person']['B']['fp'],1)

    def test_credentials_are_separate_unique_and_never_in_openapi(self):
        valid=json.dumps({'alice':'a'*32,'bob':'b'*32})
        settings=Settings(_env_file=None,keyword_admin_token='k'*32,annotation_reviewer_tokens=valid)
        for value in ('[]','{"alice":"short"}',json.dumps({'a':'x'*32,'b':'x'*32}),'{"a":"'+'x'*32+'","a":"'+'y'*32+'"}',json.dumps({'alice':'k'*32})):
            with self.assertRaises(ValidationError):Settings(_env_file=None,keyword_admin_token='k'*32,annotation_reviewer_tokens=value)
        with TestClient(create_app(settings)) as client:
            self.assertNotIn('a'*32,client.get('/openapi.json').text)

    def test_unauthorized_and_admin_cannot_impersonate_reviewer(self):
        settings=Settings(_env_file=None,keyword_admin_token='k'*32,annotation_reviewer_tokens=json.dumps({'alice':'a'*32}))
        with TestClient(create_app(settings)) as client,patch('app.api.keywords.create_engine') as database:
            path=f'/api/v1/annotation-review/{uuid4()}/samples'
            self.assertEqual(client.get(path).status_code,401)
            self.assertEqual(client.get(path,headers={'Authorization':'Bearer '+'k'*32}).status_code,401)
            self.assertEqual(client.get('/api/v1/annotation-datasets',headers={'Authorization':'Bearer '+'a'*32}).status_code,401)
            database.assert_not_called()

    def test_developer_blind_export_and_benchmark_fail_validation(self):
        with TestClient(create_app(Settings(_env_file=None,keyword_admin_token='k'*32))) as client,patch('app.api.keywords.create_engine') as database:
            headers={'Authorization':'Bearer '+'k'*32};identity=uuid4()
            self.assertEqual(client.get(f'/api/v1/annotation-datasets/{identity}/export?split=blind',headers=headers).status_code,422)
            self.assertEqual(client.post(f'/api/v1/annotation-datasets/{identity}/benchmark',headers=headers,json={'split':'blind','version_ids':[str(uuid4())]}).status_code,422)
            database.assert_not_called()
