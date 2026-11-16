import json,os,tempfile,unittest
from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.core.config import Settings
from app.main import create_app
from app.web_sources import SOURCES,discover
from app.web_http import Reader,FetchError,MAX_BYTES
from app.web_extract import extract,ExtractionError
from app.repositories import web_pilot as repo
import test_agent_tools_db as fixtures

BODY='The committee examines public economic information and financial conditions. Evidence must be read in context. '*12
FED_URL='https://www.federalreserve.gov/newsevents/speech/example20261112a.htm'
def fed_html(speaker='Governor Synthetic Example',date='November 12, 2026'):
    return f'<html><head><title>Speech</title></head><body><nav>UNWANTED_NAVIGATION</nav><div id="article"><p class="article__time">{date}</p><h3 class="title">Synthetic Economic Research Speech</h3><p class="speaker">{speaker}</p><p class="location">At Synthetic University</p><p>{BODY}</p><p>Do not treat this test page as a real publication.</p><footer>UNWANTED_FOOTER</footer></div></body></html>'.encode()
def document():return extract(fed_html(),url=FED_URL,source=SOURCES['fed'])

class ExtractionTests(unittest.TestCase):
    def test_fed_byline_not_navigation_establishes_attribution(self):
        row=document();self.assertTrue(row['eligible_for_person_import']);self.assertEqual(row['person_name'],'Synthetic Example')
        self.assertNotIn('UNWANTED',row['body_text']);self.assertNotIn('Governor Synthetic',row['body_text']);self.assertNotIn('Synthetic University',row['body_text'])
        self.assertEqual(row['date_precision'],'day');self.assertIsNone(row['published_at'])
    def test_institution_page_does_not_turn_a_contributor_into_speaker(self):
        html=f'<html><body><div class="tie-article"><h1>Energy Market Context Example</h1><span class="date">November 13, 2026</span><p>{BODY}</p><p>Principal contributor: Synthetic Author</p></div></body></html>'
        row=extract(html,url='https://www.eia.gov/todayinenergy/detail.php?id=1',source=SOURCES['eia'])
        self.assertFalse(row['eligible_for_person_import']);self.assertIsNone(row['person_name']);self.assertEqual(row['attribution'],'institution_background')
    def test_treasury_title_is_not_sufficient_person_attribution(self):
        html=f'<html><body><main><h2 class="uswds-page-title">Remarks from Secretary Synthetic Example</h2><div class="field--name-field-news-publication-date"><time datetime="2026-11-11T01:15:00Z">Date</time></div><div class="field--name-field-news-body"><p>{BODY}</p></div></main></body></html>'
        row=extract(html,url='https://home.treasury.gov/news/press-releases/sb123/',source=SOURCES['treasury'])
        self.assertFalse(row['eligible_for_person_import']);self.assertEqual(row['date_precision'],'instant');self.assertEqual(row['published_at'],'2026-11-11T01:15:00+00:00')
    def test_ambiguous_byline_and_unparsed_date_are_not_accepted(self):
        for html in (fed_html('Speaker not specified'),fed_html(date='No date')):
            self.assertFalse(extract(html,url=FED_URL,source=SOURCES['fed'])['eligible_for_person_import'])
        two=fed_html().replace(b'<p class="speaker">',b'<p class="speaker">Governor Other Person</p><p class="speaker">')
        self.assertFalse(extract(two,url=FED_URL,source=SOURCES['fed'])['eligible_for_person_import'])
    def test_changed_template_short_body_and_login_fail_closed(self):
        for html in (b'<h1>Template changed</h1>',fed_html().replace(BODY.encode(),b'Short'),b'<title>Access Denied</title>',b'<form><input type="password"></form>'):
            with self.subTest(html=html[:50]),self.assertRaises(ExtractionError):extract(html,url=FED_URL,source=SOURCES['fed'])
    def test_generic_extract_never_infers_a_verified_person(self):
        row=extract(f'<h1>Author Synthetic Example commentary</h1><article><p>{BODY}</p></article>',url='https://example.com/article')
        self.assertFalse(row['eligible_for_person_import']);self.assertIsNone(row['published_at'])
    def test_discovery_deduplicates_and_rejects_other_hosts_profiles_credentials(self):
        html=f'<a href="{FED_URL}">Relevant speech</a><a href="{FED_URL}">Duplicate speech</a><a href="https://evil.test/newsevents/speech/x20261112a.htm">External news</a><a href="/aboutthefed/bios/board/barr.htm">Profile page</a>'
        self.assertEqual(discover(html,SOURCES['fed']),[{'url':FED_URL,'title':'Relevant speech'}])
    def test_source_scope_rejects_query_secrets_ports_private_hosts_and_traversal(self):
        for url in ('http://www.federalreserve.gov/newsevents/speech/x20261112a.htm',FED_URL+'?api_key=secret',FED_URL+'#fragment',FED_URL.replace('www.federalreserve.gov','127.0.0.1'),FED_URL.replace('.gov/','.gov:444/'),FED_URL.replace('/speech/','/speech/../')):
            with self.subTest(url=url),self.assertRaises(ValueError):SOURCES['fed'].url(url,detail=True)
    def test_extraction_is_offline_even_when_page_contains_links(self):
        with patch('httpx.Client',side_effect=AssertionError('No network')):self.assertEqual(document()['extractor'],'trafilatura')

