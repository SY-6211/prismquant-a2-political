"""Inspect recovery state, or explicitly acknowledge an unfinished run/resume a source."""
import argparse
import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.collection_execution import SourceLease
from app.collection_rules import SUPPORTED
from app.core.config import Settings
from app.core.database import create_read_engine


def inspect_state(settings, source):
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            sid = SUPPORTED[source][0]
            state = connection.execute(text('SELECT * FROM collection_schedule WHERE source_id=:sid'), {'sid': sid}).mappings().one_or_none()
            unfinished = connection.execute(text("SELECT run_id,started_at FROM collection_runs WHERE source_id=:sid AND status='running' ORDER BY started_at"), {'sid': sid}).mappings()
            return {'source': source, 'schedule': dict(state) if state else None,
                    'unfinished_runs': [dict(r) for r in unfinished], 'changes_saved': False}
    finally:
        engine.dispose()


def recover(settings, source, *, run_id=None, resume=False, note):
    if not note.strip() or len(note) > 500:
        raise ValueError('A review note of 1–500 characters is required')
    with SourceLease(settings, source) as lease:
        if not lease.acquired:
            return {'status': 'skipped_busy', 'changes_saved': False}
        with lease.connection.begin():
            lease.require()
            sid = SUPPORTED[source][0]
            if run_id is not None:
                row = lease.connection.execute(text(
                    "UPDATE collection_runs SET status='stopped',finished_at=GREATEST(clock_timestamp(),started_at),report_complete=false, "
                    "recovered_at=clock_timestamp(),recovery_note=:note,stop_reason='人工核对后关闭未结束记录；不推断已提交内容数量。' "
                    "WHERE run_id=:id AND source_id=:sid AND status='running' RETURNING run_id"
                ), {'id': run_id, 'sid': sid, 'note': note}).scalar_one_or_none()
                if row is None:
                    raise ValueError('Run not unfinished or source mismatch')
            if resume:
                unfinished = lease.connection.execute(text("SELECT 1 FROM collection_runs WHERE source_id=:sid AND status='running' LIMIT 1"), {'sid': sid}).first()
                if unfinished:
                    raise ValueError('Unfinished runs must be reviewed first')
                # Preserve Retry-After and existing timestamps; resume never bypasses a deadline.
                lease.connection.execute(text('UPDATE collection_schedule SET pause_reason=NULL,failure_count=0 WHERE source_id=:sid'), {'sid': sid})
            return {'status': 'reviewed', 'source': source, 'changes_saved': True,
                    'run_id': str(run_id) if run_id else None, 'resumed': resume,
                    'note': note, 'automatic_started': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='默认只读查看恢复状态；修改需明确动作和核对说明，不开启监控')
    parser.add_argument('--source', required=True, choices=('ecb', 'trump'))
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--resolve-run', type=UUID)
    actions.add_argument('--resume', action='store_true')
    parser.add_argument('--note')
    args = parser.parse_args(argv)
    if (args.resolve_run or args.resume) and (not args.note or not args.note.strip() or len(args.note)>500):
        parser.error('恢复动作必须提供 1–500 字符 --note 核对说明')
    try:
        settings = Settings()
        result = recover(settings, args.source, run_id=args.resolve_run, resume=args.resume, note=args.note) if args.resolve_run or args.resume else inspect_state(settings, args.source)
    except (SQLAlchemyError, ValueError):
        print('恢复检查失败：请核对来源、运行状态和数据库；未显示凭证。')
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 2 if result.get('status') == 'skipped_busy' else 0


if __name__ == '__main__':
    raise SystemExit(main())
