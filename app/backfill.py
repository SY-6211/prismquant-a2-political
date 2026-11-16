"""Discover historical candidates, then import explicit bounded batches with checkpoints."""
import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import text

from app.collection_execution import SourceLease
from app.collection_runs import execute_recorded
from app.collection_failures import recovery_policy
from app.core.config import PROJECT_ROOT, Settings
from app.history_sources import discover_ecb, discover_trump, in_range
from app.scheduler_service import FileLock


def write_plan(path, plan):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def source_tools(source):
    if source == 'ecb':
        from app import batch_ecb as batch, import_batch as importer
    elif source == 'trump':
        from app import batch_trump as batch, import_trump_batch as importer
    else:
        raise ValueError('Unsupported history source')
    return batch, importer


def validate_plan(plan):
    if plan.get('version') != 1 or plan.get('source') not in ('ecb', 'trump'):
        raise ValueError('Unsupported history plan')
    since, until = date.fromisoformat(plan['since']), date.fromisoformat(plan['until'])
    if since > until or not isinstance(plan['candidates'], list) or len(plan['candidates']) > 10000:
        raise ValueError('Invalid date range or candidate count')
    expected = 'archive_listing' if plan['source'] == 'trump' else 'ecb_history_index'
    for item in plan['candidates']:
        if item.get('metadata_origin') != expected or not in_range(item.get('published_at'), since, until):
            raise ValueError('History candidate lies outside the declared scope')
    return plan


def discover(settings, client, source, since, until, max_pages, previous=None):
    batch, _ = source_tools(source)
    batch.load_context(settings)
    result = (discover_ecb if source == 'ecb' else discover_trump)(client, since, until, max_pages,
        **({'start_url': previous['next_url']} if source == 'trump' and previous and previous.get('next_url') else {}))
    old = previous['candidates'] if previous else []
    merged = {item['url']: item for item in old}
    for item in result['candidates']:
        if item['url'] in merged and merged[item['url']] != item:
            raise ValueError('History metadata changed between discovery pages')
        merged[item['url']] = item
    return validate_plan(dict(version=1, source=source, since=since.isoformat(), until=until.isoformat(),
        discovered_at=datetime.now(timezone.utc).isoformat(), candidates=list(merged.values()),
        coverage_complete=result['coverage_complete'], next_url=result['next_url'],
        pages=(previous.get('pages', 0) if previous and source == 'trump' else 0) + result['pages'],
        outcomes=previous.get('outcomes', {}) if previous else {}, runs=previous.get('runs', []) if previous else []))


def summarize(settings, plan):
    batch, _ = source_tools(plan['source'])
    prepared = batch.prepare_candidates(plan['candidates'])
    existing = batch.read_existing(settings, [item['url'] for item in prepared['candidates']])
    complete = batch.existing_complete
    saved = sum(complete(row) for row in existing.values())
    pending = [item for item in prepared['candidates'] if item['url'] not in existing and item['url'] not in plan['outcomes']]
    return dict(source=plan['source'], since=plan['since'], until=plan['until'],
        coverage_complete=plan['coverage_complete'], pages=plan['pages'], discovered=len(plan['candidates']),
        eligible_candidates=len(prepared['candidates']), skipped_existing=saved,
        existing_needs_review=len(existing)-saved, precheck_needs_review=prepared['selection']['precheck_needs_review'],
        out_of_scope=prepared['selection']['out_of_scope'], processed=len(plan['outcomes']), pending=len(pending)), pending


