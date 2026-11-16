"""Finite source-navigation acceptance. Optional authorized public-page reads; zero model calls."""
import argparse
import json
from pathlib import Path
from uuid import UUID
from sqlalchemy import text
from app import agent_memory,agent_intent,research_sources
from app.core.config import PROJECT_ROOT,Settings


def run(*,question='公司资本支出数据去哪里找？',conversation_id=None,live=False):
    data=json.loads((PROJECT_ROOT/'app/data/source_navigation_cases.json').read_text(encoding='utf8'))
    checks=[]
    for case in data['cases']:
        _,memory=agent_memory.select(case['question'],[dict(position=i+1,question=q) for i,q in enumerate(case['history'])])
        route=agent_intent.route(case['question'],memory=memory)
        checks.append(dict(id=case['id'],passed=(route['strategy'],route['task'])==(case['strategy'],case['task'])))
    history=[]
    if conversation_id:
        from app.agent_qa_live import read_credential
        from app.web_qa import service
        from app.repositories import chat,qa
        credential=read_credential(PROJECT_ROOT/'.runtime/chat-credentials/local-user.json')
        def read(c):
            owner=chat.authenticate(c,credential)['user_id'];chat.conversation(c,owner,conversation_id)
            message=c.execute(text('SELECT message_id FROM agent_chat_messages WHERE conversation_id=:c ORDER BY position DESC LIMIT 1'),{'c':conversation_id}).scalar_one()
            return qa.question(c,owner,conversation_id,message)['history']
        history=service(Settings(),read)
    _,memory=agent_memory.select(question,history)
    lookup=research_sources.prepare(question,memory,enabled=live)
    return dict(checks=checks,development_cases=len(checks),reserved_cases_not_run=6,
                independent_blind_test=False,quality_score=None,model_requests=0,model_cost_usd='0',
                live_public_reads=live,lookup=lookup,history_items=len(memory['research_state']['items']))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--question',default='公司资本支出数据去哪里找？')
    parser.add_argument('--conversation-id',type=UUID)
    parser.add_argument('--live',action='store_true',help='本轮最多读取三个固定官方入口；不调用模型')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.out.exists() or not 1<=len(args.question)<=4000:parser.error('Use a new output file and bounded question')
    result=run(question=args.question,conversation_id=args.conversation_id,live=args.live)
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(development_cases=result['development_cases'],passed=sum(r['passed'] for r in result['checks']),
                         history_items=result['history_items'],page_statuses=[{'key':r['resource_key'],'status':r['status']} for r in result['lookup']['resources']],
                         model_requests=0,network_requests=result['lookup']['network_requests']),ensure_ascii=False))
    return 0 if all(r['passed'] for r in result['checks']) else 1


if __name__=='__main__':raise SystemExit(main())
