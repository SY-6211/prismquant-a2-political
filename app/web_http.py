"""Allowlisted, paced HTTPS; no JS, cookies, credential URLs or anti-bot bypass."""
import hashlib,ipaddress,json,socket,time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin,urlsplit
from urllib.robotparser import RobotFileParser
import httpx

USER_AGENT='PrismQuantResearch/0.1 (finite academic HTML extraction pilot)'
MAX_BYTES=2*1024*1024
class FetchError(ValueError):
    def __init__(self,code,*,http_status=None,retry_seconds=None):
        super().__init__(code);self.http_status=http_status;self.retry_seconds=retry_seconds

@dataclass(frozen=True)
class Page:
    url:str
    body:bytes
    sha256:str
    fetched_at:datetime
    state:str

def public_dns(host):
    try:addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    except OSError:raise FetchError('dns_unavailable') from None
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):raise FetchError('non_public_dns')

class Reader:
    def __init__(self,source,*,cache_dir=None,client=None,resolver=public_dns,sleeper=time.sleep,clock=time.monotonic,max_requests=10,checkpoint=None,request_timeout=20,max_seconds=180):
        if not 0 < request_timeout <= 20 or not 0 < max_seconds <= 180:raise ValueError('Invalid finite reader limits')
        self.request_timeout=request_timeout;self.max_seconds=max_seconds
        self.source=source;self.cache_dir=Path(cache_dir) if cache_dir else None
        self.client=client or httpx.Client(trust_env=False,follow_redirects=False,timeout=httpx.Timeout(20,connect=10),headers={'User-Agent':USER_AGENT,'Accept':'text/html,text/plain;q=0.9','Accept-Encoding':'gzip, deflate'})
        self.owns_client=client is None;self.resolver=resolver;self.sleep=sleeper;self.clock=clock
        self.started=clock();self.last=None;self.max_requests=max_requests;self.audit=[];self.policy=None;self.robots_status=None
        self.checkpoint=checkpoint or (lambda:None);self.ownership=None
    def __enter__(self):
        if self.cache_dir:
            from app.scheduler_service import FileLock
            self.ownership=FileLock(self.cache_dir/(self.source.key+'.lock'));self.ownership.__enter__()
            if self.ownership.file is None:
                if self.owns_client:self.client.close()
                raise FetchError('source_busy')
        return self
    def __exit__(self,*args):
        if self.owns_client:self.client.close()
        if self.ownership:self.ownership.__exit__(*args)
    def _paths(self,url):
        if self.cache_dir is None:return None,None
        prefix=self.cache_dir/hashlib.sha256(url.encode()).hexdigest()
        return prefix.with_suffix('.json'),prefix.with_suffix('.body')
    def _cache(self,url):
        meta,body=self._paths(url)
        if meta is None or not meta.exists() or not body.exists():return None
        try:
            if body.stat().st_size>MAX_BYTES or meta.stat().st_size>4096:return None
            value=json.loads(meta.read_text(encoding='utf8'));raw=body.read_bytes()
            if value['url']!=url or value['sha256']!=hashlib.sha256(raw).hexdigest():return None
            return value,raw
        except (ValueError,KeyError,OSError):return None
    def _request(self,url,*,robots=False):
        self.checkpoint()
        self.source.url(url)
        if not robots and (self.policy is None or not self.policy.can_fetch('PrismQuantResearch',url)):raise FetchError('robots_disallowed')
        if self.clock()-self.started>self.max_seconds or len(self.audit)>=self.max_requests:raise FetchError('finite_request_budget')
        self.resolver(self.source.host)
        delay=max(2,self.policy.crawl_delay('PrismQuantResearch') or self.policy.crawl_delay('*') or 0) if self.policy else 2
        if delay>30:raise FetchError('crawl_delay_requires_separate_schedule')
        if self.last is not None:self.sleep(max(0,delay-(self.clock()-self.last)))
        # Shared file ownership serializes manual/automatic readers. Persist host pacing across rounds.
        if self.cache_dir and self.ownership:
            rate=self.cache_dir/(self.source.key+'-rate.json')
            try:previous=json.loads(rate.read_text(encoding='utf8'))['at']
            except (OSError,ValueError,KeyError,TypeError):previous=0
            wait=delay-(time.time()-previous)
            if wait>35:raise FetchError('host_clock_requires_review')
            if wait>0:self.sleep(wait)
            rate.write_text(json.dumps({'at':time.time()}),encoding='utf8')
        self.checkpoint()
        if self.clock()-self.started>self.max_seconds:raise FetchError('finite_request_budget')
        cached=None if robots else self._cache(url);headers={'User-Agent':USER_AGENT}
        if cached:
            meta,raw=cached
            if meta.get('etag'):headers['If-None-Match']=meta['etag']
            elif meta.get('last_modified'):headers['If-Modified-Since']=meta['last_modified']
        self.last=self.clock();entry={'url':url,'conditional':bool(cached),'status':'requesting'};self.audit.append(entry)
        try:
            with self.client.stream('GET',url,headers=headers,follow_redirects=False,timeout=self.request_timeout) as response:
                status=response.status_code;entry['http_status']=status
                if status in (301,302,303,307,308):
                    entry['status']='redirect';target=urljoin(url,response.headers.get('location',''))
                    self.source.url(target)
                    if target==url:raise FetchError('redirect_loop')
                    return self._request(target,robots=robots)
                if robots and status in (404,410):
                    entry['status']='robots_absent';return status,b'',{}
                if status in (401,403,429):
                    retry=None
                    value=response.headers.get('retry-after','')
                    try:
                        if value.isdigit():retry=int(value)
                        elif value:
                            from email.utils import parsedate_to_datetime
                            parsed=parsedate_to_datetime(value)
                            if parsed.utcoffset() is not None:retry=max(0,int((parsed-datetime.now(timezone.utc)).total_seconds()))
                        if retry is not None and not 0<=retry<=86400:retry=None
                    except (ValueError,TypeError,OverflowError):retry=None
                    raise FetchError('access_denied_or_rate_limited',http_status=status,retry_seconds=retry)
                if status==304:
                    if not cached:raise FetchError('unusable_304')
                    entry['status']='not_modified';return 200,cached[1],cached[0]
                if status!=200:raise FetchError('http_status_'+str(status))
                content_type=response.headers.get('content-type','').split(';')[0].strip().lower()
                if not robots and content_type not in ('text/html','application/xhtml+xml'):raise FetchError('unsupported_content_type')
                if robots and content_type not in ('text/plain','text/html',''):raise FetchError('invalid_robots_type')
                try:declared=int(response.headers.get('content-length','0'))
                except ValueError:raise FetchError('invalid_content_length') from None
                if declared>MAX_BYTES:raise FetchError('page_too_large')
                chunks=[];size=0
                for chunk in response.iter_bytes():
                    size+=len(chunk)
                    if size>MAX_BYTES or self.clock()-self.started>self.max_seconds:raise FetchError('page_too_large_or_deadline')
                    chunks.append(chunk)
                raw=b''.join(chunks);entry['status']='fetched'
                meta={'url':url,'sha256':hashlib.sha256(raw).hexdigest(),'etag':response.headers.get('etag','')[:500],'last_modified':response.headers.get('last-modified','')[:200]}
                if self.cache_dir and not robots:
                    self.cache_dir.mkdir(parents=True,exist_ok=True);m,b=self._paths(url)
                    # Retain the raw evidence by content hash even after URL validators advance.
                    blobs=self.cache_dir/'blobs';blobs.mkdir(exist_ok=True);blob=blobs/(meta['sha256']+'.html')
                    try:
                        with blob.open('xb') as stream:stream.write(raw)
                    except FileExistsError:
                        if hashlib.sha256(blob.read_bytes()).hexdigest()!=meta['sha256']:raise FetchError('raw_snapshot_hash_mismatch')
                    # Readers validate the pair's hash. A partially replaced pair is never reused.
                    b.write_bytes(raw);m.write_text(json.dumps(meta),encoding='utf8')
                return status,raw,meta
        except (httpx.HTTPError,OSError) as error:
            entry['status']='transport_error';raise FetchError('transport_error') from None
        except FetchError as error:
            entry['status']=str(error);raise
    def fetch(self,url):
        self.source.url(url)
        if self.policy is None:
            status,raw,_=self._request('https://'+self.source.host+'/robots.txt',robots=True);self.robots_status=status
            if status==200 and b'<html' in raw[:500].lower():raise FetchError('robots_html_response')
            self.policy=RobotFileParser();self.policy.parse(raw.decode('utf8',errors='replace').splitlines())
        _,body,_=self._request(url)
        return Page(self.audit[-1]['url'],body,hashlib.sha256(body).hexdigest(),datetime.now(timezone.utc),self.audit[-1]['status'])
