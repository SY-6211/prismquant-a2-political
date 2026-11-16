"""Exercise the actual failed question, scoped sources and exact background extracts."""
import copy,hashlib,unittest
from app import research_sources as nav
from app.agent_qa import validate_answer
import test_research_sources as helpers
import httpx

FAILED='请查找以下待核对资料的官方入口：缺乏直接关于金融行业AI应用现状、投资规模及具体案例的原文引用。'
BODY=('Artificial intelligence is used by banks in fraud detection and prevention. Generative AI can assist with document summarization and coding. These are reported examples, not independently verified findings.\n')*8
HTML='<html><head><title>Official financial AI document</title></head><body><article><h1>MENU ONLY</h1><p>Navigation links</p></article><main><h2 class="uswds-page-title">Financial AI report release</h2><div class="field--name-field-news-body"><p>'+BODY+'</p></div><div class="field--name-field-news-publication-date"><time datetime="2024-12-19">December 19, 2024</time></div></main><div id="article"><h3 class="title">Innovation</h3><span class="article__time">March 26, 2026</span><p>'+BODY+'</p></div></body></html>'

class FinancialSourceTests(unittest.TestCase):
    def test_failed_question_gets_relevant_documents_without_search_service(self):
        r=nav.prepare(FAILED,{},search_origin='')
        self.assertEqual(r['items'][0]['resource_keys'][:2],['fed-bank-ai-testimony','treasury-financial-ai-report'])
        self.assertTrue(all(s['status']=='suggested' for s in r['resources']))
    def test_matching_generalizes_across_people_and_wordings(self):
        for q in ('Barr 金融行业AI应用案例资料哪里找','Trump 银行人工智能应用情况来源','banking artificial intelligence use cases','保险行业机器学习应用现状'):
            r=nav.prepare(q,{},search_origin='');self.assertEqual(r['resources'][0]['resource_key'],'fed-bank-ai-testimony')
        for q in ('AI数据中心资本支出','英国金融行业AI应用情况','欧洲银行人工智能应用现状'):
            r=nav.prepare(q,{},search_origin='');self.assertNotIn('fed-bank-ai-testimony',[s['resource_key'] for s in r['resources']])
    def test_correct_regions_and_exact_snippets_not_navigation(self):
        t=helpers.NavigationTransportTests();self.addCleanup(t.doCleanups)
        f=t.factory(lambda r:httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(200,headers={'content-type':'text/html'},text=HTML))
        r=nav.prepare(FAILED,{},enabled=True,reader_factory=f,search_origin='')
        self.assertEqual(len(r['resources']),2)
        for s in r['resources']:
            self.assertEqual(s['status'],'read');self.assertTrue(s['evidence_snippets']);self.assertNotIn('Navigation links',s['text'])
            self.assertEqual(s['jurisdiction'],'US');self.assertFalse(s['data_verified'])
            for quote in s['evidence_snippets']:self.assertEqual(s['text'][quote['start']-s['text_start']:quote['end']-s['text_start']],quote['text'])
        c=helpers.context();c['source_lookup']=r;a=nav.answer(c);self.assertIn('投资金额仍未取得',a['summary_zh'])
        self.assertEqual(validate_answer(c,a),a)
        bad=copy.deepcopy(a);bad['source_resources'][0]['evidence_snippets'][0]['text']='invented quote'
        with self.assertRaises(ValueError):validate_answer(c,bad)
    def test_missing_article_body_never_reports_a_read_document(self):
        t=helpers.NavigationTransportTests();self.addCleanup(t.doCleanups)
        f=t.factory(lambda r:httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(200,headers={'content-type':'text/html'},text=helpers.HTML))
        r=nav.prepare(FAILED,{},enabled=True,reader_factory=f,search_origin='')
        self.assertTrue(all(s['status']=='unavailable' for s in r['resources']))

if __name__=='__main__':unittest.main()
