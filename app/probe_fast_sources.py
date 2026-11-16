"""Finite, read-only source feasibility checks, independent of production collectors.

No database, Settings/.env, model key, login, or scheduler is used. A snapshot
can establish accessibility and cache behaviour, never publication latency.
"""
import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree.ElementTree import ParseError

import httpx
from bs4 import BeautifulSoup
from defusedxml.common import DefusedXmlException

from app.history_sources import ECB_INDEX, parse_ecb_chunk
from app.preview_ecb import parse_feed_items as parse_ecb_feed
from app.preview_trump import parse_feed_items as parse_archive_feed

USER_AGENT = 'PrismQuant-Source-Feasibility/0.1 (academic project; bounded read-only check)'
LIMIT = 8 * 1024 * 1024
TARGETS = {
    'ecb_rss': 'https://www.ecb.europa.eu/rss/press.html',
    'ecb_speeches_page': 'https://www.ecb.europa.eu/press/pubbydate/html/index.en.html?name_of_publication=Speech',
    'ecb_index_versions': ECB_INDEX + 'versions.json',
    'archive_rss': 'https://www.trumpstruth.org/feed',
    'archive_home': 'https://www.trumpstruth.org/',
}
HEADER_NAMES = ('date', 'age', 'cache-control', 'expires', 'etag', 'last-modified',
                'content-type', 'vary', 'retry-after', 'location')


def utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def allowed_url(url):
    if url in TARGETS.values():
        return True
    if url in {f'https://{host}/robots.txt' for host in ('www.ecb.europa.eu', 'www.trumpstruth.org')}:
        return True
    return bool(re.fullmatch(re.escape(ECB_INDEX) + r'\d+/[A-Za-z0-9_-]+/(?:metadata\.json|data/0/chunk_0\.json)', url))


def robots_allows(body, url):
    """Support the wildcard/$ rules used by ECB, unlike RobotFileParser.can_fetch.

    Most-specific UA groups take precedence. Longest matching rule wins;
    equally specific Allow beats Disallow. This is an access hint, not a license.
    """
    groups, agents, rules = [], [], []
    for line in body.splitlines() + ['User-agent: __end__']:
        line = line.split('#', 1)[0].strip()
        if ':' not in line:
            continue
        key, value = [part.strip() for part in line.split(':', 1)]
        if key.lower() == 'user-agent':
            if rules:
                groups.append((agents, rules)); agents, rules = [], []
            agents.append(value.lower())
        elif key.lower() in ('allow', 'disallow') and agents:
            rules.append((key.lower(), value))
    matches = [(max(len(a) if a != '*' else 0 for a in aa if a == '*' or a in USER_AGENT.lower()), rr)
               for aa, rr in groups if any(a == '*' or a in USER_AGENT.lower() for a in aa)]
    if not matches:
        return True
    specificity = max(pair[0] for pair in matches)
    path = urlsplit(url).path + ('?' + urlsplit(url).query if urlsplit(url).query else '')
    decisions = []
    for score, entries in matches:
        if score != specificity:
            continue
        for kind, pattern in entries:
            if not pattern:
                continue
            anchored = pattern.endswith('$')
            pattern = pattern[:-1] if anchored else pattern
            expression = '^' + re.escape(pattern).replace(r'\*', '.*') + ('$' if anchored else '')
            if re.search(expression, path):
                decisions.append((len(pattern.replace('*', '')), kind == 'allow'))
    return max(decisions)[1] if decisions else True


def index_base(content):
    values = json.loads(content)
    first = values[0]
    if not re.fullmatch(r'\d+', first['version']) or not re.fullmatch(r'[A-Za-z0-9_-]+', first['hash']):
        raise ValueError('Invalid index version')
    return ECB_INDEX + first['version'] + '/' + first['hash'] + '/'


def summarize(key, body):
    if key in ('ecb_rss', 'archive_rss'):
        items = parse_ecb_feed(body) if key == 'ecb_rss' else parse_archive_feed(body)
        own = [item for item in items if (item.get('title') or '').startswith('Christine Lagarde:')] if key == 'ecb_rss' else items
        return {'items': len(items), 'target_candidates': len(own),
                'candidate_urls': [i['url'] for i in own if i.get('url')],
                'latest_reported_published_at': own[0].get('published_at') if own else None,
                'publication_latency': 'not_measured'}
    if key in ('archive_home', 'ecb_speeches_page'):
        soup = BeautifulSoup(body, 'html.parser')
        paths = [a.get('href', '') for a in soup.find_all('a', href=True)]
        pattern = r'/statuses/\d+' if key == 'archive_home' else r'/press/key/date/\d{4}/html/[^?#]+\.en\.html'
        base = TARGETS[key]
        urls = set()
        for path in paths:
            candidate = urljoin(base, path)
            parsed = urlsplit(candidate)
            if (parsed.scheme == 'https' and parsed.netloc == urlsplit(base).netloc
                    and re.fullmatch(pattern, parsed.path) and not parsed.query):
                urls.add(candidate.split('#',1)[0])
        urls = sorted(urls)
        return {'content_links': len(urls), 'candidate_urls': urls,
                'publication_latency': 'not_measured'}
    return {}


