"""Offline task regression and optional owner-scoped saved-conversation reads; no model calls."""
import argparse,json
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID,uuid4
from sqlalchemy import text
from app.core.config import Settings,PROJECT_ROOT
from app import agent_memory,agent_intent
from app.agent_qa import Prepare,prepare_context,request_payload
from app.agent_tools import open_session
from app.agent_qa_live import read_credential
from app.annotation_model import digest
from app.repositories import chat,qa
from app.web_qa import service


def check(conversation_id=None):
    data=json.loads((PROJECT_ROOT/'app/data/research_workflow_cases.json').read_text(encoding='utf8'))
    report=dict(checked_at=datetime.now(timezone.utc).isoformat(),model_requests=0,model_cost_usd='0',real_generation_verified=False,quality_score=None,cases=[],real_preparations=[],historical_requests=[])
    for case in data['cases']:
        history=[dict(position=i+1,question=q) for i,q in enumerate(case['history'])]
        _,memory=agent_memory.select(case['question'],history)
        selected=agent_intent.route(case['question'],case['requested'],memory)
        report['cases'].append(dict(id=case['id'],passed=(selected['strategy'],selected['task'])==(case['strategy'],case['task']),selected=selected))
    if conversation_id:
        settings=Settings();token=read_credential(PROJECT_ROOT/'.runtime/chat-credentials/local-user.json')
        def read(c):
            owner=chat.authenticate(c,token);chat.conversation(c,owner['user_id'],conversation_id)
            messages=c.execute(text('SELECT message_id FROM agent_chat_messages WHERE conversation_id=:c ORDER BY position LIMIT 2'),{'c':conversation_id}).scalars().all()
            questions=[qa.question(c,owner['user_id'],conversation_id,m) for m in messages]
            old=list(c.execute(text('SELECT r.context,j.request_hash FROM agent_qa_runs r JOIN agent_web_qa_jobs j USING(run_id) WHERE r.conversation_id=:c AND j.request_hash IS NOT NULL'),{'c':conversation_id}).mappings())
            return questions,old
        questions,old=service(settings,read)
        for row in old:report['historical_requests'].append(dict(prompt_version=row['context']['prompt_version'],hash_preserved=digest(request_payload(row['context']))==row['request_hash']))
        for q in questions:
            body=Prepare(client_request_id=uuid4(),message_id=q['message_id'],retrieval_mode='hybrid',limit=6)
            with open_session(settings,[q['person_id']]) as session:context=prepare_context(session,q,body)
            request_payload(context)
            def verify(c):
                checks=[]
                for source in context['sources']:
                    body=c.execute(text('SELECT payload FROM event_versions WHERE version_id=:v'),{'v':UUID(source['version_id'])}).scalar_one()['main_text']
                    checks.append(dict(start=source['start'],end=source['end'],characters=len(source['text']),exact_saved_substring=body[source['start']:source['end']]==source['text'],context_window=source.get('context_window'),first_words=source['text'][:70]))
                return checks
            report['real_preparations'].append(dict(question_position=q['position'],strategy=context['plan']['strategy'],task=context.get('research_task'),prompt_version=context['prompt_version'],selected_by=context['plan']['selected_by'],memory_selected=context['memory']['selected'],source_checks=service(settings,verify),tool_calls=len(context['tool_audit']),context_bytes=len(json.dumps(context,ensure_ascii=False).encode()),extra_reads=context['plan']['extra_reads']))
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--conversation-id',type=UUID);args=p.parse_args()
    if args.out.exists():p.error('Output already exists')
    try:
        result=check(args.conversation_id);args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        success=all(x['passed'] for x in result['cases']) and all(x['hash_preserved'] for x in result['historical_requests']) and all(s['exact_saved_substring'] for r in result['real_preparations'] for s in r['source_checks'])
        print(json.dumps(dict(passed=success,route_cases=len(result['cases']),historical_requests=len(result['historical_requests']),real_preparations=result['real_preparations'],model_requests=0),ensure_ascii=False))
        return 0 if success else 1
    except Exception:
        print(json.dumps(dict(status='requires_review',model_requests=0,message='离线核对未完成；未显示连接参数或凭证。'),ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
