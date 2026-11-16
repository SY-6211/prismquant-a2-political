"""Bounded task routing. History is used for intent only, never as source evidence."""
import re

REVISION=re.compile(r'原文.{0,10}(?:修订|改动|改写|版本)|保存(?:的|过的)?版本|(?:这条|这个|该).{0,8}(?:帖|发言).{0,8}(?:修订|改动|版本)|\b(?:original|saved|source|statement).{0,16}(?:revision|version|edit)',re.I)
VERIFY=re.compile(r'核对.{0,8}(?:原文|引文|出处)|(?:原文|讲话).{0,10}(?:提到|列出|写了).{0,10}(?:数据|指标)|哪[句段]|原句|英文原话|验证引用|出处|上下文|\bverbatim\b|\bverify\b.{0,12}\b(?:quote|source)\b',re.I)
CHECKLIST=re.compile(r'(?:哪些|什么|哪类|何种).{0,24}(?:数据|资料|指标|信息)|(?:还要|需要|下一步|怎么|如何|哪里).{0,16}(?:核对|验证|查证|查找|资料|数据|指标)|(?:数据|资料|指标).{0,16}(?:用途|作用|哪里找)|\b(?:which|what)\b.{0,50}\b(?:data|metrics|information)\b',re.I)
FINANCE=re.compile(r'股票|投资|金融|行业|公司|市场|企业|经济|银行|保险|券商|证券|\b(?:stock|investment|financial|company|market|sector|bank|insurer)\b',re.I)
IMPACT=re.compile(r'(?:影响|传导|受益|承压|利好|利空|风险|机会).{0,18}(?:股|行业|市场|企业|公司|投资|经济)|(?:股|行业|市场|企业|公司|投资|经济).{0,18}(?:影响|传导|受益|承压|利好|利空|上涨|下跌|造成|涨幅|收益|盈利)|\b(?:market|sector|stock|investment)\b.{0,24}\b(?:impact|effect|risk)\b',re.I)


SOURCE_FIND=re.compile(r'补查|查找缺失|哪里(?:找|查|获取|下载)|到哪(?:里|儿)|在哪(?:里)?(?:找|查|获取|下载)|(?:怎么|如何)(?:找|查找|获取|下载)|(?:数据|资料|财报).{0,12}(?:入口|网站|链接)|\bwhere\b.{0,40}\b(?:find|get|data|sources?)\b',re.I)


def route(question,requested='auto',memory=None):
    memory=memory or {}
    seed=memory.get('routing_seed') if memory.get('followup_detected') and not memory.get('topic_reset') else None
    prior=seed if seed and seed.get('strategy') in ('research','evidence','summary','compare','focused') and seed.get('task') in ('source_lookup','method_checklist','impact_analysis','evidence','summary','compare','focused') else route(seed['question']) if seed else None
    prior_research=bool(prior and prior['strategy']=='research')
    method=bool(CHECKLIST.search(question) and (FINANCE.search(question) or prior_research))
    source_find=bool(SOURCE_FIND.search(question) and (prior_research or FINANCE.search(question) or re.search(r'数据|资料|财报|讲话|发言|data|sources?',question,re.I)))
    def result(strategy,task,origin='local_intent_rules',inherited=False):
        return dict(strategy=strategy,task=task,selected_by=origin,inherited_from_history=inherited)
    reanalyze=bool(re.search(r'(?:结合|根据|利用).{0,24}(?:资料|正文|背景|来源).{0,16}(?:重新|综合|继续)?分析|\breanaly[sz]e\b',question,re.I))
    if requested!='auto':
        if requested=='research' and reanalyze:return result('research','impact_analysis','explicit_synthesis')
        if requested=='research' and source_find:return result('research','source_lookup','explicit')
        task='method_checklist' if requested=='research' and (method or prior and prior['task']=='method_checklist' and not IMPACT.search(question) and not VERIFY.search(question)) else 'impact_analysis' if requested=='research' else requested
        return result(requested,task,'explicit')
    if REVISION.search(question) or VERIFY.search(question):return result('evidence','evidence')
    if reanalyze:return result('research','impact_analysis','background_synthesis')
    if source_find:return result('research','source_lookup','official_source_navigation',prior_research)
    if method:return result('research','method_checklist','local_research_checklist_rules',prior_research and not bool(FINANCE.search(question)))
    if IMPACT.search(question) or FINANCE.search(question) and re.search(r'影响|风险|机会|\b(?:impact|risk|opportunit\w*)\b',question,re.I):return result('research','impact_analysis')
    if re.search(r'比较|对比|相比|异同|立场变化|\bcompare\b|\bdifferen',question,re.I):return result('compare','compare')
    if re.search(r'概括|归纳|总结|整体|有哪些|\bsummar|\boverview\b',question,re.I):return result('summary','summary')
    if prior_research and re.search(r'^(?:那|那么)?(?:风险|机会|影响|成立条件)呢',question):return result('research','impact_analysis','related_impact_followup',True)
    if prior_research:return result('research',prior['task'],'related_user_question',True)
    if prior and prior['strategy'] in ('evidence','summary','compare'):return result(prior['strategy'],prior['task'],'related_user_question',True)
    if re.search(r'核对|\bverify\b|\bcontext\b|\bevidence\b',question,re.I):return result('evidence','evidence')
    return result('focused','focused')