def import_plan(settings, client, plan, path, max_items, *, notify=print):
    source = plan['source']
    batch, importer = source_tools(source)
    with SourceLease(settings, source) as lease:
        if not lease.acquired:
            return dict(status='skipped_busy', saved=False)
        if plan.get('runs') and (plan['runs'][-1].get('status') in ('stopped', 'interrupted') or plan['runs'][-1].get('run_record_status') != 'saved'):
            return dict(status='blocked_plan_needs_review', saved=False,
                        warning='上次回补异常停止，先核对运行记录与写入结果，不能自动跳过失败后继续。')
        with lease.connection.begin():
            if lease.connection.execute(text("SELECT count(*) FROM collection_runs WHERE source_id=:sid AND status='running'"), {'sid': batch.SOURCE_ID}).scalar_one():
                return dict(status='blocked_unfinished_run', saved=False)
            schedule = lease.connection.execute(text(
                'SELECT pause_reason, retry_not_before, now() AS checked_at FROM collection_schedule WHERE source_id=:sid'
            ), {'sid': batch.SOURCE_ID}).mappings().one_or_none()
            if schedule and (schedule['pause_reason'] or (schedule['retry_not_before'] and schedule['retry_not_before'] > schedule['checked_at'])):
                return dict(status='blocked_source_pause_or_backoff', saved=False)
        summary, pending = summarize(settings, plan)
        selected = pending[:max_items]
        totals = dict(inserted=0, already_exists=0, needs_review=0, skipped_unsupported=0, failed=0)
        for start in range(0, len(selected), 5):
            entries = selected[start:start+5]
            def runner(config, limit, http):
                return importer.run_import(config, limit, http, feed_items=entries,
                    before_request=lambda: lease.before_request(False),
                    save=lambda config, *payload: lease.save(False, config, *payload))
            report = execute_recorded(settings, source, len(entries), client, runner, mode='manual_history_import')
            # Persist confirmed outcomes only. A crash/unknown commit is not retried blindly.
            for item in report.get('items', []):
                if item['status'] != 'not_attempted':
                    plan['outcomes'][item['url']] = {key: item.get(key) for key in ('status','reason','record_id','write_outcome_unknown')}
            plan['runs'].append({key: report.get(key) for key in ('run_id','status','summary','run_record_status')})
            write_plan(path, plan)
            for key in totals:
                totals[key] += report.get('summary', {}).get(key, 0)
            notify(json.dumps(dict(run_id=report.get('run_id'), status=report.get('status'), summary=report.get('summary')), ensure_ascii=False))
            if report.get('status') in ('stopped','interrupted') or report.get('run_record_status') != 'saved':
                with lease.connection.begin():
                    lease.require()
                    current = lease.connection.execute(text('SELECT failure_count FROM collection_schedule WHERE source_id=:sid'), {'sid':batch.SOURCE_ID}).scalar_one_or_none() or 0
                    failures, retry_at, pause_reason = recovery_policy(report, current, 300, datetime.now(timezone.utc))
                    lease.connection.execute(text(
                        'INSERT INTO collection_schedule(source_id,source,last_attempt_at,last_finished_at,last_run_id,failure_count,retry_not_before,pause_reason) '
                        'VALUES (:sid,:source,clock_timestamp(),clock_timestamp(),CAST(:run AS uuid),:failures,:retry,:pause) '
                        'ON CONFLICT(source_id) DO UPDATE SET failure_count=:failures,retry_not_before=:retry,pause_reason=:pause'
                    ), {'sid':batch.SOURCE_ID,'source':source,'run':report.get('run_id'),'failures':failures,'retry':retry_at,'pause':pause_reason})
                return dict(status='stopped', totals=totals, warning='先核对本次运行；已提交内容保留。')
        summary, _ = summarize(settings, plan)
        issues = summary['existing_needs_review'] + summary['precheck_needs_review'] + sum(
            value.get('status') in ('needs_review', 'failed', 'interrupted') for value in plan['outcomes'].values())
        status = ('completed_with_issues' if issues else 'completed') if not summary['pending'] and summary['coverage_complete'] else 'partial'
        return dict(status=status, totals=totals, **summary)


def main():
    parser = argparse.ArgumentParser(description='历史回补：默认仅发现候选；--commit 才保存。日期为 UTC 日历日期，包含首尾。')
    parser.add_argument('--source', choices=('ecb','trump'), required=True)
    parser.add_argument('--since', type=date.fromisoformat)
    parser.add_argument('--until', type=date.fromisoformat)
    parser.add_argument('--plan', type=Path, help='保存并续用的本地候选和进度文件')
    parser.add_argument('--max-pages', type=int, default=5)
    parser.add_argument('--max-items', type=int, default=20)
    parser.add_argument('--continue-index', action='store_true', help='继续发现未扫描的历史列表')
    parser.add_argument('--commit', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 100 or not 1 <= args.max_items <= 500:
        parser.error('max-pages 必须为 1–100；max-items 必须为 1–500')
    settings = Settings()
    today = datetime.now(timezone.utc).date()
    since, until = args.since or today-timedelta(days=29), args.until or today
    path = args.plan or PROJECT_ROOT / '.runtime' / 'history' / f'{args.source}-{since}-{until}.json'
    if path.suffix != '.json':
        parser.error('plan 必须使用 .json 文件')
    try:
        with FileLock(path.with_suffix('.lock')) as plan_lock, httpx.Client(timeout=httpx.Timeout(25, connect=10), follow_redirects=False,
                          headers={'User-Agent':'PrismQuant-FYP/0.1 (bounded manual history import)'}) as client:
            if plan_lock.file is None:
                print('同一历史计划已有任务执行，本次跳过。')
                return 2
            previous = None
            if path.exists():
                if path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError('History plan too large')
                previous = validate_plan(json.loads(path.read_text(encoding='utf-8')))
                if previous['source'] != args.source or (args.since and previous['since'] != since.isoformat()) or (args.until and previous['until'] != until.isoformat()):
                    raise ValueError('History plan belongs to a different scope')
                since, until = date.fromisoformat(previous['since']), date.fromisoformat(previous['until'])
            if since > until or (until-since).days > 366:
                raise ValueError('Date range must be at most 367 days')
            plan = previous
            if plan is None or (args.continue_index and not plan['coverage_complete']):
                plan = discover(settings, client, args.source, since, until, args.max_pages, previous)
                write_plan(path, plan)
            summary, _ = summarize(settings, plan)
            print(json.dumps(dict(mode='history_plan', plan=str(path.resolve()), content_saved=False, **summary), ensure_ascii=False, indent=2), flush=True)
            if args.commit:
                result = import_plan(settings, client, plan, path, args.max_items, notify=lambda value: print(value, flush=True))
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                return 0 if result['status'] in ('completed', 'completed_with_issues') else 2
            return 0
    except KeyboardInterrupt:
        print('历史回补已中断；已提交内容保留，请核对运行记录和进度文件。')
        return 130
    except Exception:
        print('历史回补停止：索引、网络、数据库或进度文件需检查；未显示连接凭证。已提交内容保留。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
