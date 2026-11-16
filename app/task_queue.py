"""Explicit finite queue operations. Importing this module starts no workers."""
import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.collection_rules import SUPPORTED
from app.collection_runs import engine_for
from app.core.config import Settings
from app.core.database import create_read_engine
from app.repositories.tasks import TaskLeaseLost, claim, enqueue, retry_reviewed, select_task, select_stats
from app.task_types import LANES, Outcome


def work(settings, lanes=('raw',), max_tasks=1, *, engine=None, handler=None):
    if type(max_tasks) is not int or not 1 <= max_tasks <= 100:
        raise ValueError('Worker batch must be 1..100')
    from app.task_jobs import execute_task
    owned_engine = engine is None
    engine = engine or engine_for(settings)
    results = []
    worker_id = uuid4()
    idle = False
    try:
        for _ in range(max_tasks):
            lease = claim(engine, lanes, worker_id=worker_id)
            if lease is None:
                idle = True
                break
            try:
                try:
                    outcome = (handler or execute_task)(settings, lease)
                except KeyboardInterrupt:
                    # Leave a recoverable lease; do not assert that a commit did not happen.
                    raise
                except TaskLeaseLost:
                    results.append({'task_id': str(lease.task['task_id']), 'status': 'lease_lost'})
                    break
                except Exception:
                    # Unknown code/config/write failures need review; no credentials or raw exception text.
                    outcome = Outcome('needs_review', 'worker_exception')
                results.append(lease.finish(outcome))
                if outcome.stop_worker:
                    break
            finally:
                lease.close()
    finally:
        if owned_engine:
            engine.dispose()
    return {'mode': 'task_worker', 'worker_id': str(worker_id), 'processed': len(results), 'items': results,
            'idle': idle, 'lanes': list(lanes)}


def enqueue_history(connection, plan, limit=20, *, apply=False):
    from app.backfill import validate_plan, source_tools
    from app.task_types import normalized_payload, payload_key
    from sqlalchemy import text
    plan = validate_plan(plan)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('History enqueue limit must be 1..100')
    items = []
    # Validate the entire declared file before writing; an invalid file cannot
    # expand the allowed date range or send an off-host URL to a worker.
    candidates = []
    batch, _ = source_tools(plan['source'])
    prepared = batch.prepare_candidates(plan['candidates'])
    for entry in prepared['candidates']:
        payload = {'source': plan['source'], 'since': plan['since'], 'until': plan['until'], 'entry': entry}
        payload, _, _ = normalized_payload('history_item', payload)
        candidates.append(payload)
    sid = SUPPORTED[plan['source']][0]
    urls = [p['entry']['url'] for p in candidates]
    keys = [payload_key('history_item', p, 'history:' + p['entry']['url']) for p in candidates]
    known_urls = set(connection.execute(text('SELECT retrieved_url FROM public_records WHERE source_id=:id '
        'AND retrieved_url=ANY(CAST(:urls AS text[]))'), {'id': sid, 'urls': urls}).scalars())
    known_keys = set(connection.execute(text("SELECT idempotency_key FROM collection_tasks WHERE source_id=:id AND kind='history_item' "
        'AND idempotency_key=ANY(CAST(:keys AS text[]))'), {'id': sid, 'keys': keys}).scalars())
    pending = [payload for payload in candidates if payload['entry']['url'] not in known_urls and
               payload_key('history_item', payload, 'history:' + payload['entry']['url']) not in known_keys]
    for payload in pending[:limit]:
        if apply:
            result = enqueue(connection, 'history_item', payload, 'history:' + payload['entry']['url'])
            items.append(result)
    return {'selected': min(len(pending), limit), 'eligible_candidates': len(candidates), 'limit': limit,
            'pending_after_batch': max(0, len(pending)-limit), 'skipped_existing': sum(p['entry']['url'] in known_urls for p in candidates),
            'skipped_submitted': sum(payload_key('history_item', p, 'history:' + p['entry']['url']) in known_keys for p in candidates),
            'applied': apply, 'items': items, 'network_requested': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    schedule = sub.add_parser('schedule', help='默认预览到期任务，--apply 才入队；不访问来源')
    schedule.add_argument('--source', choices=tuple(SUPPORTED))
    schedule.add_argument('--limit', type=int, default=3)
    schedule.add_argument('--apply', action='store_true')
    schedule.add_argument('--ecb-index',action='store_true',help='ECB 使用索引＋RSS 发现模式')
    history = sub.add_parser('enqueue-history', help='从已发现的历史计划导入独立任务，不读取远端索引')
    history.add_argument('--plan', type=Path, required=True)
    history.add_argument('--limit', type=int, default=20)
    history.add_argument('--apply', action='store_true')
    worker = sub.add_parser('worker', help='运行有限个已有任务；raw 与 history 建议分开启动')
    worker.add_argument('--lane', choices=LANES, action='append', default=None)
    worker.add_argument('--max-tasks', type=int, default=1)
    sub.add_parser('status', help='只读查看各队列积压')
    detail = sub.add_parser('show')
    detail.add_argument('task_id', type=UUID)
    retry = sub.add_parser('retry', help='核对后重试 dead/needs_review 任务；保留尝试历史')
    retry.add_argument('task_id', type=UUID)
    retry.add_argument('--note', required=True)
    args = parser.parse_args(argv)
    engine = None
    try:
        settings = Settings()
        if args.command == 'schedule':
            from app.task_jobs import enqueue_tick
            result = enqueue_tick(settings, [args.source] if args.source else tuple(SUPPORTED), args.limit, apply=args.apply,ecb_index=args.ecb_index)
        elif args.command == 'worker':
            if any(lane in ('media', 'analysis') for lane in (args.lane or [])):
                raise ValueError('Media/AI workers are not implemented yet')
            result = work(settings, args.lane or ('raw',), args.max_tasks)
        elif args.command == 'enqueue-history':
            if args.plan.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('History plan too large')
            plan = json.loads(args.plan.read_text(encoding='utf-8'))
            engine = engine_for(settings) if args.apply else create_read_engine(settings)
            with engine.begin() as connection:
                result = enqueue_history(connection, plan, args.limit, apply=args.apply)
        else:
            engine = engine_for(settings) if args.command == 'retry' else create_read_engine(settings)
            with engine.begin() as connection:
                result = retry_reviewed(connection, args.task_id, args.note) if args.command == 'retry' else (
                    select_task(connection, args.task_id) if args.command == 'show' else {'items': select_stats(connection)})
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except KeyboardInterrupt:
        print('工作进程已停止；已提交结果保留，未完成任务按租约和来源运行记录恢复。')
        return 130
    except (SQLAlchemyError, ValueError, OSError):
        print('队列操作未完成，请检查参数、迁移及任务/来源状态；未显示连接凭证。')
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
