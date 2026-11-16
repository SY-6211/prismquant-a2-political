import json
import unittest
from unittest.mock import patch

import httpx

from app.probe_fast_sources import (ECB_INDEX, TARGETS, Probe, allowed_url,
                                    index_base, robots_allows, run_probe, summarize)


class Clock:
    def __init__(self): self.value=0
    def __call__(self): return self.value
    def sleep(self, seconds): self.value+=seconds


def response(request):
    if request.url.path.endswith('/robots.txt'):
        return httpx.Response(200,text='User-agent: *\nDisallow:\nCrawl-delay: 5')
    if request.headers.get('if-none-match'):
        return httpx.Response(304)
    headers={'etag':'"sample-v1"','cache-control':'max-age=60'}
    if request.url.path.endswith('versions.json'):
        return httpx.Response(200,json=[{'version':'123','hash':'abc'}],headers=headers)
    if request.url.path.endswith('metadata.json'):
        return httpx.Response(200,json={'header':['pub_timestamp','boardmember','documentTypes','publicationProperties']},headers=headers)
    if request.url.path.endswith('chunk_0.json'):
        return httpx.Response(200,json=[1790812800,'Christine Lagarde',
            ['/press/key/date/2026/html/ecb.sp261001~abc.en.html'],{'Title':'Speech'}],headers=headers)
    if request.url.path.endswith('press.html') or request.url.path=='/feed':
        return httpx.Response(200,text='<rss><channel><item><title>Christine Lagarde: Speech</title>'
            '<link>https://www.ecb.europa.eu/press/key/date/2026/html/ecb.sp261001~abc.en.html</link>'
            '<pubDate>Thu, 01 Oct 2026 13:30:00 +0000</pubDate></item></channel></rss>',headers=headers)
    return httpx.Response(200,text='<html><a href="/statuses/123">Example</a></html>',headers=headers)


