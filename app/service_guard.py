"""Windows session guardian: restart a dead collector, never kill a live one."""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import Settings
from app.scheduler_service import (RUNTIME as WORKER_RUNTIME, FileLock, read_json,
                                   write_json, status as worker_status, start as start_worker)
from app.backup_database import create_backup, STATE as BACKUP_STATE

RUNTIME = WORKER_RUNTIME.parent / 'guard'


def status():
    state = read_json(RUNTIME/'state.json')
    with FileLock(RUNTIME/'guard.lock') as lock:
        active = lock.file is None
    desired = read_json(RUNTIME/'desired.json').get('enabled',False)
    try:
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(state['heartbeat_at'])).total_seconds()
    except (ValueError,KeyError,TypeError): age=999999
    return {**state,'active':active,'enabled':desired,
            'status':'running' if active and age<=400 else 'busy_or_stale' if active else 'stopped'}


def cycle(settings, state, *, clock=time.time):
    current=worker_status()
    if current['active']:
        state.update(collector_status=current['status'], restart_attempts=0, next_restart_at=0)
    elif clock() >= state.get('next_restart_at',0):
        result=start_worker(settings,allow_enabled=True,guarded=True)
        count=state.get('restart_attempts',0)+1
        state.update(collector_status=result['status'],restart_attempts=count,
                     next_restart_at=clock()+min(300,10*2**min(count-1,5)))
        if result.get('active'):
            state['restart_count']=state.get('restart_count',0)+1
    backup=read_json(BACKUP_STATE)
    try: age=clock()-datetime.fromisoformat(backup['finished_at']).timestamp()
    except (KeyError,ValueError,TypeError): age=999999
    if (backup.get('status')!='ok' or age>=86400) and clock() >= state.get('next_backup_at',0):
        result=create_backup(settings)
        state['backup_status']=result.get('status')
        state['next_backup_at']=clock()+(86400 if result.get('status')=='ok' else 3600)
    return state


def run(token):
    with FileLock(RUNTIME/'guard.lock') as lock:
        if lock.file is None:return 2
        state={'guard_id':token,'pid':os.getpid(),'restart_count':0,'started_at':datetime.now(timezone.utc).isoformat()}
        while read_json(RUNTIME/'desired.json').get('enabled'):
            state['heartbeat_at']=datetime.now(timezone.utc).isoformat()
            write_json(RUNTIME/'state.json',state)
            try: cycle(Settings(),state)
            except Exception:state['collector_status']='check_failed'
            state['heartbeat_at']=datetime.now(timezone.utc).isoformat()
            write_json(RUNTIME/'state.json',state)
            for _ in range(10):
                if not read_json(RUNTIME/'desired.json').get('enabled'):break
                time.sleep(1)
        return 0


def main():
    parser=argparse.ArgumentParser(description='本机采集进程守护与每日备份；不安装开机任务')
    parser.add_argument('action',choices=('start','status','stop','run'))
    parser.add_argument('--token')
    args=parser.parse_args()
    try:
        if args.action=='run':return run(args.token or str(uuid4()))
        if args.action=='start':
            with FileLock(RUNTIME/'control.lock') as control:
                if control.file is None:return 2
                write_json(RUNTIME/'desired.json',{'enabled':True})
                if not status()['active']:
                    from app.scheduler_service import launch_module
                    launch_module('app.service_guard',['run','--token',str(uuid4())])
                    for _ in range(50):
                        if status()['active']:break
                        time.sleep(.1)
        elif args.action=='stop':
            write_json(RUNTIME/'desired.json',{'enabled':False})
            from app.scheduler_service import stop
            stop()
        print(json.dumps(status(),ensure_ascii=False,indent=2))
        return 0
    except Exception:
        print('守护进程管理失败，请检查本机环境；未显示凭证。')
        return 1


if __name__=='__main__':raise SystemExit(main())
