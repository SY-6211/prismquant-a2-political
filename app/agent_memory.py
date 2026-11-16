"""Small, owner-scoped conversation working memory, never factual evidence."""
import re
from app.rag import terms_for

VERSION='conversation_memory_v1'
MAX_SCAN=50
MAX_SELECTED=4
FOLLOWUP=re.compile(r'^(?:请)?(?:帮我)?(?:补查|查找缺失)|第\s*[一二三四五六1-6]\s*(?:项|条|个)|刚才|上面|上述|之前|上一[条次轮个]|前面|这些|那些|这个|那个|继续|接着|其[中它]|^那[对会有还]|^那(?:风险|机会|条件|影响)|^(?:风险|机会|条件|影响)呢|^它|^还需要|^需要哪些|^如何验证|^怎么核对|^到哪里找|再(?:解释|说明|讲|说)|然后呢|\b(it|those|these|previous|earlier|continue|follow.?up)\b',re.I)
RESET=re.compile(r'换[个一]?话题|不再讨论|忽略之前的话题|(?:不要参考|不参考)(?:之前|前文)|new topic|change (?:the )?topic|start over',re.I)
RECENT_REFERENCE=re.compile(r'刚才|上一(?:条|次|轮)|^(?:这些|上述|那(?:风险|机会|影响|条件))|\b(?:just discussed|last answer)\b',re.I)
GENERIC={'原文','请解释','请说明','这些','那些','这个','那个','继续','什么','为什么','有哪些','政策','问题','看看','谢谢','比较','请问','continue','previous','earlier','explain','please'}


def tokens(value):
    return {t for t in terms_for(value) if t not in GENERIC and len(t)>1}


def informative(value):
    value=FOLLOWUP.sub('',value)
    value=re.sub(r'请|解释|说明|详细|一下|进一步|补充|事件|为什么|怎么|还有|呢|吗|说说|谢谢|你好|您好|好的|再|点|\b(?:hello|hi|thanks?|please|explain(?:ing)?|more|detail|okay|ok)\b','',value,flags=re.I)
    return tokens(value)


def select(question,history,window_truncated=False):
    history=list(history)[-MAX_SCAN:]
    resets=[i for i,row in enumerate(history) if RESET.search(str(row.get('question') or ''))]
    if resets:history=history[resets[-1]:]
    reset=bool(RESET.search(question));followup=not reset and bool(FOLLOWUP.search(question))
    query_terms=tokens(question);ranked=[]
    for index,row in enumerate(history):
        previous=str(row.get('question') or '')
        overlap=len(query_terms & tokens(previous))
        score=overlap*10+(3 if followup and index==len(history)-1 else 1 if followup and index==len(history)-2 else 0)
        if score and not reset:
            ranked.append((score,index,row))
    if any(r[0]>=10 for r in ranked):ranked=[r for r in ranked if r[0]>=10]
    if followup:
        related=[r for r in ranked if r[0]>=10 and informative(str(r[2].get('question') or ''))]
        latest=next(((5,i,r) for i,r in reversed(list(enumerate(history))) if informative(str(r.get('question') or ''))),None)
        seed_row=latest if RECENT_REFERENCE.search(question) else next(reversed(related),None) if related else latest
        if seed_row and RECENT_REFERENCE.search(question):
            seed_row=(max([v[0] for v in ranked]+[5])+1,seed_row[1],seed_row[2])
            ranked=[r for r in ranked if r[1]!=seed_row[1]]
        if seed_row and all(r[1]!=seed_row[1] for r in ranked):ranked.append(seed_row)
    chosen=sorted(sorted(ranked,key=lambda r:(-r[0],-r[1]))[:MAX_SELECTED],key=lambda r:r[1])
    entries=[]
    for score,index,row in chosen:
        entries.append({k:row[k] for k in ('message_id','position','answer_run_id') if row.get(k) is not None}|
            {'question':str(row.get('question') or '')[:500],
             'answer_zh':str(row['answer_zh'])[:600] if row.get('answer_zh') else None,
             'selection_score':score,'used_as_evidence':False,
             'question_truncated':bool(row.get('question_truncated') or len(str(row.get('question') or ''))>500),
             'answer_truncated':len(str(row.get('answer_zh') or ''))>600})
    # Only prior USER questions form a retrieval hint. Earlier model answers are
    # never promoted into facts or query assertions by this policy.
    seed=next((row for _,_,row in reversed(chosen) if informative(str(row.get('question') or ''))),None) if followup else None
    routing_seed=None
    if seed:
        from app.agent_intent import route
        state=None
        for row in history:
            q=str(row.get('question') or '')[:500]
            state={**route(q,memory={'followup_detected':bool(FOLLOWUP.search(q)),
                                    'topic_reset':bool(RESET.search(q)),'routing_seed':state}), 'question':q}
            if row is seed:routing_seed=state;break
    hint=(str(seed['question'])[:220]+'\n追问：'+question[:270])[:500] if seed else question[:500]
    metadata={
        'version':VERSION,'scope':'same_owner_conversation_person','scanned':len(history),'scan_limit':MAX_SCAN,
        'window_truncated':bool(window_truncated),'selected':len(entries),'selection_limit':MAX_SELECTED,
        'followup_detected':followup,'topic_reset':reset,'query_expanded_from_user_history':bool(seed),
        'hint_message_id':str(seed['message_id']) if seed and seed.get('message_id') else None,
        'unresolved_followup':bool(followup and not seed and not informative(question)),
        'history_is_evidence':False,'retrieval_hint':hint,'omitted_for_context_budget':0,
        'routing_seed':routing_seed}
    from app.research_sources import state
    from app.research_focus import select as focus
    metadata['research_focus']=focus(question,history if not reset else [])
    if followup and metadata['research_focus']['sector']:
        metadata['retrieval_hint']=(metadata['research_focus']['sector']['label_zh']+'\n'+hint)[:500]
    metadata['research_state']=state(question,history,{**metadata,'selected_entries':entries})
    return entries,metadata
