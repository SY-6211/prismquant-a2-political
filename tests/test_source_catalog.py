import copy,json,os,unittest
from uuid import uuid4
from unittest.mock import patch
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.core.config import Settings
from app.main import create_app
from app.source_catalog import load_manifest
from app.source_catalog_model import Review,public_url
from app.repositories import source_catalog as repo
import test_agent_tools_db as fixtures

class CatalogContracts(unittest.TestCase):
    def test_manifest_preserves_all_rows_and_label_mapping(self):
        manifest=load_manifest();self.assertEqual(len(manifest['entries']),84)
        self.assertEqual(sum(len(e['original']['labels']) for e in manifest['entries']),114)
        self.assertEqual(sum(not e['original']['reference_url'] for e in manifest['entries']),7)
        self.assertEqual(manifest['entries'][82]['triage']['entry_kind'],'profile')
        self.assertEqual(manifest['entries'][11]['original']['labels'][1]['label'],'Ray Dalio（Bloomberg访谈）')
    def test_all_import_triage_is_explicitly_unverified(self):
        for entry in load_manifest()['entries']:
            self.assertEqual(entry['triage']['verification'],'offline_shape_only')
            self.assertIn('entry_not_verified',entry['triage']['issues'])
            self.assertNotIn('enabled',entry)
    def test_bad_urls_cannot_be_saved_as_evidence(self):
        for url in ['javascript:alert(1)','http://127.0.0.1','http://localhost/','https://user:password@example.com','https://example.com/?api_key=secret','https://example.com/?token=secret']:
            with self.subTest(url=url),self.assertRaises(ValueError):public_url(url)
        self.assertEqual(public_url('https://example.com/?sc_lang=en'),'https://example.com/?sc_lang=en')
    def test_pilot_candidate_requires_actual_review_inputs(self):
        with self.assertRaises(ValidationError):Review(expected_revision=0,decision='pilot_candidate',note='Only a homepage')
        good=Review(expected_revision=0,decision='pilot_candidate',relevance='person_monitoring',discovery_url='https://example.com/releases',transport='html',attribution='reviewed',person_names=['Person'],access='public_documented',evidence_urls=['https://example.com/terms'],note='Reviewed for a limited pilot, not enabled.')
        self.assertEqual(good.decision,'pilot_candidate')
        with self.assertRaises(ValidationError):Review(**{**good.model_dump(),'enabled':True})
    def test_blank_notes_and_contradictory_scope_are_rejected(self):
        for fields in [{'note':' '},{'decision':'out_of_scope','relevance':'background'}]:
            body={'expected_revision':0,'decision':'needs_review','note':'Reason',**fields}
            with self.assertRaises(ValidationError):Review(**body)
    def test_admin_disabled_or_wrong_token_never_reaches_mutation(self):
        url='/api/v1/source-catalog/'+str(uuid4())+'/reviews';body={'expected_revision':0,'decision':'needs_review','note':'Review'}
        for settings,headers,status in [(Settings(_env_file=None),{},503),(Settings(_env_file=None,source_catalog_admin_token='A'*32),{},401),(Settings(_env_file=None,source_catalog_admin_token='A'*32),{'Authorization':'Bearer wrong'},401)]:
            with TestClient(create_app(settings)) as client,patch('app.api.source_catalog.execute') as execute:
                self.assertEqual(client.post(url,json=body,headers=headers).status_code,status);execute.assert_not_called()
    def test_admin_credential_is_independent_and_no_collection_endpoint_exists(self):
        with self.assertRaises(ValidationError):Settings(_env_file=None,keyword_admin_token='A'*32,source_catalog_admin_token='A'*32)
        with TestClient(create_app(Settings(_env_file=None))) as client:
            self.assertEqual(client.post('/api/v1/source-catalog/'+str(uuid4())+'/enable').status_code,404)

