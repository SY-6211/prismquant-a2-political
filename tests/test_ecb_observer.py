import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from app.observe_ecb import ECBObserver, Options, RSS, VERSIONS, ROBOTS


OLD = 'https://www.ecb.europa.eu/press/key/date/2026/html/ecb.sp261001~old.en.html'
NEW = 'https://www.ecb.europa.eu/press/key/date/2026/html/ecb.sp261105~new.en.html'
OTHER = 'https://www.ecb.europa.eu/press/key/date/2026/html/ecb.sp261105~other.en.html'
HEADER = ['pub_timestamp','boardmember','documentTypes','publicationProperties']


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += seconds
    def now(self): return datetime(2026,11,5,tzinfo=timezone.utc)+timedelta(seconds=self.value)


def rss(urls):
    return ('<rss><channel>'+''.join('<item><title>Christine Lagarde: Same title</title>'
            f'<link>{u}</link><pubDate>Thu, 05 Nov 2026 00:00:00 +0000</pubDate></item>'
            for u in urls)+'</channel></rss>')


def chunk(urls):
    values = []
    for i,url in enumerate(urls):
        values += [1793836800-i,'Christine Lagarde',[url],{'Title':'Same title'}]
    return values


def speech():
    return ('<html><meta property="article:published_time" content="2026-11-05"/>'
            '<main><div class="title"><li>SPEECH</li></div><h1>Same title</h1>'
            '<section class="section"><p class="ecb-pressContentSubtitle">'
            'Speech by Christine Lagarde, President of the ECB</p><p>'+
            'An official example speech with sufficient body text for attribution checking. '*3+
            '</p></section></main></html>')


