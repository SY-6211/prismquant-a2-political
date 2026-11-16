"""Windows API supervisor. Own only the child process it created; never seize a port."""
import argparse,json,os,socket,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
import httpx
from app.core.config import PROJECT_ROOT,Settings
from app.scheduler_service import FileLock,read_json,write_json,launch_module

RUNTIME=PROJECT_ROOT/'.runtime/stack'


def now():return datetime.now(timezone.utc).isoformat()


def status():
    with FileLock(RUNTIME/'supervisor.lock') as lock:active=lock.file is None
    state=read_json(RUNTIME/'state.json')
    return {**state,'active':active,'status':state.get('status','starting') if active else 'stopped'}


def port_available(host,port):
    if host!='127.0.0.1':raise ValueError('Local stack requires loopback; remote hosting needs a separate deployment review')
    with socket.socket() as sock:
        try:sock.bind((host,port));return True
        except OSError:return False


def probe(settings,token):
    try:
        with httpx.Client(trust_env=False,follow_redirects=False,timeout=2) as client:
            response=client.get(f'http://127.0.0.1:{settings.port}/api/v1/module-info')
            return response.status_code==200 and response.json().get('runtime_instance')==token
    except (httpx.HTTPError,ValueError):return False


def spawn(settings,token):
    env=os.environ.copy();env.update(PQ_ENVIRONMENT='production',PQ_CONSOLE_MODE='research',PQ_RUNTIME_INSTANCE=token)
    # Windows venv python.exe is a redirector. Launch the actual interpreter with
    # this environment's packages so the owned handle is the API server itself.
    interpreter=getattr(sys,'_base_executable',sys.executable)
    packages=Path(sys.prefix)/'Lib/site-packages'
    env['PYTHONPATH']=str(packages)+os.pathsep+str(PROJECT_ROOT)
    return subprocess.Popen([interpreter,'-X','utf8','-m','app.runtime_api'],cwd=PROJECT_ROOT,env=env,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def maintain(settings,state,child,token,*,clock=time.time,launcher=spawn,available=port_available,healthy=probe):
    current=clock()
    if child is not None and child.poll() is not None:
        state['last_exit_code']=child.returncode;child=None
        failures=state.get('failures',0)+1
        state.update(failures=failures,next_restart_at=current+min(300,2**min(failures,8)),status='backoff')
    if child is None and current>=state.get('next_restart_at',0):
        if available(settings.host,settings.port):
            try:
                child=launcher(settings,token);state.update(api_pid=child.pid,status='starting',restart_count=state.get('restart_count',0)+1)
            except OSError:
                state.update(status='launch_failed',next_restart_at=current+30)
        else:
            state.update(status='port_in_use',next_restart_at=current+30)
    if child is not None:
        if healthy(settings,token):state.update(status='running',failures=0)
        else:state['status']='starting_or_unhealthy' # A live child is never force-restarted on a failed probe.
    state['heartbeat_at']=now()
    return state,child


def run(token):
    settings=Settings();RUNTIME.mkdir(parents=True,exist_ok=True)
    if settings.host!='127.0.0.1':raise ValueError('Loopback required')
    with FileLock(RUNTIME/'supervisor.lock') as ownership:
        if ownership.file is None:return 2
        child=None;state={'supervisor_id':token,'pid':os.getpid(),'started_at':now(),'restart_count':0,'status':'starting'}
        try:
            while read_json(RUNTIME/'desired.json').get('supervisor_id')==token:
                state,child=maintain(settings,state,child,token)
                # Honor an explicit collector stop. Restart only a crashed, still-desired guard.
                from app.service_guard import RUNTIME as guard_runtime,status as guard_status
                if read_json(guard_runtime/'desired.json').get('enabled') and not guard_status()['active'] and time.time()>=state.get('next_guard_check',0):
                    state['next_guard_check']=time.time()+30
                    try:launch_module('app.service_guard',['start'])
                    except Exception:state['guardian_recovery']='launch_failed'
                write_json(RUNTIME/'state.json',state)
                for _ in range(3):
                    if read_json(RUNTIME/'desired.json').get('supervisor_id')!=token:break
                    time.sleep(1)
        finally:
            # This handle refers only to the process created above, never a PID read from disk.
            if child is not None and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            state.update(status='stopped',heartbeat_at=now());write_json(RUNTIME/'state.json',state)
    return 0


def start(settings):
    with FileLock(RUNTIME/'control.lock') as control:
        if control.file is None:return {'status':'control_busy'}
        if status()['active']:return status()
        if not port_available(settings.host,settings.port):return {'status':'port_in_use','active':False}
        token=str(uuid4());write_json(RUNTIME/'desired.json',{'supervisor_id':token})
        launch_module('app.stack',['run','--token',token])
        for _ in range(40):
            value=status()
            if value.get('supervisor_id')==token and value.get('status')=='running':return value
            time.sleep(.2)
        return status()


def stop():
    with FileLock(RUNTIME/'control.lock') as control:
        if control.file is None:return {'status':'control_busy'}
        write_json(RUNTIME/'desired.json',{})
    for _ in range(30):
        value=status()
        if not value['active']:return value
        time.sleep(.2)
    return {**value,'status':'stop_requested'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('start','stop','status','run'));parser.add_argument('--token');args=parser.parse_args()
    try:
        if args.action=='run':return run(args.token or str(uuid4()))
        value=start(Settings()) if args.action=='start' else stop() if args.action=='stop' else status()
        print(json.dumps(value,ensure_ascii=False,indent=2));return 0 if value.get('status') in ('running','stopped','stop_requested') else 1
    except Exception:print(json.dumps({'status':'failed','message':'运行管理失败；检查本机配置、端口和私有日志。'}));return 1


if __name__=='__main__':raise SystemExit(main())