@unittest.skipUnless(os.environ.get('PQ_CATALOG_DB_TESTS')=='1','Isolated catalog database tests are opt-in')
class CatalogDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.AgentToolDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.AgentToolDatabaseTests.drop_schema.__func__)
    def setUp(self):
        self.c=self.engine.connect();self.tx=self.c.begin();self.addCleanup(self.c.close);self.addCleanup(lambda:self.tx.rollback() if self.tx.is_active else None)
        self.manifest=load_manifest();self.batch=repo.import_manifest(self.c,self.manifest)
        self.item=repo.listing(self.c)[0]
        self.client=TestClient(create_app(Settings(_env_file=None,source_catalog_admin_token='A'*32)));self.addCleanup(self.client.close)
        self.mock=patch('app.api.source_catalog.execute',side_effect=lambda settings,fn,*args,readonly=True:fn(self.c,*args));self.mock.start();self.addCleanup(self.mock.stop)
    def test_import_is_idempotent_and_does_not_create_active_sources(self):
        again=repo.import_manifest(self.c,self.manifest);self.assertFalse(again['created']);self.assertEqual(again['import_id'],self.batch['import_id'])
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM source_catalog_candidates')).scalar_one(),84)
        for table in ('people','sources','person_sources','collection_runs'):
            self.assertEqual(self.c.execute(text(f'SELECT count(*) FROM {table}')).scalar_one(),0)
    def test_filters_pagination_and_metadata_preserve_exact_counts(self):
        summary=repo.overview(self.c);self.assertEqual(summary['candidate_count'],84);self.assertEqual(summary['issues']['missing_url'],7);self.assertEqual(summary['reviewed_count'],0)
        self.assertEqual(len(repo.listing(self.c,limit=100,issue='missing_url')),7)
        self.assertEqual(len(repo.listing(self.c,q='Elon')),1);self.assertEqual(repo.listing(self.c,q='%_'),[])
        rows=[];cursor=None
        while True:
            page=repo.listing(self.c,limit=13,cursor=cursor);rows.extend(page[:13])
            if len(page)<=13:break
            cursor=page[12]['candidate_id']
        self.assertEqual([r['number'] for r in rows],list(range(1,85)))
    def test_a_review_appends_and_stale_revision_is_rejected(self):
        candidate=self.item['candidate_id'];one=repo.review(self.c,candidate,Review(expected_revision=0,decision='awaiting_access',note='Need account permission'),'reviewer')
        self.assertEqual(one['revision'],1)
        with self.assertRaises(repo.Conflict):repo.review(self.c,candidate,Review(expected_revision=0,decision='needs_review',note='Stale'),'reviewer')
        repo.review(self.c,candidate,Review(expected_revision=1,decision='needs_review',note='Reconsider scope'),'reviewer')
        history=repo.history(self.c,candidate,limit=1);self.assertEqual(history[0]['revision'],2);self.assertEqual(history[1]['review']['note'],'Need account permission')
        self.assertEqual(repo.history(self.c,candidate,before=2)[0]['revision'],1)
    def test_imported_data_and_audit_are_immutable(self):
        for table in ('source_catalog_candidates','source_catalog_imports'):
            with self.assertRaises(DBAPIError),self.c.begin_nested():self.c.execute(text(f'DELETE FROM {table}'))
        repo.review(self.c,self.item['candidate_id'],Review(expected_revision=0,decision='needs_review',note='Keep history'),'reviewer')
        with self.assertRaises(DBAPIError),self.c.begin_nested():self.c.execute(text('DELETE FROM source_catalog_reviews'))
    def test_http_read_filters_and_admin_review_do_not_enable_collection(self):
        url='/api/v1/source-catalog';identifier=str(self.item['candidate_id'])
        self.assertEqual(self.client.get(url+'/summary').json()['candidate_count'],84)
        self.assertEqual(len(self.client.get(url,params={'issue':'missing_url','limit':100}).json()['items']),7)
        body={'expected_revision':0,'decision':'needs_review','note':'HTTP independent admin review'}
        result=self.client.post(url+'/'+identifier+'/reviews',json=body,headers={'Authorization':'Bearer '+'A'*32});self.assertEqual(result.status_code,200);self.assertEqual(result.json()['revision'],1)
        self.assertEqual(self.client.post(url+'/'+identifier+'/reviews',json=body,headers={'Authorization':'Bearer '+'A'*32}).status_code,409)
        self.assertEqual(self.client.get(url+'/'+str(uuid4())).status_code,404)
        self.assertEqual(self.client.get(url+'/'+identifier+'/reviews').json()['items'][0]['review']['note'],body['note'])
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM sources')).scalar_one(),0)
