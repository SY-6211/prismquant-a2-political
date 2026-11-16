"""Finite official-source navigation. No general search, model calls, or company-data claims."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import time
from urllib.parse import urlsplit

from app.core.config import PROJECT_ROOT
from app.web_http import Reader, FetchError
from app.web_extract import extract, ExtractionError

VERSION = 'official_source_navigation_v1'
PROMPT = 'person_source_navigation_v1'
MAX_READS = 3
MAX_SECONDS = 45


@dataclass(frozen=True)
class Resource:
    key: str
    name: str
    url_value: str
    topics: tuple[str, ...]
    steps: tuple[str, ...]
    coverage: str
    terms: tuple[str, ...]
    @property
    def host(self): return urlsplit(self.url_value).hostname
    def url(self, value):
        # A source title, model answer, or discovered hyperlink cannot expand the network scope.
        if value not in (self.url_value, 'https://' + self.host + '/robots.txt'):
            raise ValueError('url_outside_official_navigation')
        return value


RESOURCES = (
    Resource('fed-bank-ai-testimony', '美联储：银行创新与 AI 应用证词（2026-03-26）', 'https://www.federalreserve.gov/newsevents/testimony/guynn20260326a.htm',
        ('financial_ai',), ('查看证词的 Artificial Intelligence 部分，核对提到的应用场景。','区分该证词作者与当前监控人物；案例描述不能代表所有银行的采用率。'),
        '美国背景资料，作者 Randall D. Guynn；不是 Barr 原话。可补充应用案例与风险背景，不提供具体公司的 AI 投资金额。',('financial_ai_background',)),
    Resource('treasury-financial-ai-report', '美国财政部：金融服务业 AI 报告发布说明（2024）', 'https://home.treasury.gov/news/press-releases/jy2760',
        ('financial_ai',), ('阅读报告发布说明；需要完整研究时，再查看页面所链接的报告。','核对报告年份、反馈样本和适用范围；本轮读取的是 HTML 发布说明，不是 PDF 全文。'),
        '美国财政部背景资料（2024），不是人物本人发言或最新市场统计；可补充应用、机会与风险，不足以核实某家公司的投资金额。',('financial_ai_background',)),
    Resource('fed-supervision', '美联储：银行监管与政策资料', 'https://www.federalreserve.gov/supervisionreg.htm',
        ('bank_regulation',), ('按监管主题、监管函件或政策文件查找相关资料。','核对文件日期、适用机构及政策是否生效；没有明确 AI 内容时，不能推断监管机构的 AI 立场。'),
        '银行监管背景入口；入口本身不能证明某项 AI 政策已实施，也不是所选人物的发言。',
        ('金融监管','银行监管','监管政策','监管机构','financial regulation','bank supervision','regulatory policy')),
    Resource('fed-speeches', '美联储：官方讲话目录', 'https://www.federalreserve.gov/newsevents/speeches.htm',
        ('speech',), ('按人物和日期定位讲话。', '打开讲话正文，核对署名及发布日期；机构声明与个人发言分别处理。'),
        '官方讲话入口；本轮不保证已读取某位人物的最新讲话全文。',
        ('barr', '巴尔', '美联储讲话', '美联储发言', '鲍威尔', 'powell')),
    Resource('ecb-speeches', '欧洲央行：官方讲话目录', 'https://www.ecb.europa.eu/press/key/html/index.en.html',
        ('speech',), ('在目录筛选人物、日期和主题。', '打开讲话正文，核对作者及正文；发布日只有日期时，不推算发布时间。'),
        '欧洲央行讲话入口，不代表已取得某位人物的全部发言。',
        ('拉加德', 'lagarde', '欧洲央行讲话', 'ecb speech')),
    Resource('bls-cpi', 'BLS：消费者价格指数', 'https://www.bls.gov/cpi/',
        ('inflation',), ('选择 CPI 新闻发布或数据表。', '明确总体或核心、同比或环比、是否季调和数据期间。'),
        '美国消费者价格数据入口，不是人物言论或对个股影响的证明。',
        ('通胀', 'cpi', '消费者价格', 'inflation')),
    Resource('sec-filings', 'SEC EDGAR：公司申报检索', 'https://www.sec.gov/edgar/search/',
        ('company',), ('输入公司名称、股票代码或 CIK。', '按所需期间筛选 10-K、10-Q 或 8-K。', '打开申报正文，在现金流量表、附注和 MD&A 查找相关项目；对照期间和计量单位。'),
        '公司公开披露入口；本轮不查询具体公司或下载申报。员工薪酬明细等项目未必单独披露。',
        ('capex', 'r&d', '资本支出', '研发', '利润率', '公司', '企业', '银行', '财报', '算力', '数据中心', 'capital', 'company', 'bank', 'margin')),
    Resource('sec-api', 'SEC：申报与 XBRL 数据接口说明', 'https://www.sec.gov/search-filings/edgar-application-programming-interfaces',
        ('company',), ('先确定目标公司的 CIK。', '查阅 submissions 与 companyfacts 的接口说明。', '提取前确认财务标签、单位、财年及申报日期；不能把不同口径直接比较。'),
        '接口说明页；不是已经取得的公司财务数值。SEC 说明这些数据接口不要求 API Key。',
        ('capex', 'r&d', '财务', '财报', '资本支出', '研发', '利润率', 'financial', 'xbrl', 'companyfacts')),
    Resource('bls-jobs', 'BLS：职业就业与工资统计', 'https://www.bls.gov/oes/',
        ('labor',), ('选择所需年份及全国、地区或行业表。', '确定职业分类 SOC 和行业分类 NAICS，查找就业人数与工资。', '跨年比较时核对分类变化与调查说明；这些表不等于实时招聘职位数。'),
        '就业与工资统计入口；不能据此直接认定 AI 造成就业变化，也不提供单家公司的完整员工薪酬。',
        ('劳动', '就业', '工资', '薪酬', '招聘', '岗位', '劳动力', 'labor', 'employment', 'wage', 'job')),
    Resource('bls-productivity', 'BLS：劳动生产率与成本', 'https://www.bls.gov/productivity/',
        ('productivity',), ('选择行业或主要部门的生产率与成本系列。', '核对产出、工时和单位劳动成本的定义、期间及修订。', '将系列作为背景对照；识别 AI 的因果影响仍需要独立研究。'),
        '生产率与成本统计入口，不是单家公司的 AI 生产率测量。',
        ('生产率', '劳动收入', '劳动份额', '劳动成本', 'productivity', 'labor share')),
    Resource('bea-data', 'BEA：经济数据分类入口', 'https://www.bea.gov/data',
        ('macro',), ('选择产业、收入、国际贸易或固定资产投资主题。', '进入适用的数据表，选择行业、期间和频率。', '核对现价或不变价、季调及修订；宏观投资总量不能等同于 AI 专项投资。'),
        '宏观数据导航；不保证含有单独的 AI 行业投资分类。',
        ('宏观', '投资增速', '收入份额', '固定资产', 'gdp', '经济', '贸易', '关税', 'macro', 'trade', 'investment')),
    Resource('ftc-reports', 'FTC：竞争与政策研究报告', 'https://www.ftc.gov/policy/reports',
        ('competition',), ('在报告目录查找相关行业、云服务或 AI 竞争报告。', '核对报告日期、市场定义和覆盖企业。', '若报告未披露市场份额，需另找口径一致的数据；不能凭报告目录计算 CR3 或 CR5。'),
        '竞争研究的候选报告入口；不保证提供 AI 细分市场份额或 CR3/CR5。',
        ('集中度', '市场份额', 'cr3', 'cr5', '竞争', '垄断', 'concentration', 'market share')),
)
ARTICLE_KEYS=('fed-bank-ai-testimony','treasury-financial-ai-report','fed-bank-ai-policy','jpm-ai-disclosure')
RESOURCES += (
 Resource('fed-bank-ai-policy','美联储：AI在金融体系中的应用与监管（2026-05-01）','https://www.federalreserve.gov/newsevents/speech/bowman20260501a.htm',('bank_regulation',),('阅读AI应用和监管部分；区分监管指导的适用范围与新技术。',),'其他官员的官方背景，不是所选人物原话。',('银行AI监管','金融AI监管','bank AI regulation','金融行业监管与人工智能')),
 Resource('jpm-ai-disclosure','JPMorganChase：2025年报运营负责人致股东信','https://www.jpmorganchase.com/ir/annual-report/2025/ar-ceo-letter-jennifer-piepszak',('company_disclosure',),('核对2025报告期、署名、具体指标及其披露口径。','致股东信是公司报告的主张，不能等同于审计确认的AI投资额。'),'单家公司披露；不是行业整体证据，也不是政治人物发言。',('jpmorgan','摩根大通','jpm ai','JPMorganChase')),
)
BY_KEY = {r.key:r for r in RESOURCES}


def state(question, history, memory):
    """Use an earlier checklist as untrusted task navigation, never as factual evidence."""
    if not memory.get('enabled', True) or memory.get('topic_reset') or not memory.get('followup_detected'):
        return {'items': [], 'selection': 'current_question', 'history_is_evidence': False}
    from app.agent_memory import RESET
    rows = list(history)[-50:]
    for i in reversed(range(len(rows))):
        if RESET.search(str(rows[i].get('question') or '')):
            rows = rows[i:]; break
    # Prefer a relevant selected turn; a bare "these/second" refers to the latest checklist.
    selected = {str(x.get('message_id')) for x in memory.get('selected_entries', [])}
    recent = next((r for r in reversed(rows) if r.get('research_steps') or r.get('evidence_gaps_zh')), None)
    if not recent:
        return {'items': [], 'selection': 'no_saved_checklist', 'history_is_evidence': False}
    if selected and str(recent.get('message_id')) not in selected:
        latest = rows[-1] if rows else None
        if latest is not recent:
            return {'items': [], 'selection': 'no_related_checklist', 'history_is_evidence': False}
    items=[]
    steps=recent.get('research_steps')
    origin='saved_checklist'
    if not steps:
        origin='analysis_evidence_gaps'
        gaps=recent.get('evidence_gaps_zh')
        steps=[dict(data_zh=g,purpose_zh='补查前文尚未核对的资料；缺口描述不是事实证据。',how_to_check_zh='') for g in gaps[:5] if isinstance(g,str) and g.strip()] if isinstance(gaps,list) else []
    if not isinstance(steps,list):steps=[]
    for i,s in enumerate(steps[:6],1):
        if not isinstance(s,dict) or not isinstance(s.get('data_zh'),str):continue
        number=s.get('number',i)
        if type(number) is not int or not 1<=number<=6:continue
        items.append({'number':number,**{k:str(s.get(k) or '')[:300] for k in ('data_zh','purpose_zh','how_to_check_zh')}})
    checklist=[{'number':s['number'],'data_zh':s['data_zh']} for s in items]
    chosen=re.search(r'第\s*([一二三四五六1-6])\s*(?:项|条|个)',question)
    selection='previous_checklist'
    if chosen:
        number=int(chosen[1]) if chosen[1].isdigit() else '一二三四五六'.index(chosen[1])+1
        items=[s for s in items if s['number']==number];selection='checklist_item_'+str(number)
    return {'items':items,'checklist':checklist,'origin':origin,'selection':selection,'from_message_id':str(recent.get('message_id') or ''),
            'from_answer_run_id':str(recent.get('answer_run_id') or ''),'history_is_evidence':False}


def prepare(question, memory, *, enabled=False, reader_factory=Reader, clock=time.monotonic, search_origin=None, searcher=None,
            max_reads=MAX_READS,max_seconds=MAX_SECONDS,skip_keys=()):
    if type(max_reads) is not int or not 0<=max_reads<=MAX_READS or not 0<max_seconds<=MAX_SECONDS:raise ValueError('Invalid finite read budget')
    navigation=memory.get('research_state',{})
    items=list(navigation.get('items',[]))
    if not items and navigation.get('selection','').startswith('checklist_item_'):
        return dict(version=VERSION,items=[],resources=[],network_requests=0,reads=0,
                    checklist_items=navigation.get('checklist',[]),stop_reason='checklist_item_missing',external_reading_enabled=enabled,model_requests=0)
    if not items:
        hint=memory.get('retrieval_hint',question)
        items=[dict(number=1,data_zh=hint[:300],purpose_zh='',how_to_check_zh='')]
    matches=[]
    for item in items:
        # General previous model text is only a navigation hint, not an attribution or URL.
        hay=(item['data_zh']+' '+item.get('purpose_zh','')+' '+item.get('how_to_check_zh','')).lower()
        ranked=[(max((len(term) for term in r.terms if term in hay),default=0),r) for r in RESOURCES]
        financial_ai=bool(re.search(r'金融|银行|保险|financial|bank|insurance',hay,re.I) and re.search(r'人工智能|(?<![a-z])ai(?![a-z])|artificial intelligence|机器学习|machine learning|大模型',hay,re.I))
        if re.search(r'英国|欧洲|\bUK\b|\bEU\b|European|United Kingdom',hay,re.I) and not re.search(r'美国|\bUS\b|United States',hay,re.I):financial_ai=False
        if re.search(r'jpmorgan|摩根大通|\bjpm\b',hay,re.I):ranked=[(120 if r.key=='jpm-ai-disclosure' else score,r) for score,r in ranked]
        if financial_ai:ranked=[(max(score,80) if r.key in ('fed-bank-ai-testimony','treasury-financial-ai-report') else score,r) for score,r in ranked]
        if financial_ai and re.search(r'监管|regulat|supervis',hay,re.I):ranked=[(95 if r.key=='fed-bank-ai-policy' else score,r) for score,r in ranked]
        if re.search(r'金融监管|银行监管|监管政策|监管机构|financial regulation|bank supervision|regulatory policy',item['data_zh'],re.I):
            ranked=[(score+50 if r.key=='fed-supervision' else score,r) for score,r in ranked]
        company_financial=bool(re.search(r'公司|企业|银行|company|bank',item['data_zh'],re.I) and re.search(r'资本支出|研发|利润率|薪酬占|财务|capex|r&d|margin',item['data_zh'],re.I))
        if company_financial:ranked=[(score+50 if score and r.key in ('sec-filings','sec-api') else score,r) for score,r in ranked]
        keys=[r.key for score,r in sorted(ranked,key=lambda x:-x[0]) if score]
        matches.append({'number':item['number'],'data_zh':item['data_zh'],'resource_keys':keys[:3]})
    keys=list(dict.fromkeys(k for item in matches for k in item['resource_keys']))
    # Round-robin first choices cover different checklist items before extra resources.
    ordered=list(dict.fromkeys([item['resource_keys'][0] for item in matches if item['resource_keys']]+keys))
    start=clock();reads=requests=0;resources=[]
    from app import research_search
    if search_origin is None:
        from app.core.config import Settings
        search_origin=Settings().searxng_url
    search_result=(searcher or research_search.search)(' '.join(i['data_zh'] for i in items[:2]),search_origin) if enabled else {'status':'disabled','requests':0,'results':[]}
    dynamic={}
    for found in search_result['results']:
        if research_search.approved(found['url'])!=found['channel']:continue
        key=found['key']
        dynamic[key]=Resource(key,found['title'],found['url'],('background',),('打开原始页面并核对作者、日期与正文。','将页面内容与研究问题核对；搜索排名不代表事实支持。'),
                              '搜索发现的官方正文；不自动归属于所选人物，未核验其中事实。',())
    # Prefer up to two actual articles to another catalog landing page. The
    # same three-read ceiling covers both discovery paths.
    ordered=list(dynamic)+ordered
    if dynamic:
        matches.append(dict(number=0,data_zh='搜索补充候选（需核对与当前问题的相关性）',resource_keys=list(dynamic)))
    def checkpoint():
        if clock()-start>=max_seconds:raise FetchError('lookup_deadline')
    for key in ordered:
        resource=dynamic.get(key) or BY_KEY[key]
        out=dict(resource_key=key,name=resource.name,url=resource.url_value,access='public_entry',
                 steps=list(resource.steps),coverage_zh=resource.coverage,status='suggested',
                 text='',text_sha256=None,html_sha256=None,read_at=None,page_date=None,
                 data_verified=False,role='background_navigation_not_person_statement')
        if enabled and key not in skip_keys and reads<max_reads and clock()-start<max_seconds:
            reads+=1
            reader=None
            try:
                checkpoint()
                with reader_factory(resource,cache_dir=PROJECT_ROOT/'.runtime'/'source-navigation',max_requests=3,
                                    checkpoint=checkpoint,request_timeout=8,max_seconds=max_seconds) as reader:
                    page=reader.fetch(resource.url_value)
                    extraction_source=None
                    if resource.key in ARTICLE_KEYS:
                        from app.web_sources import SOURCES
                        extraction_source=SOURCES['treasury' if resource.key=='treasury-financial-ai-report' else 'fed'] if resource.key!='jpm-ai-disclosure' else None
                    elif key in dynamic:
                        from app.web_sources import SOURCES
                        extraction_source=SOURCES[research_search.approved(resource.url_value)]
                    if resource.key=='jpm-ai-disclosure':
                        from bs4 import BeautifulSoup
                        from types import SimpleNamespace
                        soup=BeautifulSoup(page.body,'html.parser')
                        regions=soup.select('.cmp-text')
                        if not regions:raise ExtractionError('body_selector_changed')
                        payload='<html><head>'+str(soup.title)+'</head><body>'+''.join(str(x) for x in regions)+'</body></html>'
                        document=extract(payload,url=page.url,source=SimpleNamespace(body_selector='body',title_selector='title',date_selector='time',key='company'))
                    else:document=extract(page.body,url=page.url,source=extraction_source)
                body=document['body_text'];offset=0
                if resource.key in ARTICLE_KEYS:
                    if not re.search(r'artificial intelligence|generative AI|machine learning',body,re.I):raise ExtractionError('financial_ai_body_not_found')
                    match=re.search(r'Artificial Intelligence|generative AI|machine learning',body,re.I)
                    if match:offset=max(0,body.rfind('\n',0,match.start())+1)
                text=body[offset:offset+2000] if resource.key in ARTICLE_KEYS else body[:1300]
                out.update(status='read',text=text,text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                           html_sha256=page.sha256,body_sha256=document['body_sha256'],
                           read_at=page.fetched_at.isoformat(),page_date=document['page_date'],
                           title=document['title'],excerpt_truncated=len(document['body_text'])>len(text))
                if key in dynamic:
                    out.update(content_kind='official_background_document',text_start=offset,text_end=offset+len(text),
                               source_date=document['page_date'],excerpt_matches_extracted_body=True)
                if resource.key=='jpm-ai-disclosure':out.update(issuer_type='company',reporting_period='2025')
                if resource.key in ARTICLE_KEYS:
                    out.update(content_kind='official_background_document',jurisdiction='US',text_start=offset,text_end=offset+len(text),
                               excerpt_matches_extracted_body=body[offset:offset+len(text)]==text,
                               source_date={'fed-bank-ai-testimony':'2026-03-26','treasury-financial-ai-report':'2024-12-19','fed-bank-ai-policy':'2026-05-01'}.get(resource.key,document['page_date']))
                    snippets=[]
                    for match in re.finditer(r'[^\n]+',text):
                        paragraph=match.group()
                        if len(paragraph)<80 or not re.search(r'artificial intelligence|generative AI|machine learning',paragraph,re.I):continue
                        length=min(420,len(paragraph))
                        if length<len(paragraph):length=paragraph.rfind(' ',0,length)
                        if length<60:continue
                        snippets.append(dict(text=paragraph[:length],start=offset+match.start(),end=offset+match.start()+length))
                        if len(snippets)==2:break
                    out['evidence_snippets']=snippets
            except (FetchError,ExtractionError,ValueError,OSError) as error:
                code=str(error)
                known={'lookup_deadline','source_busy','robots_disallowed','access_denied_or_rate_limited',
                       'transport_error','non_public_dns','dns_unavailable','login_or_challenge',
                       'empty_short_or_oversized_body','missing_article_title','finite_request_budget',
                       'url_outside_official_navigation','unsupported_content_type','robots_html_response','financial_ai_body_not_found'}
                out.update(status='unavailable',failure_code=code if code in known else 'source_unavailable')
            finally:
                if reader is not None:requests+=len(reader.audit)
        resources.append(out)
    return dict(version=VERSION,items=matches,checklist_items=navigation.get('checklist') or [{'number':i['number'],'data_zh':i['data_zh']} for i in items],resources=resources,network_requests=requests,reads=reads,search=search_result,
                stop_reason='bounded_catalog' if keys else 'no_catalog_match',external_reading_enabled=enabled,
                catalog_search_only=not bool(search_result['requests']),web_search_executed=bool(search_result['requests']),company_data_fetched=False,
                model_requests=0,max_reads=max_reads,max_seconds=max_seconds)


def answer(context):
    """Host-rendered navigation has no model-generated addresses or unsupported current data."""
    lookup=context['source_lookup'];resources=lookup['resources']
    if lookup['stop_reason']=='checklist_item_missing':
        summary='没有找到所指的清单项目，请说明项目名称。'
    elif not resources:
        summary='当前官方来源目录未匹配到这类资料。请补充数据名称、国家或目标公司；本次没有进行全网搜索。'
    else:
        summary='以下对应前文的数据项目，列出官方入口和查找步骤。页面读取状态与具体数据核验分开展示；本轮没有取得具体公司数值。'
    if lookup.get('search',{}).get('requests'):
        summary+=' 本轮已尝试搜索补充来源；搜索状态：'+lookup['search']['status']+'。'
    background=[r for r in resources if r.get('content_kind')=='official_background_document' and r['status']=='read']
    if background:summary+=' 已读取 '+str(len(background))+' 份相关官方正文，可核对应用与风险背景；具体公司的 AI 投资金额仍未取得。'
    steps=[dict(number=i['number'],data_zh=i['data_zh'],purpose_zh='',how_to_check_zh='',
                basis_type='general_method',premise_points=[]) for i in lookup['checklist_items']]
    blocks=[summary]
    for item in lookup['items']:
        blocks.append(str(item['number'])+'. '+item['data_zh'])
        for key in item['resource_keys']:
            r=next(r for r in resources if r['resource_key']==key)
            blocks.append(r['name']+' '+r['url']+'\n状态：'+r['status']+'（具体数据未核验）\n'+'\n'.join(r['steps']))
    return dict(answer_mode='source_navigation',research_task='source_lookup',points=[],outcome='answered' if resources else 'insufficient_evidence',
                answer_zh='\n\n'.join(blocks),summary_zh=summary,research_steps=steps,source_items=lookup['items'],source_resources=resources,
                needs_review=True,human_review_status='unreviewed',factual_support='not_independently_assessed',
                limitations_zh=['来源目录匹配和读取入口页不代表已取得、核验所需数据；本轮未进行全网搜索。'] if not lookup.get('web_search_executed') else ['搜索结果仅为候选；获准页面读取及其内容的事实核验分别处理。'],
                historical_availability_verified=False,validation_scope='frozen_official_navigation_and_snapshot_hashes')


def validate(context, value):
    lookup=context['source_lookup']
    if lookup.get('version')!=VERSION:raise ValueError('Unknown source navigation version')
    for r in lookup['resources']:
        spec=BY_KEY.get(r['resource_key'])
        if spec is None:
            from app.research_search import approved
            found=next((f for f in lookup.get('search',{}).get('results',[]) if f['key']==r['resource_key'] and f['url']==r['url'] and approved(f['url'])==f['channel']),None)
            if found:spec=Resource(found['key'],found['title'],found['url'],(),(),'',())
        if spec is None or r['url']!=spec.url_value or r['data_verified'] or r['role']!='background_navigation_not_person_statement':
            raise ValueError('Navigation scope changed')
        if r['status']=='read' and (not r['text'] or r['text_sha256']!=hashlib.sha256(r['text'].encode()).hexdigest()):
            raise ValueError('Navigation snapshot changed')
        for snippet in r.get('evidence_snippets',[]):
            a=snippet['start']-r['text_start'];b=snippet['end']-r['text_start']
            if not 0<=a<b<=len(r['text']) or r['text'][a:b]!=snippet['text']:raise ValueError('Background quote changed')
    expected=answer(context)
    if value!=expected:raise ValueError('Navigation answer differs from frozen host result')
    return expected
