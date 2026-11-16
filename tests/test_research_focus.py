import unittest
from app.research_focus import select,scope_errors
from app import agent_memory,research_sources as nav

class FocusTests(unittest.TestCase):
 def test_generic_followup_keeps_sector_over_many_turns(self):
  history=[{'question':'AI对银行和金融行业有什么影响？'},{'question':'需要哪些公司数据？'},{'question':'这些数据在哪里找？'}]
  _,m=agent_memory.select('接着刚才，相关股票先研究什么？',history)
  self.assertEqual(m['research_focus']['sector']['key'],'banking');self.assertIn('银行',m['retrieval_hint'])
 def test_explicit_topic_change_and_history_off_clear_old_sector(self):
  self.assertEqual(select('换个话题，研究能源行业',[{'question':'银行AI影响'}])['sector']['key'],'energy')
  self.assertIsNone(select('相关股票怎么研究',[])['sector'])
 def test_previous_model_answer_never_sets_sector(self):
  self.assertIsNone(select('这些数据',[{'question':'概括讲话','answer_zh':'银行金融行业'}])['sector'])
 def test_bank_cloud_cost_allowed_but_vendor_market_share_rejected(self):
  c={'memory':{'research_focus':select('银行行业')}}
  self.assertFalse(scope_errors(c,{'research_steps':[{'data_zh':'银行的云服务支出、员工成本和投入期间'}]}))
  self.assertTrue(scope_errors(c,{'research_steps':[{'data_zh':'AI芯片以及公有云IaaS市场份额'}]}))
 def test_company_document_selected_only_when_named(self):
  r=nav.prepare('摩根大通的AI投入有哪些官方披露？',{},search_origin='')
  self.assertEqual(r['resources'][0]['resource_key'],'jpm-ai-disclosure')
  self.assertNotIn('jpm-ai-disclosure',[x['resource_key'] for x in nav.prepare('银行人工智能应用',{},search_origin='')['resources']])
 def test_company_body_excludes_navigation_and_keeps_unknown_publication_date(self):
  import httpx
  from test_research_sources import NavigationTransportTests
  t=NavigationTransportTests();self.addCleanup(t.doCleanups)
  html='<html><head><title>Company annual letter</title></head><body><article><h1>MENU ONLY</h1>Links</article><div class="cmp-text"><p>'+('Artificial intelligence supports banks in fraud detection and coding. '*8)+'</p></div></body></html>'
  reader=t.factory(lambda r:httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(200,headers={'content-type':'text/html'},text=html))
  value=nav.prepare('摩根大通 AI披露',{},enabled=True,max_reads=1,reader_factory=reader,search_origin='')['resources'][0]
  self.assertEqual(value['status'],'read');self.assertNotIn('MENU ONLY',value['text']);self.assertIsNone(value['source_date']);self.assertEqual(value['reporting_period'],'2025')

if __name__=='__main__':unittest.main()
