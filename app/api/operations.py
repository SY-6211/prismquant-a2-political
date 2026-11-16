from datetime import datetime, timezone
from fastapi import APIRouter, Request
from sqlalchemy import text

from app.core.database import create_read_engine
from app.scheduler_service import status as collector_status, read_json
from app.service_guard import status as guard_status
from app.backup_database import STATE as BACKUP_STATE

router=APIRouter(prefix='/api/v1',tags=['运行状态'])


@router.get('/system-status',summary='获取本机采集状态和最近备份结果')
def system_status(request:Request):
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'database':{'status':'unavailable'},
            'collector':{'status':'unknown'},'guardian':{'status':'unknown'},'backup':{'status':'never'}}
    try:
        engine=create_read_engine(request.app.state.settings)
        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
                schedules=conn.execute(text('SELECT source,pause_reason,retry_not_before,last_finished_at FROM collection_schedule ORDER BY source')).mappings().all()
                unfinished=conn.execute(text("SELECT count(*) FROM collection_runs WHERE status='running'")).scalar_one()
            result['database']={'status':'ok','unfinished_runs':unfinished,
                'sources':[dict(row) for row in schedules]}
        finally:engine.dispose()
    except Exception:pass
    for name, reader, keys in (
        ('collector',collector_status,('status','active','heartbeat_at','heartbeat_age_seconds','phase','cycle_count','last_results')),
        ('guardian',guard_status,('status','active','enabled','restart_count','collector_status','heartbeat_at')),
        ('backup',lambda:read_json(BACKUP_STATE),('status','finished_at','bytes','archive_verified','restore_tested')),
    ):
        try:
            value=reader()
            if value:result[name]={key:value[key] for key in keys if key in value}
        except Exception:result[name]={'status':'unknown'}
    return result
