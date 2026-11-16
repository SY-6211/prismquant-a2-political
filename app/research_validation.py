"""Real saved-text retrieval and prompt preparation only. Never calls a model or saves conversations."""
import argparse,json
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
from app.core.config import Settings,PROJECT_ROOT
from app.source_catalog import execute
from app.monitoring_status import summary
from app.agent_tools import open_session,encoded
from app.agent_qa import Prepare,prepare_context,request_payload
from app import agent_plan
from app.annotation_model import digest


def check(settings):
    sources=execute(settings,summary,None,{},settings.fast_discovery_enabled,readonly=True)['items']
    report=dict(checked_at=datetime.now(timezone.utc).isoformat(),model_requests=0,model_cost_usd='0',
                model_quality_evaluated=False,quality_score=None,real_retrieval=[],development_routes=[])
    for key,question in [('fed','根据已保存原文，人工智能发展对金融行业可能有什么影响？'),
                         ('trump','根据已保存原文，关税表态对美国汽车行业可能有什么影响？')]:
        row=next((r for r in sources if r['source']==key and r['latest_saved']),None)
        if not row:continue
        body=Prepare(client_request_id=uuid4(),message_id=uuid4(),retrieval_mode='hybrid',research_mode='research',limit=4)
        question=dict(person_id=row['person_id'],conversation_id=uuid4(),message_id=body.message_id,content=question,position=1,history=[])
        with open_session(settings,[row['person_id']]) as session:context=prepare_context(session,question,body)
        payload=request_payload(context)
        report['real_retrieval'].append(dict(source=key,person_name=row['person_name'],question=question['content'],
            prompt_contract=context['prompt_contract'],context_sha256=digest(context),request_sha256=digest(payload),
            retrieval=context['retrieval'],source_count=len(context['sources']),tool_calls=len(context['tool_audit']),
            context_bytes=len(encoded(context)),source_versions=[dict(event_id=s['event_id'],version_id=s['version_id'],
            source_ref=s['source_ref'],text_sha256=s['text_sha256']) for s in context['sources']],
            prepared_only=True))
    cases=json.loads((PROJECT_ROOT/'app/data/research_review_cases.json').read_text(encoding='utf8'))
    for case in cases['cases']:
        strategy,_=agent_plan.strategy_for(case['question'])
        report['development_routes'].append(dict(**case,selected_strategy=strategy,route_passed=strategy==case['expected_strategy'],
                                               generated_answer=None,human_review=None))
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);args=p.parse_args()
    if args.out.exists():p.error('Output already exists')
    try:
        result=check(Settings());args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf8')
        print(json.dumps(dict(real_retrieval_cases=len(result['real_retrieval']),source_counts=[r['source_count'] for r in result['real_retrieval']],
            route_checks_passed=sum(c['route_passed'] for c in result['development_routes']),model_requests=0,quality_score=None),ensure_ascii=False))
        return 0 if all(c['route_passed'] for c in result['development_routes']) and len(result['real_retrieval'])==2 else 1
    except Exception:
        print(json.dumps(dict(status='requires_review',model_requests=0,message='离线准备未完成；未显示连接参数或凭证。'),ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
