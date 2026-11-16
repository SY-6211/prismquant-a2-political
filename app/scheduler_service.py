"""Local detached worker with cooperative stop, ownership locks and rotating logs."""
import argparse
import base64
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import PROJECT_ROOT, Settings
from app.core.database import create_read_engine
from app.collection_rules import check_rules
from app.scheduler import tick

RUNTIME = PROJECT_ROOT / '.runtime' / 'scheduler'


def now():
    return datetime.now(timezone.utc).isoformat()


class FileLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        if not self.path.stat().st_size:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            self.file = handle
        except OSError:
            handle.close()
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close()  # OS releases the lock even if the process exits.


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def status(runtime=RUNTIME):
    state = read_json(runtime / 'state.json')
    with FileLock(runtime / 'worker.lock') as lock:
        active = lock.file is None
    if not active:
        return {**state, 'status': 'stopped', 'active': False}
    if not state or state.get('status') == 'stopped':
        return {'status': 'starting', 'active': True}
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state['heartbeat_at'])).total_seconds()
    except (ValueError, KeyError, TypeError):
        age = 99999
    return {**state, 'active': True, 'status': 'running' if age <= 90 else 'busy_or_stale',
            'heartbeat_age_seconds': max(0, round(age, 1))}


def preflight(settings):
    rules = check_rules(settings)
    engine = create_read_engine(settings)
    try:
        with engine.connect() as connection:
            revision = connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
            unfinished = connection.execute(text("SELECT count(*) FROM collection_runs WHERE status='running'")).scalar_one()
            pauses = connection.execute(text('SELECT count(*) FROM collection_schedule WHERE pause_reason IS NOT NULL')).scalar_one()
    finally:
        engine.dispose()
    expected = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / 'alembic.ini'))).get_current_head()
    issues = []
    if revision != expected:
        issues.append('数据库迁移版本未到当前版本。')
    if any(item.status == 'invalid_configuration' for item in rules.items):
        issues.append('来源配置需核对。')
    if unfinished:
        issues.append('存在未结束运行，请先核对。')
    if pauses:
        issues.append('存在暂停来源，请先核对。')
    return {'status': 'ready' if not issues else 'needs_review', 'migration': revision,
            'eligible_count': rules.eligible_count, 'all_switches_off': all(
                item.source_enabled is False and item.monitoring_enabled is False and item.link_enabled is False for item in rules.items),
            'unfinished_runs': unfinished, 'paused_sources': pauses, 'issues': issues,
            'network_requested': False, 'changes_saved': False}