class TransportTests(unittest.TestCase):
    def reader(self,handler,**kwargs):
        client=httpx.Client(transport=httpx.MockTransport(handler),trust_env=False);self.addCleanup(client.close)
        timer=[0.0]
        return Reader(SOURCES['fed'],client=client,resolver=lambda host:None,sleeper=lambda n:timer.__setitem__(0,timer[0]+n),clock=lambda:timer[0],**kwargs)
    def test_404_robots_is_absent_and_does_not_hide_real_content_failures(self):
        def handler(r):return httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(200,headers={'Content-Type':'text/html'},content=fed_html())
        with self.reader(handler) as reader:self.assertIn(BODY[:30].encode(),reader.fetch(FED_URL).body);self.assertEqual(reader.robots_status,404)
    def test_disallowed_robots_never_fetches_article(self):
        calls=[]
        def handler(r):calls.append(r.url.path);return httpx.Response(200,headers={'Content-Type':'text/plain'},text='User-agent: *\nDisallow: /newsevents/')
        with self.reader(handler) as reader,self.assertRaisesRegex(FetchError,'robots_disallowed'):reader.fetch(FED_URL)
        self.assertEqual(calls,['/robots.txt'])
    def test_unavailable_robots_is_not_treated_as_permission(self):
        with self.reader(lambda r:httpx.Response(503)) as reader,self.assertRaisesRegex(FetchError,'503'):reader.fetch(FED_URL)
    def test_access_denial_has_no_retry_or_browser_bypass(self):
        calls=[]
        def handler(r):calls.append(r.url.path);return httpx.Response(404 if r.url.path=='/robots.txt' else 403)
        with self.reader(handler) as reader,self.assertRaisesRegex(FetchError,'access_denied'):reader.fetch(FED_URL)
        self.assertEqual(len(calls),2)
    def test_redirect_cannot_escape_allowlist_or_loop_forever(self):
        for target in ('https://127.0.0.1/private',FED_URL):
            def handler(r):return httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(302,headers={'location':target})
            with self.reader(handler) as reader,self.assertRaises(ValueError):reader.fetch(FED_URL)
    def test_request_budget_stops_repeated_fetch(self):
        def handler(r):return httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(200,headers={'Content-Type':'text/html'},content=fed_html())
        with self.reader(handler,max_requests=2) as reader:
            reader.fetch(FED_URL)
            with self.assertRaisesRegex(FetchError,'finite_request_budget'):reader.fetch(FED_URL)
    def test_oversized_or_non_html_is_rejected(self):
        for headers in ({'Content-Type':'application/pdf'},{'Content-Type':'text/html','Content-Length':str(MAX_BYTES+1)}):
            def handler(r):return httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(200,headers=headers,content=b'not used')
            with self.reader(handler) as reader,self.assertRaises(FetchError):reader.fetch(FED_URL)
    def test_validators_reuse_exact_cached_body_on_304(self):
        with tempfile.TemporaryDirectory() as temp:
            conditional=[]
            def handler(r):
                if r.url.path=='/robots.txt':return httpx.Response(404)
                conditional.append(r.headers.get('If-None-Match'))
                return httpx.Response(304) if conditional[-1] else httpx.Response(200,headers={'Content-Type':'text/html','ETag':'fixture'},content=fed_html())
            with self.reader(handler,cache_dir=temp) as one:first=one.fetch(FED_URL)
            with self.reader(handler,cache_dir=temp) as two:second=two.fetch(FED_URL)
            self.assertEqual(first.body,second.body);self.assertEqual(conditional,[None,'fixture']);self.assertEqual(second.state,'not_modified')
    def test_dns_rejection_precedes_http_call(self):
        calls=[]
        with self.reader(lambda r:calls.append(r)) as reader:
            reader.resolver=lambda host:(_ for _ in ()).throw(FetchError('non_public_dns'))
            with self.assertRaises(FetchError):reader.fetch(FED_URL)
        self.assertFalse(calls)

