"""Offline HTML extraction. Metadata is a claim; publication seconds are never guessed."""
import hashlib,re
from datetime import date,datetime,timezone
from importlib.metadata import version
from bs4 import BeautifulSoup

PARSER='trafilatura_web_v1'
class ExtractionError(ValueError):pass

def extract(html,*,url,source=None):
    if not isinstance(html,(str,bytes)) or len(html)>2*1024*1024:raise ExtractionError('invalid_html_size')
    try:from trafilatura import bare_extraction
    except ImportError:raise ExtractionError('install_requirements_web') from None
    soup=BeautifulSoup(html,'html.parser')
    if soup.select('form input[type="password"]') or re.search(r'(captcha|just a moment|access denied|robot check)',soup.title.get_text() if soup.title else '',re.I):raise ExtractionError('login_or_challenge')
    title_node=soup.select_one(source.title_selector) if source else soup.select_one('h1')
    title=title_node.get_text(' ',strip=True) if title_node else None
    body=soup.select_one(source.body_selector) if source else soup.select_one('article, main')
    if source and body is None:raise ExtractionError('body_selector_changed')
    region=BeautifulSoup(str(body) if body else str(soup),'html.parser')
    for element in region.select('nav,header,footer,aside,script,style,form,.related-content,.share,.social-sharing,.article__header,.article__time,.news__speaker,.speaker,.location,.title,.disclaimer'):
        element.decompose()
    # Trafilatura's cleaner may discard a one-paragraph fragment root. Keep an HTML/body envelope.
    payload=str(region) if region.find('html') else '<html><body>'+str(region)+'</body></html>'
    document=bare_extraction(payload,url=url,include_comments=False,include_tables=True,include_images=False,
        include_links=False,favor_precision=True,with_metadata=False,deduplicate=False)
    text=(document.text if document else None)
    strategy='precision'
    if not text:
        document=bare_extraction(payload,url=url,include_comments=False,include_tables=True,include_images=False,
            include_links=False,favor_precision=False,with_metadata=False,deduplicate=False)
        text=document.text if document else None;strategy='balanced_fallback'
    if not text or not 160<=len(text.strip())<=100000:raise ExtractionError('empty_short_or_oversized_body')
    text=text.strip()
    if not title:title=document.title if document else None
    if not title or not 5<=len(title)<=2000:raise ExtractionError('missing_article_title')
    raw_date=None;page_date=None;published=None
    if source:
        element=soup.select_one(source.date_selector)
        if element:raw_date=element.get('datetime') or element.get('content') or element.get_text(' ',strip=True)
    if not raw_date:
        meta=soup.select_one('meta[property="article:published_time"],meta[name="date"],time[datetime]')
        if meta:raw_date=meta.get('content') or meta.get('datetime')
    if raw_date:
        raw_date=raw_date.strip()[:200]
        try:
            parsed=datetime.fromisoformat(raw_date.replace('Z','+00:00'))
            if parsed.utcoffset() is not None:published=parsed.astimezone(timezone.utc).isoformat()
            page_date=parsed.date().isoformat()
        except ValueError:
            for fmt in ('%B %d, %Y','%b %d, %Y','%m/%d/%Y','%Y-%m-%d'):
                try:page_date=datetime.strptime(raw_date,fmt).date().isoformat();break
                except ValueError:pass
    author=soup.select_one('#article .speaker') if source and source.key=='fed' else None
    author_claim=author.get_text(' ',strip=True) if author else None
    author_name=None
    if author_claim:
        match=re.fullmatch(r'(?:Chair|Vice Chair(?: for Supervision)?|Governor|Vice Chairman|Chairman)\s+([A-Z][A-Za-z.\- ]{3,100})',author_claim)
        if match:author_name=match[1].strip()
    speech_verified=bool(source and source.key=='fed' and author_name and page_date and len(soup.select('#article .speaker'))==1)
    return {'url':url,'title':title,'body_text':text,'body_characters':len(text),'body_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'html_sha256':hashlib.sha256(html.encode() if isinstance(html,str) else html).hexdigest(),
        'page_date':page_date,'published_at':published,'published_raw':raw_date,
        'date_precision':'instant' if published else ('day' if page_date else 'unknown'),
        'author_claim':author_claim,'person_name':author_name if speech_verified else None,
        'attribution':'official_speech_byline' if speech_verified else 'institution_background',
        'speaker_evidence':author_claim if speech_verified else None,
        'eligible_for_person_import':speech_verified,'parser_version':PARSER,'extractor':'trafilatura','extractor_version':version('trafilatura'),
        'review_status':'accepted' if speech_verified else 'needs_review','extraction_strategy':strategy,
        'notice':'官方页面归属与原文提取，不代表内容事实已独立核实。' if speech_verified else '机构背景或待核对正文；不得当作标题中人物的本人发言。'}
