"""One optional local SearXNG request; results are candidates, never factual evidence."""
import hashlib
import json
from urllib.parse import urlsplit
import httpx
from app.web_sources import SOURCES

MAX_BYTES=262144


def endpoint(value):
    if not value:return ''
    p=urlsplit(value)
    if p.scheme!='http' or p.hostname!='127.0.0.1' or p.username or p.password or p.path not in ('','/') or p.query or p.fragment or not p.port:
        raise ValueError('Use a local SearXNG origin with an explicit port')
    return f'http://127.0.0.1:{p.port}'


def approved(url):
    if not isinstance(url,str):return None
    # Only previously reviewed publication channels may be opened. Redirects,
    # search snippets and model text cannot enlarge the fetch permission.
    for key,source in SOURCES.items():
        try:
            source.url(url,detail=True)
            return key
        except (ValueError,TypeError):pass
    return None


def search(query,origin='',*,client=None):
    origin=endpoint(origin)
    base=dict(version='searxng_official_candidates_v1',status='not_configured',requests=0,results=[],
              query='',filtered_results=0,results_are_evidence=False,upstream_errors=False)
    if not origin:return base
    query=' '.join(str(query).split())[:240]
    if not query:return {**base,'status':'empty_query'}
    query+=' (site:www.federalreserve.gov OR site:home.treasury.gov OR site:www.eia.gov)'
    base.update(query=query,status='unavailable',requests=1)
    own=client is None
    client=client or httpx.Client(trust_env=False,follow_redirects=False,timeout=5)
    try:
        with client.stream('GET',origin+'/search',params={'q':query,'format':'json','language':'all'},timeout=5,follow_redirects=False) as response:
            if response.status_code!=200:
                return {**base,'status':'json_disabled_or_denied' if response.status_code==403 else 'rate_limited' if response.status_code==429 else 'unavailable'}
            if response.headers.get('content-type','').split(';')[0].strip()!='application/json':return {**base,'status':'invalid_response'}
            raw=bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw)>MAX_BYTES:return {**base,'status':'response_too_large'}
            data=json.loads(raw)
        if not isinstance(data,dict) or not isinstance(data.get('results'),list):return {**base,'status':'invalid_response'}
        found=[];seen=set();filtered=0
        for row in data['results'][:50]:
            if not isinstance(row,dict):filtered+=1;continue
            url=row.get('url');key=approved(url)
            if not key:filtered+=1;continue
            if url in seen:continue
            seen.add(url)
            found.append(dict(url=url,channel=key,title=str(row.get('title') or '官方页面')[:200],
                              key='search-'+hashlib.sha256(url.encode()).hexdigest()[:16]))
            if len(found)==2:break
        return {**base,'status':'ok' if found else 'no_approved_results','results':found,
                'filtered_results':filtered,'upstream_errors':bool(data.get('unresponsive_engines'))}
    except (httpx.HTTPError,ValueError,TypeError):return {**base,'status':'unavailable'}
    finally:
        if own:client.close()


def main():
    from app.core.config import Settings
    value=search('Michael Barr artificial intelligence',Settings().searxng_url)
    print(json.dumps({k:value[k] for k in ('status','requests','filtered_results','upstream_errors')},ensure_ascii=False))
    return 0 if value['status'] in ('ok','no_approved_results','not_configured') else 1


if __name__=='__main__':raise SystemExit(main())