class Probe:
    def __init__(self, client, *, sleep=time.sleep, counter=time.perf_counter):
        self.client, self.sleep, self.counter = client, sleep, counter
        self.last_request, self.policies, self.validators, self.halted = {}, {}, {}, set()
        self.records = []
        self.round_number = 0

    def fetch(self, key, url, *, conditional=False, robots=False):
        if not allowed_url(url):
            raise ValueError('URL is outside the finite probe allowlist')
        host = urlsplit(url).hostname
        if host in self.halted:
            return None
        policy = self.policies.get(host)
        if not robots and (not policy or not policy['allowed'] or not robots_allows(policy['body'], url)):
            self.records.append({'key': key, 'url': url, 'state': 'skipped_robots_unavailable_or_disallowed'})
            return None
        delay = max(5.0, policy.get('delay', 5.0)) if policy else 5.0
        if host in self.last_request:
            self.sleep(max(0, delay - (self.counter() - self.last_request[host])))
        headers = self.validators.get(url, {}) if conditional else {}
        record = {'key': key, 'url': url, 'round':self.round_number,
                  'started_at': utc(), 'conditional': bool(headers)}
        started = self.counter()
        self.last_request[host] = started
        body = bytearray()
        try:
            # No redirect following, credential headers, query invention or retries.
            with self.client.stream('GET', url, headers=headers) as response:
                record.update(status=response.status_code,
                              headers={k: response.headers[k][:1024] for k in HEADER_NAMES if k in response.headers})
                if response.status_code in (401, 403, 429) or response.status_code >= 500:
                    self.halted.add(host)
                for chunk in response.iter_bytes(65536):
                    body.extend(chunk)
                    if len(body) > LIMIT:
                        raise ValueError('Body limit exceeded')
                record.update(state='received', bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
                if response.status_code == 200:
                    etag, modified = response.headers.get('etag'), response.headers.get('last-modified')
                    self.validators[url] = ({'If-None-Match': etag} if etag else
                                            {'If-Modified-Since': modified} if modified else {})
            if record['status'] == 200 and not robots:
                record['summary'] = summarize(key, bytes(body))
            if robots:
                text = body.decode('utf-8', errors='replace')
                parser = RobotFileParser(); parser.parse(text.splitlines())
                allowed = record['status'] == 200
                self.policies[host] = {'allowed': allowed, 'body': text if allowed else '',
                                       'delay': parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 5}
                record['robots_text'] = text[:16000] if allowed else None
                if b'unavailable in your area' in body.lower():
                    record['region_unavailable_notice'] = True
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, ParseError, DefusedXmlException) as error:
            record['state'] = 'failed_or_unrecognized'
            record['error_type'] = type(error).__name__
            self.halted.add(host)
        finally:
            record.update(finished_at=utc(), request_and_processing_ms=round((self.counter()-started)*1000, 3))
            self.records.append(record)
        return bytes(body) if record.get('status') == 200 and record['state'] == 'received' else None


def run_probe(client, rounds=1, interval=30, *, sleep=time.sleep):
    if rounds not in (1, 2) or not 30 <= interval <= 60:
        raise ValueError('Only 1–2 rounds, 30–60 seconds apart are supported')
    probe = Probe(client, sleep=sleep)
    started = utc()
    for host in ('www.ecb.europa.eu', 'www.trumpstruth.org'):
        probe.fetch('robots', f'https://{host}/robots.txt', robots=True)
    for round_no in range(rounds):
        probe.round_number = round_no + 1
        if round_no:
            sleep(interval)
        for key, url in TARGETS.items():
            body = probe.fetch(key, url, conditional=bool(round_no))
            if key == 'ecb_index_versions' and body:
                try:
                    base = index_base(body)
                    metadata = probe.fetch('ecb_index_metadata', base+'metadata.json', conditional=bool(round_no))
                    if metadata:
                        header = json.loads(metadata)['header']
                        chunk = probe.fetch('ecb_index_chunk', base+'data/0/chunk_0.json', conditional=bool(round_no))
                        if chunk:
                            entries, stamps = parse_ecb_chunk(chunk, header)
                            probe.records[-1]['summary'] = {'items':len(stamps),'target_candidates':len(entries),
                                'candidate_urls':[entry['url'] for entry in entries],
                                'latest_reported_published_at': entries[0]['published_at'] if entries else None,
                                'publication_latency':'not_measured'}
                except (ValueError, KeyError, TypeError, IndexError):
                    probe.records[-1]['index_state'] = 'unrecognized_no_more_index_requests'
    return {'mode':'finite_source_feasibility_probe', 'probe_version':'fast_sources_v1',
            'started_at':started,'finished_at':utc(),
            'rounds':rounds,'database_accessed':False,'production_changed':False,'models_called':False,
            'long_observation_started':False,'public_to_page_status':'not_measured',
            'records':probe.records,
            'notes':['这是可达性和缓存快照，不是24–48小时延迟实测。',
                     '同一主机请求至少相隔5秒并参考robots crawl-delay；权限/限流/服务错误停止该主机。',
                     '首次快照中的既有事件没有本项目的首次公开观测时间。']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='有限只读来源验证；不入库、不调用模型、不启用监控')
    parser.add_argument('--rounds', type=int, choices=(1,2), default=1)
    parser.add_argument('--interval', type=int, choices=range(30,61), default=30)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    # These few requests are deliberately spaced out. Do not reuse idle sockets:
    # the tested ECB edge sometimes closed a pooled connection without a reply.
    with httpx.Client(timeout=httpx.Timeout(15),follow_redirects=False,trust_env=False,
                      limits=httpx.Limits(max_keepalive_connections=0),
                      headers={'User-Agent':USER_AGENT}) as client:
        result=run_probe(client,args.rounds,args.interval)
    encoded=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(encoded+'\n',encoding='utf-8')
    else:
        print(encoded)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