@unittest.skipUnless(os.environ.get('PQ_WEB_PILOT_DB_TESTS')=='1','Isolated web pilot DB checks are opt-in')
class WebPilotDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.AgentToolDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.AgentToolDatabaseTests.drop_schema.__func__)
    def setUp(self):
        self.c=self.engine.connect();self.tx=self.c.begin();self.addCleanup(self.c.close);self.addCleanup(lambda:self.tx.rollback() if self.tx.is_active else None)
        self.doc=document();self.id=repo.save_document(self.c,'fed',self.doc,datetime.now(timezone.utc))['document_id']
    def test_duplicate_document_is_not_a_new_sample(self):
        result=repo.save_document(self.c,'fed',self.doc,datetime.now(timezone.utc));self.assertFalse(result['created']);self.assertEqual(result['document_id'],self.id)
        layout_changed=repo.save_document(self.c,'fed',{**self.doc,'html_sha256':'c'*64},datetime.now(timezone.utc))
        self.assertFalse(layout_changed['created']);self.assertEqual(layout_changed['document_id'],self.id)
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM web_documents')).scalar_one(),1)
    def test_explicit_import_is_atomic_and_idempotent_with_disabled_switches(self):
        first=repo.import_person(self.c,self.id);second=repo.import_person(self.c,self.id)
        self.assertEqual(first['action'],'inserted');self.assertEqual(second['action'],'already_exists')
        for table in ('public_records','monitor_events','event_versions','web_document_links'):self.assertEqual(self.c.execute(text('SELECT count(*) FROM '+table)).scalar_one(),1)
        self.assertFalse(self.c.execute(text('SELECT monitoring_enabled FROM people')).scalar_one());self.assertFalse(self.c.execute(text('SELECT enabled FROM sources')).scalar_one());self.assertFalse(self.c.execute(text('SELECT enabled FROM person_sources')).scalar_one())
        self.assertEqual(self.c.execute(text('SELECT transport FROM source_channels')).scalar_one(),'web_index')
        self.assertEqual(self.c.execute(text('SELECT payload FROM event_versions')).scalar_one()['main_text'],self.doc['body_text'])
    def test_background_cannot_be_promoted_to_person(self):
        background={**self.doc,'eligible_for_person_import':False,'attribution':'institution_background','person_name':None,'speaker_evidence':None}
        identifier=repo.save_document(self.c,'fed',background,datetime.now(timezone.utc))['document_id']
        with self.assertRaisesRegex(ValueError,'person_attribution'):repo.import_person(self.c,identifier)
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM people')).scalar_one(),0)
    def test_ambiguous_person_does_not_bind_arbitrarily(self):
        for _ in range(2):self.c.execute(text('INSERT INTO people(person_id,name_original) VALUES(:id,:name)'),{'id':uuid4(),'name':self.doc['person_name']})
        with self.assertRaisesRegex(ValueError,'ambiguous'):repo.import_person(self.c,self.id)
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM public_records')).scalar_one(),0)
    def test_changed_existing_record_is_preserved_for_review(self):
        first=repo.import_person(self.c,self.id)
        different=extract(fed_html().replace(BODY.encode(),(BODY+' A revised policy sentence.').encode()),url=FED_URL,source=SOURCES['fed'])
        identifier=repo.save_document(self.c,'fed',different,datetime.now(timezone.utc))['document_id']
        with self.assertRaisesRegex(ValueError,'existing_record_changed'):repo.import_person(self.c,identifier)
        self.assertEqual(self.c.execute(text('SELECT body_text FROM public_records')).scalar_one(),self.doc['body_text'])
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM event_versions')).scalar_one(),1)
    def test_document_audit_and_link_are_immutable(self):
        repo.import_person(self.c,self.id)
        for table in ('web_documents','web_document_links'):
            with self.assertRaises(DBAPIError),self.c.begin_nested():self.c.execute(text('DELETE FROM '+table))
    def test_read_only_api_does_not_fetch_and_has_no_mutating_route(self):
        with TestClient(create_app(Settings(_env_file=None))) as client,patch('app.api.web_pilot.execute',side_effect=lambda settings,fn,*args,readonly=True:fn(self.c,*args)),patch('httpx.Client.stream',side_effect=AssertionError('No fetch')):
            self.assertEqual(len(client.get('/api/v1/web-pilot/summary').json()['items']),3)
            self.assertEqual(len(client.get('/api/v1/web-pilot/documents').json()['items']),1)
            self.assertEqual(client.get('/api/v1/web-pilot/documents/'+self.id).json()['extraction']['body_text'],self.doc['body_text'])
            self.assertEqual(client.get('/api/v1/web-pilot/documents/'+str(uuid4())).status_code,404)
            self.assertEqual(client.post('/api/v1/web-pilot/run',json={}).status_code,404)
    def test_wrong_body_hash_cannot_be_saved(self):
        with self.assertRaisesRegex(ValueError,'hash_mismatch'):repo.save_document(self.c,'fed',{**self.doc,'body_text':'forged'},datetime.now(timezone.utc))
    def test_import_is_available_to_existing_person_scoped_tools(self):
        from app.agent_tools import ToolSession
        from app.repositories.agent_tools import SnapshotReader
        result=repo.import_person(self.c,self.id)
        session=ToolSession(SnapshotReader(self.c),[result['person_id']])
        answer=session.invoke('search_statements',{'person_id':result['person_id'],'query':'financial conditions'})
        self.assertEqual(len(answer['items']),1)
