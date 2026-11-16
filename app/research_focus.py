"""Conversation topic state from user questions only. Never factual evidence."""
import re

SECTORS=(('banking','银行与金融行业',r'银行|金融|保险|券商|bank|financ|insur'),('energy','能源行业',r'能源|石油|天然气|energy|oil|gas'),('chips','半导体行业',r'半导体|芯片行业|semiconductor|chip sector'))
RESET=re.compile(r'换[个一]?话题|不再讨论|忽略之前|new topic|start over',re.I)

def select(question,history=()):
    sector=None;origin=None
    for row in [*history,{'question':question,'message_id':None}]:
        text=str(row.get('question') or '')
        if RESET.search(text):sector=origin=None
        for key,label,pattern in SECTORS:
            if re.search(pattern,text,re.I):sector={'key':key,'label_zh':label};origin=str(row.get('message_id') or 'current_question');break
    return {'version':'user_topic_focus_v1','sector':sector,'from_user_message':origin,'used_as_evidence':False}

def scope_errors(context,answer):
    focus=context.get('memory',{}).get('research_focus',{})
    if focus.get('sector',{} ) is None or focus.get('sector',{}).get('key')!='banking':return []
    text=' '.join(str(s.get('data_zh','')) for s in answer.get('research_steps',[]))
    # A bank's cloud spending is valid; replacing bank research with the market
    # shares of chip/cloud vendors requires an explicit user scope change.
    return ['unrequested_vendor_sector'] if re.search(r'AI芯片|半导体.{0,8}市场份额|公有云.{0,12}市场份额|(?:IaaS|PaaS)',text,re.I) else []