class ECBObserverTests(unittest.TestCase):
    def make(self, directory, handler=None, options=None):
        clock = Clock(); calls = []; owner = {}
        def transport(req):
            calls.append((clock(), req))
            number = owner['observer'].round
            if handler:
                custom = handler(req, number)
                if custom is not None: return custom
            if str(req.url) == ROBOTS:
                return httpx.Response(200,text='User-agent: *\nDisallow:\nCrawl-delay: 5')
            headers = {'ETag':'"v1"','Cache-Control':'max-age=60'}
            if req.headers.get('if-none-match'):
                return httpx.Response(304,headers=headers)
            if str(req.url) == RSS:
                return httpx.Response(200,text=rss([OLD]),headers=headers)
            if str(req.url) == VERSIONS:
                return httpx.Response(200,json=[{'version':'1','hash':'abc'}],headers=headers)
            if req.url.path.endswith('metadata.json'):
                return httpx.Response(200,json={'header':HEADER,'sort':['!pub_timestamp'],
                                               'chunk_size':250,'total_records':1},headers=headers)
            if req.url.path.endswith('chunk_0.json'):
                return httpx.Response(200,json=chunk([OLD]),headers=headers)
            return httpx.Response(200,text=speech(),headers=headers)
        client = httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False)
        self.addCleanup(client.close)
        observer = ECBObserver(client, Path(directory)/'run', options or Options(),
                               counter=clock,sleep=clock.sleep,now=clock.now)
        owner['observer'] = observer
        return observer, clock, calls

    def test_three_rounds_revalidate_without_fetching_unchanged_chunks_or_counting_old_news(self):
        with tempfile.TemporaryDirectory() as folder:
            observer, _, calls = self.make(folder)
            report = observer.run()
            self.assertEqual(report['state'],'completed')
            self.assertEqual(report['completed_rounds'],3)
            self.assertEqual(report['requests'],9)
            self.assertEqual(report['baseline_candidates'],1)
            self.assertEqual(report['new_observed_candidates'],0)
            self.assertEqual(report['paired_new_candidates'],0)
            self.assertEqual(report['sample_conclusion'],'insufficient_new_event_samples')
            self.assertEqual(report['publication_latency'],'not_measured')
            self.assertEqual(report['public_to_browser_latency'],'not_measured')
            self.assertEqual(sum(r.url.path.endswith('chunk_0.json') for _,r in calls),1)
            self.assertTrue(any(r.headers.get('if-none-match') for _,r in calls))
            self.assertFalse(any('cookie' in r.headers or 'authorization' in r.headers for _,r in calls))
            for (t,_),(u,_) in zip(calls,calls[1:]): self.assertGreaterEqual(u-t,5)
            for endpoint in (RSS,VERSIONS):
                stamps=[t for t,r in calls if str(r.url)==endpoint]
                self.assertTrue(all(b-a >= 60 for a,b in zip(stamps,stamps[1:])))
            final=json.loads((observer.directory/'report.json').read_text(encoding='utf8'))
            self.assertEqual(final['state'],'completed')
            self.assertEqual(json.loads((observer.directory/'heartbeat.json').read_text())['phase'],'finished')

    def test_new_index_event_then_rss_uses_native_url_identity_not_title(self):
        def handler(req, number):
            if str(req.url) == VERSIONS and number >= 2:
                return httpx.Response(200,json=[{'version':'2','hash':'def'}],headers={'ETag':'"v2"'})
            if '/2/def/data/' in req.url.path:
                return httpx.Response(200,json=chunk([NEW,OTHER,OLD]))
            if str(req.url) == RSS and number >= 3:
                return httpx.Response(200,text=rss([NEW,OTHER,OLD]),headers={'ETag':'"r2"'})
        with tempfile.TemporaryDirectory() as folder:
            observer, _, calls = self.make(folder,handler)
            report=observer.run()
            self.assertEqual(report['new_observed_candidates'],2)
            self.assertEqual(report['paired_new_candidates'],2)
            self.assertEqual(report['detail_checks'],2)
            self.assertTrue(all(e['detail']['status']=='verified_page_attribution' for e in report['new_events']))
            self.assertTrue(all(c['rss_minus_index_first_seen_seconds'] > 0 for c in report['comparisons']))
            self.assertTrue(all(e['first_seen']['index']['last_absent_view_request_started_at'] for e in report['new_events']))
            self.assertTrue(all(e['first_seen']['index']['absence_scope']=='returned_window_only_not_origin_publication' for e in report['new_events']))
            self.assertEqual(sum(str(r.url)==NEW for _,r in calls),1)

    def test_unchanged_version_200_does_not_fetch_metadata_again(self):
        def handler(req, _):
            if str(req.url)==VERSIONS:
                return httpx.Response(200,json=[{'version':'1','hash':'abc'}])
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls=self.make(folder,handler)
            observer.run()
            self.assertEqual(sum(r.url.path.endswith('metadata.json') for _,r in calls),1)

    def test_failure_during_new_version_does_not_commit_old_window_as_new_version(self):
        metadata_calls=[]
        def handler(req,number):
            if str(req.url)==VERSIONS and number >= 2:
                return httpx.Response(200,json=[{'version':'2','hash':'def'}])
            if '/2/def/metadata.json' in req.url.path:
                metadata_calls.append(number)
                if number==2: return httpx.Response(503)
            if '/2/def/data/' in req.url.path:
                return httpx.Response(200,json=chunk([NEW,OLD]))
        with tempfile.TemporaryDirectory() as folder:
            observer,_,_=self.make(folder,handler,Options(duration=400))
            report=observer.run()
            self.assertEqual(metadata_calls,[2,3])
            self.assertEqual(report['new_observed_candidates'],1)
            self.assertEqual(report['state'],'completed_with_gaps')
            self.assertEqual(report['new_events'][0]['first_seen']['index']['gap_since_previous_view'],True)

    def test_first_success_after_failed_initial_rss_is_baseline(self):
        def handler(req,number):
            if str(req.url)==RSS and number==1: return httpx.Response(503)
        with tempfile.TemporaryDirectory() as folder:
            observer,_,_=self.make(folder,handler,Options(duration=400))
            report=observer.run()
            self.assertEqual(report['new_observed_candidates'],0)
            self.assertTrue(observer.candidates[OLD]['first_seen']['rss']['baseline'])
            self.assertTrue(report['failures_and_coverage_gaps'])

    def test_access_denial_and_rate_limit_stop_without_retry_and_keep_headers(self):
        for code in (401,403,429):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                def handler(req,_):
                    if str(req.url)==RSS: return httpx.Response(code,headers={'Retry-After':'120'})
                observer,_,calls=self.make(folder,handler)
                report=observer.run()
                self.assertEqual(len(calls),2)
                self.assertEqual(report['stop_reason'],'rate_limited' if code==429 else 'access_denied')
                records=[json.loads(s) for s in (observer.directory/'requests.jsonl').read_text().splitlines()]
                self.assertEqual(records[-1]['headers']['retry-after'],'120')

    def test_redirect_is_not_followed(self):
        def handler(req,_):
            if str(req.url)==RSS: return httpx.Response(302,headers={'Location':'https://untrusted.example/'})
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls=self.make(folder,handler)
            self.assertEqual(observer.run()['stop_reason'],'redirect_or_http_error')
            self.assertEqual(len(calls),2)

    def test_stop_file_interrupts_wait_and_prevents_further_network(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,clock,calls=self.make(folder)
            def sleep(seconds):
                clock.sleep(seconds)
                if clock() >= 35: observer.stop_file.touch()
            observer.sleep=sleep
            report=observer.run()
            self.assertEqual(report['stop_reason'],'stop_file')
            self.assertEqual(len(calls),5)
            self.assertLess(report['elapsed_seconds'],40)

    def test_keyboard_interrupt_finalises_both_report_and_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,_,_=self.make(folder)
            observer.sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
            self.assertEqual(observer.run()['state'],'interrupted')
            self.assertEqual(json.loads((observer.directory/'heartbeat.json').read_text())['state'],'interrupted')

    def test_request_and_duration_limits_are_hard_bounds(self):
        for options,reason in ((Options(max_requests=1),'request_limit'),(Options(duration=10),'duration_limit')):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                observer,_,calls=self.make(folder,options=options)
                self.assertEqual(observer.run()['stop_reason'],reason)
                self.assertLessEqual(len(calls),2)

    def test_robots_disallow_and_unavailable_prevent_source_access(self):
        for code,body in ((200,'User-agent: *\nDisallow: /rss/'),(404,''),(200,'not a robots policy')):
            with self.subTest(code=code,body=body),tempfile.TemporaryDirectory() as folder:
                def handler(req,_):
                    if str(req.url)==ROBOTS:return httpx.Response(code,text=body)
                observer,_,calls=self.make(folder,handler)
                report=observer.run()
                self.assertEqual(report['state'],'stopped')
                self.assertEqual(len(calls),1)

    def test_robots_longer_delay_and_last_modified_validator(self):
        def handler(req,_):
            if str(req.url)==ROBOTS:return httpx.Response(200,text='User-agent: *\nDisallow:\nCrawl-delay: 8')
            if str(req.url)==RSS:
                if req.headers.get('if-modified-since'):return httpx.Response(304)
                return httpx.Response(200,text=rss([OLD]),headers={'Last-Modified':'Thu, 05 Nov 2026 00:00:00 GMT'})
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls=self.make(folder,handler)
            observer.run()
            self.assertTrue(any(r.headers.get('if-modified-since') for _,r in calls))
            self.assertTrue(all(b-a >= 8 for (a,_),(b,_) in zip(calls,calls[1:])))

    def test_transport_failure_backoff_is_recorded_and_does_not_make_new_sample(self):
        def handler(req,number):
            if str(req.url)==RSS and number==1:raise httpx.RemoteProtocolError('closed')
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls=self.make(folder,handler,Options(duration=400))
            report=observer.run()
            self.assertEqual(report['state'],'completed_with_gaps')
            self.assertEqual(report['new_observed_candidates'],0)
            self.assertEqual(report['failures_and_coverage_gaps'][0]['error_type'],'RemoteProtocolError')
            self.assertGreaterEqual(calls[2][0]-calls[1][0],60)

    def test_invalid_xml_index_schema_and_304_without_cache_stop_explicitly(self):
        for mode in ('xml','schema','304'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                def handler(req,_):
                    if mode=='xml' and str(req.url)==RSS:return httpx.Response(200,text='<broken>')
                    if mode=='304' and str(req.url)==RSS:return httpx.Response(304)
                    if mode=='schema' and req.url.path.endswith('metadata.json'):return httpx.Response(200,json={})
                observer,_,_=self.make(folder,handler)
                report=observer.run()
                self.assertIn(report['stop_reason'],('schema_changed','response_unrecognized'))

    def test_invalid_options_and_existing_evidence_directory_are_rejected(self):
        for options in (Options(interval=30),Options(rounds=31),Options(duration=1801),Options(max_details=11)):
            with self.assertRaises(ValueError):options.validate()
        with tempfile.TemporaryDirectory() as folder:
            self.make(folder)
            with self.assertRaises(FileExistsError):self.make(folder)

    def test_observation_never_constructs_settings_or_database_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,_,_=self.make(folder)
            with patch('app.core.config.Settings.__init__', side_effect=AssertionError('Settings forbidden')), \
                 patch('app.core.database.create_read_engine', side_effect=AssertionError('DB forbidden')):
                self.assertEqual(observer.run()['state'],'completed')

    def test_failed_detail_is_not_left_as_an_active_attempt(self):
        def handler(req,number):
            if str(req.url)==VERSIONS and number>=2:
                return httpx.Response(200,json=[{'version':'2','hash':'def'}])
            if '/2/def/data/' in req.url.path:
                return httpx.Response(200,json=chunk([NEW,OLD]))
            if str(req.url)==NEW:return httpx.Response(503)
        with tempfile.TemporaryDirectory() as folder:
            observer,_,_=self.make(folder,handler,Options(duration=400))
            report=observer.run()
            self.assertEqual(report['new_events'][0]['detail']['status'],'request_failed_with_backoff')
            self.assertEqual(report['state'],'completed_with_gaps')

    def test_untrusted_urls_and_oversize_content_are_not_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls=self.make(folder)
            for url in ('https://evil.example/',RSS+'?cachebust=1',
                        'https://secret@www.ecb.europa.eu/rss/press.html'):
                with self.assertRaises(ValueError):observer.fetch('rss',url)
            self.assertEqual(len(calls),0)
            with patch('app.observe_ecb.MAX_BYTES',64):
                self.assertEqual(observer.run()['stop_reason'],'response_unrecognized')

    def test_detail_check_runs_after_both_discovery_channels(self):
        def handler(req,number):
            if str(req.url)==VERSIONS and number>=2:
                return httpx.Response(200,json=[{'version':'2','hash':'def'}])
            if '/2/def/data/' in req.url.path:
                return httpx.Response(200,json=chunk([NEW,OLD]))
            if str(req.url)==RSS and number>=2:
                return httpx.Response(200,text=rss([NEW,OLD]))
        with tempfile.TemporaryDirectory() as folder:
            observer,_,calls=self.make(folder,handler)
            report=observer.run()
            self.assertEqual(report['paired_new_candidates'],1)
            urls=[str(r.url) for _,r in calls]
            second_rss=urls.index(RSS,urls.index(RSS)+1)
            self.assertGreater(urls.index(NEW),second_rss)
            self.assertEqual(urls.count(NEW),1)


if __name__=='__main__':unittest.main()