class FastSourceProbeTests(unittest.TestCase):
    def test_html_relative_and_absolute_links_have_same_identity_and_external_links_excluded(self):
        result=summarize('archive_home',b'<a href="/statuses/123">a</a>'
            b'<a href="https://www.trumpstruth.org/statuses/123">b</a>'
            b'<a href="https://evil.example/statuses/456">c</a>')
        self.assertEqual(result['content_links'],1)
        self.assertEqual(result['candidate_urls'],['https://www.trumpstruth.org/statuses/123'])

    def test_allowlist_rejects_credentials_redirects_and_version_traversal(self):
        for url in ('https://evil.example/', 'https://www.ecb.europa.eu.evil.example/rss/press.html',
                    'https://secret@www.ecb.europa.eu/rss/press.html',
                    ECB_INDEX+'123/abc/../../secret',TARGETS['ecb_rss']+'?cachebust=1'):
            self.assertFalse(allowed_url(url))
        for value in ({'version':'../123','hash':'abc'},{'version':'123','hash':'a/b'}):
            with self.assertRaises(ValueError): index_base(json.dumps([value]))

    def test_robots_wildcards_end_anchor_allow_and_specific_agent(self):
        body='User-agent: *\nDisallow: /*_content.en.html$\nDisallow: /private\nAllow: /private/public\n'
        self.assertFalse(robots_allows(body,'https://www.ecb.europa.eu/a_content.en.html'))
        self.assertTrue(robots_allows(body,'https://www.ecb.europa.eu/a_content.en.html/other'))
        self.assertTrue(robots_allows(body,'https://www.ecb.europa.eu/private/public/x'))
        self.assertFalse(robots_allows(body,'https://www.ecb.europa.eu/private/x'))
        self.assertFalse(robots_allows('User-agent: *\nAllow: /\nUser-agent: PrismQuant\nDisallow: /',TARGETS['ecb_rss']))

    def test_two_rounds_conditional_requests_and_no_publication_latency_claim(self):
        requests=[]
        def handle(req): requests.append(req); return response(req)
        clock=Clock()
        with httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=False) as client:
            with patch('app.probe_fast_sources.time.perf_counter',clock):
                result=run_probe(client,2,30,sleep=clock.sleep)
        self.assertLessEqual(len(requests),16)
        self.assertTrue(any(r.headers.get('if-none-match') for r in requests))
        self.assertTrue(any(r['status']==304 for r in result['records']))
        self.assertFalse(result['models_called']); self.assertFalse(result['database_accessed'])
        self.assertFalse(result['long_observation_started'])
        self.assertEqual(result['public_to_page_status'],'not_measured')
        self.assertEqual({r['round'] for r in result['records']},{0,1,2})
        self.assertFalse(any('authorization' in r.headers or 'cookie' in r.headers for r in requests))

    def test_403_429_and_server_error_stop_host_without_retries(self):
        for code in (403,429,503):
            calls=[];clock=Clock()
            def handler(req): calls.append(req);return httpx.Response(code,text='server response')
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                probe=Probe(client,sleep=clock.sleep,counter=clock)
                probe.policies['www.ecb.europa.eu']={'allowed':True,'body':'User-agent: *\nAllow: /'}
                probe.fetch('ecb_rss',TARGETS['ecb_rss']);probe.fetch('ecb_rss',TARGETS['ecb_rss'])
            self.assertEqual(len(calls),1)

    def test_protocol_error_stops_host_and_keeps_failure_evidence(self):
        calls=[]
        def handler(req):
            calls.append(req)
            raise httpx.RemoteProtocolError('Peer closed connection')
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            probe=Probe(client)
            probe.policies['www.ecb.europa.eu']={'allowed':True,'body':'User-agent: *\nAllow: /'}
            probe.fetch('ecb_rss',TARGETS['ecb_rss'])
            probe.fetch('ecb_index_versions',TARGETS['ecb_index_versions'])
        self.assertEqual(len(calls),1)
        self.assertEqual(probe.records[0]['state'],'failed_or_unrecognized')
        self.assertEqual(probe.records[0]['error_type'],'RemoteProtocolError')

    def test_redirect_is_recorded_without_following_external_location(self):
        calls=[]
        def handler(req):calls.append(req);return httpx.Response(302,headers={'location':'https://evil.example/'})
        with httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=False) as client:
            probe=Probe(client);probe.policies['www.ecb.europa.eu']={'allowed':True,'body':'User-agent: *\nAllow: /'}
            probe.fetch('ecb_rss',TARGETS['ecb_rss'])
        self.assertEqual(len(calls),1); self.assertEqual(probe.records[0]['status'],302)

    def test_unavailable_robots_prevents_content_requests(self):
        calls=[]
        def handler(req):calls.append(req);return httpx.Response(404)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            probe=Probe(client);probe.fetch('robots','https://www.ecb.europa.eu/robots.txt',robots=True)
            probe.fetch('ecb_rss',TARGETS['ecb_rss'])
        self.assertEqual(len(calls),1)
        self.assertEqual(probe.records[-1]['state'],'skipped_robots_unavailable_or_disallowed')

    def test_crawl_delay_is_observed(self):
        clock=Clock();called=[]
        def handler(req):called.append(clock());return response(req)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            probe=Probe(client,sleep=clock.sleep,counter=clock)
            probe.fetch('robots','https://www.ecb.europa.eu/robots.txt',robots=True)
            probe.fetch('ecb_rss',TARGETS['ecb_rss'])
            probe.fetch('ecb_speeches_page',TARGETS['ecb_speeches_page'])
        self.assertEqual(called,[0,5,10])

    def test_bound_validation_and_xml_parse_failures_stay_finite(self):
        for rounds,interval in ((3,30),(1,1),(2,61)):
            with self.assertRaises(ValueError):run_probe(None,rounds,interval)
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='<broken>'))) as client:
            probe=Probe(client);probe.policies['www.ecb.europa.eu']={'allowed':True,'body':'User-agent: *\nAllow: /'}
            probe.fetch('ecb_rss',TARGETS['ecb_rss'])
        self.assertEqual(probe.records[-1]['state'],'failed_or_unrecognized')
        self.assertEqual(probe.records[-1]['error_type'],'ParseError')


if __name__=='__main__':unittest.main()
