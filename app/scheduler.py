"""Explicit single-round or foreground scheduling. Importing starts no work."""
import argparse
import json
import time
from datetime import datetime, timezone
from app.collection_failures import recovery_policy

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.collection_execution import SourceLease, execute_under_lease
from app.collection_rules import SUPPORTED, evaluate_rule
from app.core.config import Settings
from app.preview_batch import batch_limit, now_text
from app.repositories.collection_rules import select_context


def runner_for(source):
    if source == 'ecb':
        from app.import_batch import run_import
    else:
        from app.import_trump_batch import run_import
    return run_import


def run_source(settings, source, limit=3, *, client_factory=None, checkpoint=None):
    result = {'source': source, 'status': 'stopped', 'network_requested': False}
    report = None
    try:
        if checkpoint is not None:
            checkpoint()
        with SourceLease(settings, source) as lease:
            if not lease.acquired:
                return {**result, 'status': 'skipped_busy', 'reason': '同一来源已有批量任务。'}
            conn = lease.connection
            with conn.begin():
                lease.require()
                decision = evaluate_rule(source, select_context(conn, source))
                if not decision.eligible:
                    return {**result, 'status': decision.status, 'reasons': [r.model_dump() for r in decision.reasons]}
                sid = SUPPORTED[source][0]
                unfinished = conn.execute(text("SELECT run_id FROM collection_runs WHERE source_id=:sid AND status='running' LIMIT 1"), {'sid': sid}).scalar_one_or_none()
                if unfinished:
                    return {**result, 'status': 'blocked_unfinished_run', 'run_id': str(unfinished),
                            'reason': '发现未记录结束的运行，先核对其结果；不自动认定为完成或再次采集。'}
                state = conn.execute(text('SELECT * FROM collection_schedule WHERE source_id=:sid'), {'sid': sid}).mappings().one_or_none()
                if state and state['pause_reason']:
                    return {**result, 'status': 'paused', 'reason': state['pause_reason']}
                if state and state['retry_not_before'] and state['retry_not_before'] > datetime.now(timezone.utc):
                    return {**result, 'status': 'backoff', 'retry_not_before': state['retry_not_before'].isoformat(), 'reason': '等待退避或 Retry-After 指定时间。'}
                previous_failures = state['failure_count'] if state else 0
                # Current interval applies to the previous attempt/finish, including
                # across process restarts. A failed attempt still waits an interval.
                due = conn.execute(text(
                    'SELECT clock_timestamp() >= GREATEST(last_attempt_at,COALESCE(last_finished_at,last_attempt_at)) '
                    '+ make_interval(secs => :seconds) FROM collection_schedule WHERE source_id=:sid'
                ), {'sid': sid, 'seconds': decision.poll_interval_seconds}).scalar_one_or_none()
                if due is False:
                    return {**result, 'status': 'not_due', 'reason': '尚未达到该来源的读取间隔。'}
                conn.execute(text(
                    'INSERT INTO collection_schedule(source_id,source,last_attempt_at) VALUES (:sid,:source,clock_timestamp()) '
                    'ON CONFLICT(source_id) DO UPDATE SET last_attempt_at=EXCLUDED.last_attempt_at,last_finished_at=NULL,last_run_id=NULL'
                ), {'sid': sid, 'source': source})
            factory = client_factory or (lambda: httpx.Client(
                timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
                headers={'User-Agent': 'PrismQuant-FYP/0.1 (scheduled bounded collection)'}))
            with factory() as client:
                kwargs = {'checkpoint': checkpoint} if checkpoint is not None else {}
                report = execute_under_lease(lease, limit, client, runner_for(source), automatic=True, **kwargs)
            # Stay on the lease session for the scheduling update; loss means stop.
            with conn.begin():
                lease.require()
                failures, retry_at, pause_reason = recovery_policy(report, previous_failures, decision.poll_interval_seconds, datetime.now(timezone.utc))
                conn.execute(text(
                    'UPDATE collection_schedule SET failure_count=:failures,retry_not_before=:retry,pause_reason=:pause, last_finished_at=clock_timestamp(),last_run_id=CAST(:run AS uuid) '
                    'WHERE source_id=:sid'
                ), {'sid': sid, 'run': report.get('run_id'), 'failures': failures, 'retry': retry_at, 'pause': pause_reason})
            return {**result, 'status': report['status'], 'run_id': report.get('run_id'),
                    'run_record_status': report.get('run_record_status'), 'summary': report.get('summary'),
                    'saved': report.get('saved'), 'network_requested': None,
                    'reason': report.get('stop_reason') or report.get('run_record_warning'),
                    **{key: report.get(key) for key in ('failure_kind', 'http_status', 'retry_not_before', 'retry_after_invalid')},
                    'write_outcome_unknown': bool(report.get('write_outcome_unknown') or any(
                        i.get('write_outcome_unknown') for i in report.get('items', [])))}
    except (SQLAlchemyError, ValueError):
        if report is not None:
            return {**result, 'run_id': report.get('run_id'), 'saved': report.get('saved'),
                    'summary': report.get('summary'), 'run_record_status': report.get('run_record_status'),
                    'network_requested': None,
                    'reason': '调度结束时间未能保存；已确认的内容提交和运行记录结果保留，请核对后续调度状态。'}
        return {**result, 'reason': '调度数据库或执行锁检查失败，停止该来源；已提交内容保留，结果需核对。', 'network_requested': None}


