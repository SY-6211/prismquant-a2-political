"""Restore a verified local backup into a disposable loopback PostgreSQL instance."""
import argparse,hashlib,json,os,re,secrets,shutil,socket,subprocess
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine,text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool
from app.core.config import PROJECT_ROOT,Settings
from app.core.database import create_read_engine
from app.backup_database import create_backup,verify_archive,executable,BACKUPS,STATE
from app.scheduler_service import read_json,write_json

ROOT=PROJECT_ROOT/'.runtime/restore-check'
TABLES=('public_records','archive_post_details','event_versions','agent_chat_messages','agent_qa_runs',
 'agent_qa_results','agent_analysis_runs','agent_analysis_results','annotation_labels','agent_answer_feedback')


def fingerprints(c):
    return {table:sorted(hashlib.sha256(v.encode()).hexdigest() for v in c.execute(text('SELECT to_jsonb(t)::text FROM '+table+' t')).scalars()) for table in TABLES}


def cleanup(target,identity):
    root=ROOT.resolve();path=Path(target).resolve()
    if path.parent!=root or not re.fullmatch(r'[a-f0-9]{32}',path.name) or path.name!=identity:
        raise ValueError('Unsafe restore-check cleanup path')
    if read_json(path/'owner.json').get('identity')!=identity:raise ValueError('Restore workspace is not owned by this run')
    for item in path.rglob('*'):
        if item.is_symlink() or item.is_junction() or not item.resolve().is_relative_to(path):raise ValueError('Unexpected restore workspace link')
    shutil.rmtree(path)


def call(name,args,env,timeout=120):
    result=subprocess.run([executable(name),*map(str,args)],env=env,stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=timeout)
    if result.returncode:raise RuntimeError(name+'_failed')


def check(settings,*,output=None):
    identity=uuid4().hex;folder=ROOT/identity;folder.mkdir(parents=True,exist_ok=False)
    write_json(folder/'owner.json',{'identity':identity})
    target=folder/'data';engine=None;result={'status':'failed','restore_tested':False,'source_database_untouched':True,'model_requests':0}
    env=os.environ.copy();password=secrets.token_urlsafe(40);env.update(PGPASSWORD=password,PGCONNECT_TIMEOUT='5')
    stage='source_snapshot';stopped=False
    try:
        source=create_read_engine(settings)
        try:
            with source.connect().execution_options(isolation_level='REPEATABLE READ') as c:
                before=fingerprints(c);revision=c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
        finally:source.dispose()
        stage='backup';backup=create_backup(settings)
        if backup['status']!='ok':raise RuntimeError('backup_failed')
        archive=BACKUPS/backup['filename'];info=verify_archive(archive,settings)
        if info['sha256']!=backup['sha256']:raise ValueError('Backup hash differs')
        password_file=folder/'password.txt';password_file.write_text(password+'\n',encoding='utf8')
        stage='initdb';call('initdb',['-D',target,'-U','pq_restore','--auth-host=scram-sha-256','--auth-local=scram-sha-256','--pwfile',password_file,'--encoding=UTF8','--locale=C'],env)
        password_file.unlink()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        stage='start';call('pg_ctl',['start','-w','-t','30','-D',target,'-l',folder/'server.log','-o',f'-h 127.0.0.1 -p {port} -c max_connections=10 -c shared_buffers=16MB'],env,45)
        stage='restore';call('pg_restore',['--no-owner','--no-privileges','--exit-on-error','-h','127.0.0.1','-p',port,'-U','pq_restore','-d','postgres',archive],env,180)
        stage='verify';engine=create_engine(URL.create('postgresql+psycopg',username='pq_restore',password=password,host='127.0.0.1',port=port,database='postgres'),poolclass=NullPool,hide_parameters=True)
        with engine.connect() as c:
            restored=fingerprints(c)
            if c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()!=revision:raise ValueError('Restored migration differs')
            if any(not set(rows).issubset(restored[table]) for table,rows in before.items()):raise ValueError('Restored immutable samples differ')
        result.update(status='ok',restore_tested=True,archive_sha256=info['sha256'],migration=revision,
            tables_checked=len(TABLES),baseline_rows=sum(map(len,before.values())),restored_rows=sum(map(len,restored.values())))
        # Mark only this exact successful backup, never another concurrent backup.
        for manifest in (archive.with_suffix('.json'),STATE):
            data=read_json(manifest)
            if data.get('filename')==archive.name and data.get('sha256')==info['sha256']:
                data['restore_tested']=True;write_json(manifest,data)
    except Exception:
        result.update(failure_stage=stage,message='恢复验证未通过；源数据库未被恢复或覆盖。')
    finally:
        if engine:engine.dispose()
        try:
            if (target/'postmaster.pid').exists():call('pg_ctl',['stop','-w','-t','30','-m','fast','-D',target],env,40)
            stopped=not (target/'postmaster.pid').exists()
        except Exception:stopped=False
        result['temporary_server_stopped']=stopped
        if stopped:
            try:cleanup(folder,identity);result['temporary_files_removed']=True
            except Exception:result['temporary_files_removed']=False
        else:result['temporary_files_removed']=False
        if not stopped or not result['temporary_files_removed']:result['status']='cleanup_needs_review'
    if output:
        output=Path(output)
        with output.open('x',encoding='utf8') as file:json.dump(result,file,ensure_ascii=False,indent=2)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path);args=p.parse_args()
    if args.out and args.out.exists():p.error('Output exists; preserve previous reports')
    value=check(Settings(),output=args.out);print(json.dumps(value,ensure_ascii=False,indent=2));return 0 if value['status']=='ok' else 1


if __name__=='__main__':raise SystemExit(main())
