"""Read-only multi-person retrieval cases. Never generate or score model answers."""
import argparse,json
from uuid import UUID,uuid4
from pathlib import Path
from app.core.config import Settings
from app.agent_tools import open_session
from app.agent_qa import Prepare,prepare_context

CASES=(('summary','概括已保存讲话中关于经济政策的观点。'),('research','这些政策可能怎样影响银行和金融行业？'),('compare','比较两次已保存发言，哪些观点不同？'),('auto','银行人工智能监管资料去哪里找？'),('research','接着刚才，研究相关股票先核对哪些公司数据？'),('auto','换个话题，研究能源行业的政策风险。'))

def run(people):
 rows=[]
 for person in people:
  history=[]
  for index,(mode,question) in enumerate(CASES):
   message=uuid4();q=dict(person_id=person,conversation_id=uuid4(),message_id=message,position=index+1,content=question,history=history)
   b=Prepare(client_request_id=uuid4(),message_id=message,retrieval_mode='hybrid',research_mode=mode,use_history=True,limit=6)
   with open_session(Settings(),[person]) as session:c=prepare_context(session,q,b)
   rows.append(dict(person_id=str(person),question=question,task=c.get('research_task',c['plan']['strategy']),sector=c['memory'].get('research_focus'),sources=len(c['sources']),person_scope_correct=all(x['person_id']==str(person) for x in c['sources']),model_requests=0,quality='not_evaluated'))
   history.append(dict(question=question,message_id=message,position=index+1))
 return {'cases':rows,'model_requests':0,'business_writes':0,'answer_quality_evaluated':False,'held_out_questions':['同一政策在不同实施情景下，对地区银行与跨国银行有何不同影响？','第二项指标的单位、期间和可比口径怎么核对？','如果当前原文只有一侧观点，能否证明立场变化？']}

def main():
 p=argparse.ArgumentParser();p.add_argument('--person-id',type=UUID,action='append',required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
 if a.out.exists() or not 1<=len(a.person_id)<=3:raise ValueError('New output and one to three people required')
 a.out.write_text(json.dumps(run(a.person_id),ensure_ascii=False,indent=2),encoding='utf8')
 print(json.dumps({'status':'saved','cases':len(a.person_id)*len(CASES),'model_requests':0}))
if __name__=='__main__':main()
