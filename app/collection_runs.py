"""Record CLI batch executions independently of per-content transactions."""

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.core.database import database_url
from app.baseline import RunTiming, ItemTiming, clean_timing


def engine_for(settings):
    return create_engine(database_url(settings), poolclass=NullPool, hide_parameters=True,
                         connect_args={'connect_timeout': 5, 'options': '-c lock_timeout=5000 -c statement_timeout=10000'})


def start_run(connection, source, limit, mode='manual_batch_import'):
    if source not in ('ecb', 'trump') or type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError('Invalid run request')
    if mode not in ('manual_batch_import', 'automatic_batch_import', 'manual_history_import'):
        raise ValueError('Invalid run mode')
    from app.collection_rules import SUPPORTED
    SOURCE_ID, PERSON_ID, _, _ = SUPPORTED[source]
    run_id = uuid4()
    connection.execute(text(
        'INSERT INTO collection_runs(run_id,source,source_id,person_id,mode,batch_limit,started_at) '
        "VALUES (:id,:source,:sid,:pid,:mode,:limit,:started)"
    ), dict(id=run_id, source=source, sid=SOURCE_ID, pid=PERSON_ID, mode=mode, limit=limit,
            started=datetime.now(timezone.utc)))
    return run_id


def finish_run(connection, run_id, report, *, complete=True):
    """Only whitelist metadata; summaries and item rows commit as one transaction."""
    if report['status'] not in ('completed', 'completed_with_issues', 'no_candidates', 'no_new_candidates', 'stopped', 'interrupted'):
        raise ValueError('Invalid terminal status')
    summary = report.get('summary', {}) if complete else {}
    selection = report.get('selection', {})
    if complete:
        # Validate terminal counts before writing any history rows.
        from app.api.collection_runs import RunCounts
        counts = RunCounts(**summary)
        if selection.get('selected', 0) != counts.attempted + counts.not_attempted:
            raise ValueError('Selection and outcome counts disagree')
    parameters = dict(id=run_id, status=report['status'], complete=complete,
                      finished=datetime.now(timezone.utc), reason=report.get('stop_reason'),
                      summary=json.dumps(summary, allow_nan=False), selection=json.dumps(selection, allow_nan=False),
                      timing=json.dumps(clean_timing(report.get('timing'),RunTiming),allow_nan=False))
    for key in ('source_enabled', 'monitoring_enabled', 'link_enabled'):
        parameters[key] = report.get(key)
    updated = connection.execute(text(
        'UPDATE collection_runs SET status=:status,finished_at=:finished,stop_reason=:reason,report_complete=:complete, '
        'summary=CAST(:summary AS jsonb),selection=CAST(:selection AS jsonb),timing=CAST(:timing AS jsonb),source_enabled=:source_enabled, '
        'monitoring_enabled=:monitoring_enabled,link_enabled=:link_enabled '
        "WHERE run_id=:id AND status='running' RETURNING run_id"
    ), parameters).scalar_one_or_none()
    if updated is None:
        raise ValueError('Run missing or already finalized')
    for phase, items in (('precheck', report.get('precheck_results', [])), ('detail', report.get('items', []))):
        for position, item in enumerate(items):
            connection.execute(text(
                'INSERT INTO collection_run_items(run_id,phase,position,url,title,record_id,status,reason,write_outcome_unknown,timing) '
                'VALUES (:id,:phase,:position,:url,:title,:record,:status,:reason,:unknown,CAST(:timing AS jsonb))'
            ), dict(id=run_id, phase=phase, position=position, url=item.get('url'),
                    title=item.get('rss_title'), record=UUID(item['record_id']) if item.get('record_id') else None,
                    status=item['status'], reason=item.get('reason'), unknown=item.get('write_outcome_unknown', False),
                    timing=json.dumps(clean_timing(item.get('timing'),ItemTiming),allow_nan=False)))


def execute_recorded(settings, source, limit, client, runner, *, mode='manual_batch_import'):
    """Called by batch execution entry points, including the explicit scheduler."""
    engine = None
    try:
        engine = engine_for(settings)
        with engine.begin() as connection:
            run_id = start_run(connection, source, limit, mode)
    except Exception:
        if engine is not None:
            engine.dispose()
        return {'status': 'stopped', 'saved': False, 'run_record_status': 'start_failed',
                'stop_reason': '无法创建运行记录，未开始采集；请检查数据库及迁移版本。'}
    try:
        complete = True
        try:
            report = runner(settings, limit, client)
        except (Exception, KeyboardInterrupt) as exc:
            complete = False
            report = {'status': 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'stopped',
                      'saved': None, 'write_outcome_unknown': True,
                      'stop_reason': '运行意外结束，未取得完整汇总；已提交内容保留，当前写入结果需复核。'}
        try:
            with engine.begin() as connection:
                finish_run(connection, run_id, report, complete=complete)
            report['run_record_status'] = 'saved'
        except (Exception, KeyboardInterrupt):
            # Never retry the content runner to repair a logging failure.
            report['run_record_status'] = 'finalization_failed'
            report['run_record_warning'] = '运行结果未能确认写入历史；内容提交结果不变，请按记录 ID 或地址复核。'
        report['run_id'] = str(run_id)
        report['mode'] = mode
        return report
    finally:
        engine.dispose()