def tick(settings, sources, limit=3, *, client_factory=None):
    if type(limit) is not int or not 1 <= limit <= 5 or any(s not in SUPPORTED for s in sources):
        raise ValueError('Invalid scheduler parameters')
    if getattr(settings,'fast_discovery_enabled',False) is True:
        from app.monitor_speed import run_source as fast_source
        return {'mode':'fast_discovery_tick','items':[fast_source(settings,key) for key in ('ecb','trump','fed','eia','treasury')]}
    items = []
    for source in dict.fromkeys(sources):
        item = run_source(settings, source, limit, client_factory=client_factory)
        items.append(item)
        if item['status'] == 'interrupted':
            break
    return {'mode': 'scheduler_tick', 'checked_at': now_text(), 'limit': limit, 'items': items}


def interval_arg(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('检查间隔必须是 5–300 秒的整数') from None
    if not 5 <= result <= 300:
        raise argparse.ArgumentTypeError('检查间隔必须是 5–300 秒的整数')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='按现有开关执行调度；不会打开开关。--once 单轮，--loop 前台持续运行')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--loop', action='store_true')
    parser.add_argument('--source', choices=('ecb', 'trump'), help='默认顺序检查两个来源')
    parser.add_argument('--limit', type=batch_limit, default=3)
    parser.add_argument('--tick-seconds', type=interval_arg, default=10, help='仅控制本地规则检查频率，不替代来源读取间隔')
    parser.add_argument('--enqueue', action='store_true', help='仅把到期来源写入持久队列，不访问来源；由独立 worker 执行')
    parser.add_argument('--ecb-index',action='store_true',help='与 --enqueue 同用，为 ECB 投递索引发现任务')
    args = parser.parse_args(argv)
    if args.ecb_index and not args.enqueue:
        parser.error('--ecb-index 必须与 --enqueue 同用')
    try:
        settings = Settings()
        while True:
            if args.enqueue:
                from app.task_jobs import enqueue_tick
                report = enqueue_tick(settings, [args.source] if args.source else list(SUPPORTED), args.limit,ecb_index=args.ecb_index)
            else:
                report = tick(settings, [args.source] if args.source else list(SUPPORTED), args.limit)
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            if any(item['status'] == 'interrupted' for item in report['items']):
                return 130
            if args.once:
                if any(item['status'] in ('stopped', 'blocked_unfinished_run', 'paused') or item.get('run_record_status') in ('start_failed', 'finalization_failed') for item in report['items']):
                    return 1
                return 2 if any(item['status'] in ('invalid_configuration', 'completed_with_issues') for item in report['items']) else 0
            time.sleep(args.tick_seconds)
    except KeyboardInterrupt:
        print('调度器已停止；已提交内容保留，未开启其他后台任务。')
        return 130
    except ValueError:
        print('本地配置无效，调度器未启动。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
