"""Reviewed, finite HTML pilots. Supplemental sources never rewrite the 84-row catalog."""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit, urljoin, unquote

@dataclass(frozen=True)
class Source:
    key: str
    name: str
    publisher: str
    host: str
    index: str
    detail_pattern: str
    body_selector: str
    title_selector: str
    date_selector: str
    policy_url: str
    policy_note: str
    catalog_number: int | None = None

    def url(self, value, *, detail=False):
        if not isinstance(value,str) or len(value)>2048 or any(c.isspace() or ord(c)<32 for c in value):raise ValueError('invalid_url')
        p=urlsplit(value)
        if p.scheme!='https' or p.netloc!=self.host or p.port not in (None,443) or p.username or p.password or '\\' in value or '%' in value:raise ValueError('url_outside_reviewed_source')
        clean=p._replace(fragment='').geturl()
        if p.fragment:raise ValueError('fragment_not_supported')
        path=p.path+('?' + p.query if p.query else '')
        if detail and not re.fullmatch(self.detail_pattern,path):raise ValueError('not_an_article_url')
        if not detail and clean not in (self.index,'https://'+self.host+'/robots.txt') and not re.fullmatch(self.detail_pattern,path):raise ValueError('url_outside_pilot')
        return clean

SOURCES={
 'fed':Source('fed','美联储官方讲话（HTML试接）','Board of Governors of the Federal Reserve System','www.federalreserve.gov',
    'https://www.federalreserve.gov/newsevents/2026-speeches.htm',r'/newsevents/speech/[a-z][a-z0-9]*\d{8}[a-z]?\.htm',
    '#article', 'h3.title', '.article__time',
    'https://www.federalreserve.gov/disclaimer.htm','官方说明：未另行标注的信息属公共领域，转载需注明来源。仅处理官方文字，排除第三方附件。'),
 'eia':Source('eia','EIA Today in Energy（HTML试接）','U.S. Energy Information Administration','www.eia.gov',
    'https://www.eia.gov/todayinenergy/',r'/todayinenergy/detail\.php\?id=\d{1,9}',
    '.tie-article', '.tie-article h1', '.tie-article .date',
    'https://www.eia.gov/about/copyrights_reuse.php','官方允许引用和分发其信息产品并注明来源及发布日期；不采集照片和第三方受保护附件。机构背景，不冒充人物发言。',49),
 'treasury':Source('treasury','美国财政部声明与讲话（HTML试接）','U.S. Department of the Treasury','home.treasury.gov',
    'https://home.treasury.gov/news/press-releases/statements-remarks',r'/news/press-releases/[a-z]{1,5}\d{2,9}/?',
    '.field--name-field-news-body', 'main h2.uswds-page-title', 'main .field--name-field-news-publication-date time',
    'https://www.copyright.gov/title17/92chap1.html#105','仅限美国联邦机构自产公开文字的有限研究试读（17 USC 105）；保留来源，排除第三方附件。标题中的人名只作待核对信息，不自动归为本人全文。'),
}

def get_source(key):
    if key not in SOURCES:raise ValueError('unknown_pilot_source')
    return SOURCES[key]

def discover(html, source, *, limit=3):
    from bs4 import BeautifulSoup
    if type(limit)!=int or not 1<=limit<=50:raise ValueError('invalid_limit')
    soup=BeautifulSoup(html,'html.parser');items=[];seen=set()
    for a in soup.select('a[href]'):
        try:url=source.url(urljoin(source.index,a['href']).split('#')[0],detail=True)
        except ValueError:continue
        title=' '.join(a.get_text(' ',strip=True).split())
        if url in seen or len(title)<8 or title.lower().startswith(('read more','view all')):continue
        seen.add(url);items.append({'url':url,'title':title[:1000]})
        if len(items)>=limit:break
    return items
