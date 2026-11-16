"""Bounded, allowlisted ECB HTTP with persistent validators and robots checks."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time
import math
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import text

from app.batch_ecb import SOURCE_ID, prepare_candidates
from app.history_sources import ECB_INDEX, parse_ecb_chunk
from app.preview_ecb import parse_feed_items
from app.preview_ecb_speech import speech_url
from app.probe_fast_sources import HEADER_NAMES, USER_AGENT, index_base, robots_allows
from app.repositories.ecb_discovery import ensure_state, snapshot, state

ROBOTS = 'https://www.ecb.europa.eu/robots.txt'
RSS = 'https://www.ecb.europa.eu/rss/press.html'
VERSIONS = ECB_INDEX+'versions.json'
MAX_BYTES = 8*1024*1024


def parse_index(metadata_body, chunk_body):
    meta = json.loads(metadata_body)
    if not isinstance(meta,dict):
        raise ValueError('ECB metadata must be an object')
    header = meta.get('header')
    if (not isinstance(meta.get('sort'),list) or not meta['sort'] or meta['sort'][0]!='!pub_timestamp' or type(meta.get('chunk_size')) is not int
            or not 1<=meta['chunk_size']<=1000 or type(meta.get('total_records')) is not int or meta['total_records']<1
            or not isinstance(header,list) or not all(isinstance(name,str) for name in header) or len(header)!=len(set(header))):
        raise ValueError('ECB index contract changed')
    values=json.loads(chunk_body)
    if not isinstance(values,list) or not header or len(values)%len(header) or 'pub_timestamp' not in header:
        raise ValueError('ECB chunk must be a complete flat array')
    for offset in range(header.index('pub_timestamp'),len(values),len(header)):
        stamp=values[offset]
        if type(stamp) not in (int,float) or not math.isfinite(stamp):
            raise ValueError('ECB index timestamp is not a finite number')
    entries,stamps = parse_ecb_chunk(chunk_body,header)
    if not stamps or len(stamps)>meta['chunk_size'] or stamps!=sorted(stamps,reverse=True):
        raise ValueError('ECB index window is empty or unordered')
    return entries,{'scope':'first_index_chunk_only','rows':len(stamps),'total_records':meta['total_records'],
                    'oldest_reported_at':min(stamps),'newest_reported_at':max(stamps),'publication_time_verified':False}


class ECBReader:
    def __init__(self, client, lease, *, checkpoint=None, max_requests=12):
        self.client,self.lease = client,lease
        self.checkpoint = checkpoint or (lambda:None)
        self.max_requests = max_requests
        self.requests = []
        self.policy = None
        self.base = None
        self.deadline = time.monotonic()+180

    def check_deadline(self):
        if time.monotonic()>=self.deadline:
            raise ValueError('Finite ECB round duration exceeded')

    def guard(self):
        self.check_deadline()
        self.checkpoint()
        with self.lease.connection.begin():
            self.lease.require(automatic=True)

    def cache(self,url):
        with self.lease.connection.begin():
            row = self.lease.connection.execute(text('SELECT *,clock_timestamp() AS now FROM ecb_http_cache '
                'WHERE source_id=:id AND url=:url'),{'id':SOURCE_ID,'url':url}).mappings().one_or_none()
            return dict(row) if row else None

    def allowed(self,url):
        if url in (ROBOTS,RSS,VERSIONS):
            return True
        if self.base and url in (self.base+'metadata.json',self.base+'data/0/chunk_0.json'):
            return True
        try:
            return speech_url(url)==url
        except ValueError:
            return False

    def fetch(self,url):
        if not self.allowed(url):
            raise ValueError('URL is outside ECB discovery scope')
        self.guard()
        if url!=ROBOTS and (not self.policy or not robots_allows(self.policy,url)):
            raise ValueError('Robots policy unavailable or disallows resource')
        cached = self.cache(url)
        if cached and cached['fresh_until']>cached['now']:
            self.requests.append({'url':url,'state':'fresh_cache','observed_at':cached['checked_at'].isoformat(),
                                  'body_sha256':hashlib.sha256(cached['body']).hexdigest()})
            return bytes(cached['body'])
        if sum(row['state']!='fresh_cache' for row in self.requests)>=self.max_requests:
            raise ValueError('Finite request budget exhausted')
        self.checkpoint()
        self.lease.before_request(True)
        self.check_deadline()
        headers = {}
        if cached:
            if cached['etag']: headers['If-None-Match']=cached['etag']
            elif cached['last_modified']: headers['If-Modified-Since']=cached['last_modified']
        record = {'url':url,'state':'requesting','conditional':bool(headers)}
        try:
            with self.client.stream('GET',url,headers=headers,timeout=httpx.Timeout(15,connect=10)) as response:
                record['http_status']=response.status_code
                if response.status_code!=304:
                    response.raise_for_status()
                observed = datetime.now(timezone.utc)
                public_headers = {name:response.headers[name][:1024] for name in HEADER_NAMES if name in response.headers}
                if response.status_code==304:
                    if not cached or not headers:
                        raise ValueError('304 without cached body and validator')
                    body = bytes(cached['body'])
                    public_headers = {**cached['headers'],**public_headers}
                    record['state']='not_modified'
                elif response.status_code==200:
                    buffer = bytearray()
                    for chunk in response.iter_bytes(65536):
                        self.check_deadline()
                        self.checkpoint()
                        buffer.extend(chunk)
                        if len(buffer)>MAX_BYTES:
                            raise ValueError('ECB response exceeds size limit')
                    body = bytes(buffer)
                    record['state']='received'
                else:
                    raise ValueError('Expected HTTP 200 or validated 304; redirects are not followed')
                cache_control = public_headers.get('cache-control','').lower()
                match = re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d{1,10})',cache_control)
                age = public_headers.get('age','0')
                age = int(age) if age.isascii() and age.isdigit() and len(age)<=10 else 0
                ttl = max(60,int(match[1])-age) if match else 60
                ttl = min(ttl,3600) if url==ROBOTS else ttl
                if 'no-cache' in cache_control: ttl=0
                with self.lease.connection.begin():
                    self.lease.require(automatic=True)
                    if 'no-store' in cache_control:
                        self.lease.connection.execute(text('DELETE FROM ecb_http_cache WHERE source_id=:id AND url=:url'),{'id':SOURCE_ID,'url':url})
                    else:
                        self.lease.connection.execute(text('INSERT INTO ecb_http_cache '
                        '(source_id,url,body,etag,last_modified,checked_at,fresh_until,headers) '
                        'VALUES (:id,:url,:body,:etag,:modified,:at,:until,CAST(:headers AS jsonb)) '
                        'ON CONFLICT(source_id,url) DO UPDATE SET body=EXCLUDED.body,etag=EXCLUDED.etag,last_modified=EXCLUDED.last_modified,'
                        'checked_at=EXCLUDED.checked_at,fresh_until=EXCLUDED.fresh_until,headers=EXCLUDED.headers'),
                            {'id':SOURCE_ID,'url':url,'body':body,'etag':public_headers.get('etag'),'modified':public_headers.get('last-modified'),
                             'at':observed,'until':observed+timedelta(seconds=ttl),'headers':json.dumps(public_headers,allow_nan=False)})
                    self.lease.connection.execute(text('DELETE FROM ecb_http_cache WHERE source_id=:id AND url NOT IN '
                        '(SELECT url FROM ecb_http_cache WHERE source_id=:id ORDER BY checked_at DESC,url LIMIT 16)'),{'id':SOURCE_ID})
                record.update(observed_at=observed.isoformat(),headers=public_headers,body_sha256=hashlib.sha256(body).hexdigest())
                return body
        finally:
            if record['state']=='requesting': record['state']='failed'
            self.requests.append(record)
            self.client.cookies.clear()

    def robots(self):
        self.policy = self.fetch(ROBOTS).decode('utf-8',errors='strict')
        if not re.search(r'(?im)^\s*user-agent\s*:',self.policy):
            raise ValueError('Unrecognized robots response')
        robot = RobotFileParser()
        robot.parse(self.policy.splitlines())
        delay = robot.crawl_delay(USER_AGENT) or robot.crawl_delay('*') or 5
        if not 0<=delay<=60:
            raise ValueError('Robots delay exceeds this bounded adapter')
        with self.lease.connection.begin():
            self.lease.connection.execute(text('UPDATE ecb_discovery_state SET host_delay_seconds=:delay WHERE source_id=:id'),
                                          {'id':SOURCE_ID,'delay':max(5,delay)})

    def discover(self):
        self.robots()
        base = index_base(self.fetch(VERSIONS))
        if len(base)>512:
            raise ValueError('Unexpected ECB index locator length')
        self.base = base
        with self.lease.connection.begin():
            current = state(self.lease.connection)
        if current['index_ready'] and current['index_base']==base:
            entries,coverage = current['index_window'],current['coverage']
            reused = True
        else:
            entries,coverage = parse_index(self.fetch(base+'metadata.json'),self.fetch(base+'data/0/chunk_0.json'))
            reused = False
        with self.lease.connection.begin():
            index = snapshot(self.lease.connection,'web_index',entries,base=base,coverage=coverage)
        prepared = prepare_candidates(parse_feed_items(self.fetch(RSS)))
        if prepared['selection']['precheck_needs_review']:
            raise ValueError('Conflicting RSS candidates')
        with self.lease.connection.begin():
            rss = snapshot(self.lease.connection,'rss',prepared['candidates'])
        return {'index':index,'rss':rss,'index_window_reused':reused,'coverage':coverage}
