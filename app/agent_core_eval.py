"""Offline synthetic contracts for memory, strategy routing and prompt manifests."""
import argparse,json
from pathlib import Path
from app.agent_memory import select
from app.agent_plan import strategy_for
from app.qa_prompts import manifest,SUPPORTED,STRATEGIES

def evaluate():
    history=[{'position':i+1,'question':q,'answer_zh':'Synthetic old answer; not evidence.'} for i,q in enumerate(('人工智能金融风险','关税贸易','利率通胀'))]
    results=[]
    for name,q,expected in [('older_relevant','继续解释之前的人工智能风险',[1]),('topic_reset','换个话题，只讨论能源',[]),('last_subject_followup','那对股市有什么影响？',[2,3])]:
        rows,memory=select(q,history)
        results.append({'case':name,'kind':'synthetic_memory_contract','passed':[r['position'] for r in rows]==expected,
                        'selected_positions':[r['position'] for r in rows],'last_two_baseline_positions':[2,3],
                        'history_is_evidence':memory['history_is_evidence']})
    for q,expected in [('概括讲话的要点','summary'),('比较两个时期并核对上下文','compare'),('核对原文上下文','evidence'),('模型version 3提到了什么','focused'),('原文版本有修订吗','evidence')]:
        actual,_=strategy_for(q);results.append({'case':q,'kind':'synthetic_strategy_contract','passed':actual==expected,'selected':actual})
    manifests=[manifest(v,s) for v in SUPPORTED for s in (STRATEGIES if v==SUPPORTED[-1] else ['focused'])]
    return {'evaluation':'synthetic_contracts_only','cases':results,'passed':sum(r['passed'] for r in results),'total':len(results),
            'prompt_manifests':manifests,'model_requests':0,'source_fetches':0,'independent_accuracy_evaluation':False,
            'limitations':'These synthetic checks are not a real-world accuracy, recall, reasoning or investment-performance score.'}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path);args=p.parse_args(argv)
    value=evaluate()
    if args.out:
        with args.out.open('x',encoding='utf8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
        print(json.dumps({'saved':str(args.out),'passed':value['passed'],'total':value['total'],'model_requests':0},ensure_ascii=False))
    else:print(json.dumps(value,ensure_ascii=False,indent=2))
    return 0 if value['passed']==value['total'] else 1

if __name__=='__main__':raise SystemExit(main())
