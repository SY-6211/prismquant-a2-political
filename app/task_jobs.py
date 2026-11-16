"""Concrete source/history handlers; future media and AI workers remain unregistered."""
from dataclasses import replace
from datetime import datetime, timezone
import math

import httpx
from sqlalchemy import text

from app.collection_execution import SourceLease, execute_under_lease
from app.collection_failures import recovery_policy
from app.collection_rules import SUPPORTED, evaluate_rule
from app.collection_runs import engine_for
from app.repositories.collection_rules import select_context
from app.repositories.tasks import enqueue
from app.task_types import normalized_payload, outcome_for_report


def enqueue_tick(settings, sources, limit=3, *, apply=True, ecb_index=False):
    if any(s not in SUPPORTED for s in sources) or type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError('Invalid source scheduling request')
    engine = engine_for(settings)
    try:
        items = []
        with engine.begin() as connection:
            for source in dict.fromkeys(sources):
                decision = evaluate_rule(source, select_context(connection, source))
                if not decision.eligible:
                    items.append({'source': source, 'status': decision.status})
                    continue
                sid = SUPPORTED[source][0]
                if source=='ecb' and ecb_index:
                    from app.repositories.ecb_discovery import state as discovery_state
                    current = discovery_state(connection)
                    status = ('paused' if current and current['pause_reason'] else
                              'backoff' if current and current['retry_not_before'] and current['retry_not_before']>current['checked_at'] else
                              'not_due' if current and current['last_scan_at'] and (current['checked_at']-current['last_scan_at']).total_seconds()<60 else None)
                    if status:
                        items.append({'source':source,'status':status})
                        continue
                unfinished = connection.execute(text("SELECT count(*) FROM collection_runs WHERE source_id=:id AND status='running'"),
                                                {'id': sid}).scalar_one()
                state = connection.execute(text('SELECT *,clock_timestamp() AS checked_at FROM collection_schedule WHERE source_id=:id'),
                                           {'id': sid}).mappings().one_or_none()
                if unfinished:
                    items.append({'source': source, 'status': 'blocked_unfinished_run'})
                    continue
                if state and state['pause_reason']:
                    items.append({'source': source, 'status': 'paused'})
                    continue
                if state and state['retry_not_before'] and state['retry_not_before'] > state['checked_at']:
                    items.append({'source': source, 'status': 'backoff'})
                    continue
                if state and not (source=='ecb' and ecb_index):
                    previous = max(v for v in (state['last_attempt_at'], state['last_finished_at']) if v is not None)
                    if (state['checked_at'] - previous).total_seconds() < decision.poll_interval_seconds:
                        items.append({'source': source, 'status': 'not_due'})
                        continue
                stamp = connection.execute(text('SELECT extract(epoch FROM clock_timestamp())')).scalar_one()
                interval = 60 if source=='ecb' and ecb_index else decision.poll_interval_seconds
                request_id = f'schedule:{source}:{math.floor(stamp / interval)}'
                payload = {'source':source,'limit':limit}
                if source=='ecb' and ecb_index: payload['discovery']='ecb_index'
                item = enqueue(connection, 'collect_source', payload, request_id) if apply else {'status': 'would_queue'}
                items.append({'source': source, **item})
        return {'mode': 'scheduler_queue_tick', 'items': items, 'applied': apply, 'network_requested': False}
    finally:
        engine.dispose()


def run_history_item(settings, payload, checkpoint, *, client_factory=None):
    source = payload['source']
    sid = SUPPORTED[source][0]
    with SourceLease(settings, source) as lease:
        if not lease.acquired:
            return {'status': 'skipped_busy'}
        checkpoint()
        with lease.connection.begin():
            lease.require()
            decision = evaluate_rule(source, select_context(lease.connection, source))
            if decision.status == 'invalid_configuration':
                return {'status': 'invalid_configuration'}
            if lease.connection.execute(text("SELECT count(*) FROM collection_runs WHERE source_id=:id AND status='running'"),
                                        {'id': sid}).scalar_one():
                return {'status': 'blocked_unfinished_run'}
            state = lease.connection.execute(text('SELECT *,clock_timestamp() AS checked_at FROM collection_schedule WHERE source_id=:id'),
                                             {'id': sid}).mappings().one_or_none()
            if state and state['pause_reason']:
                return {'status': 'paused'}
            if state and state['retry_not_before'] and state['retry_not_before'] > state['checked_at']:
                return {'status': 'backoff', 'retry_not_before': state['retry_not_before']}
        from app.backfill import source_tools
        _, importer = source_tools(source)
        def runner(config, limit, client, **kwargs):
            return importer.run_import(config, limit, client, feed_items=[payload['entry']], **kwargs)
        factory = client_factory or (lambda: httpx.Client(timeout=httpx.Timeout(25, connect=10), follow_redirects=False,
            headers={'User-Agent': 'PrismQuant-FYP/0.1 (queued single history item)'}))
        with factory() as client:
            report = execute_under_lease(lease, 1, client, runner, automatic=False, checkpoint=checkpoint, mode='manual_history_import')
        if report.get('status') in ('stopped', 'interrupted') or report.get('run_record_status') != 'saved':
            with lease.connection.begin():
                lease.require()
                failures, retry_at, pause_reason = recovery_policy(report, state['failure_count'] if state else 0,
                    decision.poll_interval_seconds, datetime.now(timezone.utc))
                lease.connection.execute(text('INSERT INTO collection_schedule '
                    '(source_id,source,last_attempt_at,last_finished_at,last_run_id,failure_count,retry_not_before,pause_reason) '
                    'VALUES (:id,:source,clock_timestamp(),clock_timestamp(),CAST(:run AS uuid),:failures,:retry,:pause) '
                    'ON CONFLICT(source_id) DO UPDATE SET failure_count=:failures,retry_not_before=:retry,pause_reason=:pause'),
                    {'id': sid, 'source': source, 'run': report.get('run_id'), 'failures': failures, 'retry': retry_at, 'pause': pause_reason})
        return report


def execute_task(settings, lease):
    kind, task = lease.task['kind'], lease.task
    payload, _, _ = normalized_payload(kind, task['payload'])
    if task['source_id'] != SUPPORTED[payload['source']][0]:
        raise ValueError('Task source identity mismatch')
    lease.checkpoint()
    if kind == 'collect_source':
        if payload.get('discovery')=='ecb_index':
            from app.ecb_fast import run_ecb_fast
            report = run_ecb_fast(settings,payload['limit'],commit=True,checkpoint=lease.checkpoint)
        else:
            from app.scheduler import run_source
            report = run_source(settings, payload['source'], payload['limit'], checkpoint=lease.checkpoint)
    elif kind == 'history_item':
        report = run_history_item(settings, payload, lease.checkpoint)
    else:
        raise ValueError('Task handler is not implemented')
    outcome = outcome_for_report(report)
    return replace(outcome, result={k: report.get(k) for k in ('run_id', 'summary')}, stop_worker=report.get('status') == 'interrupted')