def worker(token, runtime=RUNTIME, *, interval=10, sleep=time.sleep, run_tick=tick):
    runtime.mkdir(parents=True, exist_ok=True)
    with FileLock(runtime / 'worker.lock') as ownership:
        if ownership.file is None:
            return 2
        logger = logging.getLogger('pq.scheduler.' + token)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(runtime / 'worker.log', maxBytes=2*1024*1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
        state = {'worker_id': token, 'pid': os.getpid(), 'started_at': now(), 'heartbeat_at': now(),
                 'status': 'running', 'phase': 'starting', 'cycle_count': 0, 'last_results': []}
        code = 0
        dispatcher = None
        try:
            settings = Settings()
            if run_tick is tick and settings.fast_discovery_enabled:
                from app.monitor_speed import Dispatcher
                def checkpoint():
                    if read_json(runtime/'stop.json').get('worker_id')==token:raise ValueError('worker_stop_requested')
                dispatcher=Dispatcher(settings,checkpoint)
            while read_json(runtime / 'stop.json').get('worker_id') != token:
                state.update(heartbeat_at=now(), phase='checking')
                write_json(runtime / 'state.json', state)
                result = dispatcher.tick() if dispatcher else run_tick(settings, ['ecb', 'trump'])
                state.update(cycle_count=state['cycle_count']+1, heartbeat_at=now(), phase='waiting',
                             last_results=[{key: item.get(key) for key in ('source','status','run_id')} for item in result['items']])
                write_json(runtime / 'state.json', state)
                logger.info(json.dumps(state['last_results'], ensure_ascii=False))
                if any(item['status'] == 'interrupted' for item in result['items']):
                    break
                for _ in range(interval):
                    if read_json(runtime / 'stop.json').get('worker_id') == token:
                        break
                    sleep(1)
                    state['heartbeat_at'] = now()
                    write_json(runtime / 'state.json', state)
        except (Exception, KeyboardInterrupt):
            logger.error('Worker stopped unexpectedly; inspect database run history. Exception details omitted.')
            code = 1
        finally:
            if dispatcher:dispatcher.close()
            state.update(status='stopped', phase='stopped', heartbeat_at=now(), exit_code=code)
            write_json(runtime / 'state.json', state)
            logger.removeHandler(handler)
            handler.close()
        return code


def launch_worker(token):
    return launch_module('app.scheduler_service', ['worker', '--token', token])


def launch_module(module, arguments):
    # WMI creates the process outside the launching terminal's Windows Job Object.
    # ShowWindow=0 keeps it hidden; no service or recurring task is registered.
    command = subprocess.list2cmdline([sys.executable, '-X', 'utf8', '-m', module, *arguments])
    def quote(value):
        return "'" + value.replace("'", "''") + "'"
    script = (
        "$ErrorActionPreference='Stop'\n"
        "$startupInfo=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}\n"
        "$created=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=" + quote(command) +
        ";CurrentDirectory=" + quote(str(PROJECT_ROOT)) + ";ProcessStartupInformation=$startupInfo}\n"
        "if ($created.ReturnValue -ne 0) { exit 1 }\n"
    )
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
    if result.returncode:
        raise RuntimeError('Windows background launch failed')


def start(settings, runtime=RUNTIME, *, allow_enabled=False, guarded=False):
    with FileLock(runtime / 'control.lock') as control:
        if control.file is None:
            return {'status': 'control_busy'}
        if guarded and not read_json(RUNTIME.parent / 'guard' / 'desired.json').get('enabled'):
            return {'status': 'guard_disabled'}
        current = status(runtime)
        if current['active']:
            return current
        check = preflight(settings)
        if check['status'] != 'ready' or (check['eligible_count'] and not allow_enabled):
            return {'status': 'start_blocked', 'preflight': check,
                    'reason': '请先核对；已有允许采集的来源时需显式 --allow-enabled 才能启动。'}
        token = str(uuid4())
        launch_worker(token)
        for _ in range(50):
            current = status(runtime)
            if current.get('worker_id') == token and current['active']:
                return current
            time.sleep(0.1)
        return {'status': 'startup_unconfirmed', 'worker_id': token, 'reason': '请运行 status 查看启动结果，不重复强制启动。'}


def stop(runtime=RUNTIME):
    if runtime == RUNTIME:
        desired = RUNTIME.parent / 'guard' / 'desired.json'
        if desired.exists():
            write_json(desired, {'enabled': False})
    with FileLock(runtime / 'control.lock') as control:
        if control.file is None:
            return {'status': 'control_busy'}
        current = status(runtime)
        if not current['active']:
            return current
        token = current.get('worker_id')
        if not token:
            return {'status': 'starting', 'reason': '稍后再查询或停止。'}
        write_json(runtime / 'stop.json', {'worker_id': token, 'requested_at': now()})
        for _ in range(50):
            current = status(runtime)
            if not current['active']:
                return current
            time.sleep(0.1)
        return {**current, 'status': 'stop_requested', 'reason': '等待当前轮次结束；已提交内容保留，未强制终止进程。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='本机后台调度管理，不修改监控开关')
    parser.add_argument('action', choices=('check','start','status','stop','logs','worker'))
    parser.add_argument('--allow-enabled', action='store_true')
    parser.add_argument('--token')
    args = parser.parse_args(argv)
    try:
        if os.name != 'nt':
            raise ValueError('This local deployment supports Windows')
        if args.action == 'worker':
            if not args.token:
                parser.error('worker requires --token')
            return worker(args.token)
        if args.action == 'check':
            result = preflight(Settings())
        elif args.action == 'start':
            result = start(Settings(), allow_enabled=args.allow_enabled)
        elif args.action == 'stop':
            result = stop()
        elif args.action == 'status':
            result = status()
        else:
            path = RUNTIME / 'worker.log'
            print('\n'.join(path.read_text(encoding='utf-8').splitlines()[-20:]) if path.exists() else '暂无调度日志。')
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result['status'] in ('needs_review','start_blocked','startup_unconfirmed','busy_or_stale','control_busy') else 0
    except Exception:
        print('后台调度管理失败，请检查项目环境、数据库及运行目录；未显示凭证。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
