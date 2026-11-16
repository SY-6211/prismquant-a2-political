"""Free, bounded reminder processing; no source fetching or model API access."""
import argparse,asyncio,json
from uuid import UUID
from app.core.config import Settings
from app.repositories import agent_analysis,reminders as repo

def service(settings,fn,*args):
    engine=agent_analysis.write_engine(settings)
    try:
        with engine.begin() as c:return fn(c,*args)
    finally:engine.dispose()

async def loop(settings):
    while True:
        try:await asyncio.to_thread(service,settings,repo.process)
        except asyncio.CancelledError:raise
        except Exception:pass # Status heartbeat becomes stale; no false success or private error output.
        await asyncio.sleep(settings.reminder_poll_seconds)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    for name in ('status','enable','disable','run'):sub.add_parser(name)
    config=sub.add_parser('configure');config.add_argument('--rule-id',type=UUID,required=True);config.add_argument('--cooldown-seconds',type=int,default=300);config.add_argument('--dedup-seconds',type=int,default=86400)
    review=sub.add_parser('review');review.add_argument('--reminder-id',type=UUID,required=True);review.add_argument('--decision',choices=('approve','false_positive','restore'),required=True);review.add_argument('--note',required=True);review.add_argument('--expected-revision',type=int,required=True)
    args=p.parse_args(argv)
    try:
        settings=Settings()
        if args.action=='status':value=agent_analysis.read(settings,repo.summary)
        elif args.action=='configure':value=service(settings,repo.configure,args.rule_id,args.cooldown_seconds,args.dedup_seconds)
        elif args.action=='review':value=service(settings,repo.review,args.reminder_id,args.decision,args.note,args.expected_revision)
        else:value=service(settings,{'enable':repo.enable,'disable':repo.disable,'run':repo.process}[args.action])
        print(json.dumps(value,ensure_ascii=False,indent=2,default=str));return 0
    except Exception:print('提醒操作未完成，请核对规则发布、迁移与审核版本。没有调用模型或访问来源。');return 1

if __name__=='__main__':raise SystemExit(main())
