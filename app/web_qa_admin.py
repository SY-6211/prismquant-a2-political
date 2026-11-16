"""Local-only finite allowance management. Never accepts or prints API keys."""
import argparse,json
from uuid import UUID
from app.core.config import Settings
from app.model_trial_live import units
from app.repositories import web_qa as repo
from app.web_qa import service,recover
def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    grant=sub.add_parser('grant');grant.add_argument('--user-id',type=UUID,required=True);grant.add_argument('--approval',required=True);grant.add_argument('--cap-usd',required=True);grant.add_argument('--max-requests',type=int,required=True);grant.add_argument('--hours',type=int,default=2);grant.add_argument('--approved',action='store_true',required=True)
    summary=sub.add_parser('summary');summary.add_argument('--user-id',type=UUID,required=True)
    close=sub.add_parser('close');close.add_argument('--grant-id',type=UUID,required=True)
    repair=sub.add_parser('recover');repair.add_argument('--run-id',type=UUID,required=True)
    cancel=sub.add_parser('cancel-queued');cancel.add_argument('--run-id',type=UUID,required=True)
    args=p.parse_args()
    try:
        settings=Settings()
        if args.action=='grant':value=service(settings,repo.grant,args.user_id,args.approval,units(args.cap_usd),args.max_requests,args.hours)
        elif args.action=='summary':value=service(settings,repo.summary,args.user_id)
        elif args.action=='close':value=service(settings,repo.close,args.grant_id)
        elif args.action=='cancel-queued':value=service(settings,repo.cancel_queued,args.run_id)
        else:value=recover(settings,args.run_id)
        print(json.dumps(value,ensure_ascii=False,indent=2,default=str));return 0
    except Exception:print('操作未完成。请核对授权、用户、数据库和原费用回执；不会自动重复模型请求。');return 1
if __name__=='__main__':raise SystemExit(main())
