"""Local PostgreSQL custom-format backups. Credentials never enter argv or logs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import PROJECT_ROOT, Settings
from app.scheduler_service import FileLock, read_json, write_json

BACKUPS = PROJECT_ROOT / '.backups'
STATE = PROJECT_ROOT / '.runtime' / 'backup.json'


def executable(name):
    found = shutil.which(name)
    if found:
        return found
    path = Path(r'C:\Program Files\PostgreSQL\18\bin') / (name + '.exe')
    if not path.is_file():
        raise ValueError('PostgreSQL backup tool missing')
    return str(path)


def run_tool(arguments, settings, *, stdout=subprocess.DEVNULL):
    environment = os.environ.copy()
    environment['PGPASSWORD'] = settings.db_password.get_secret_value()
    environment['PGCONNECT_TIMEOUT'] = '10'
    result = subprocess.run(arguments, env=environment, stdin=subprocess.DEVNULL,
                            stdout=stdout, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=300)
    if result.returncode:
        raise RuntimeError('PostgreSQL backup command failed')


def verify_archive(path, settings):
    # Listing alone does not read all data blocks. Rendering all SQL to NUL
    # also verifies that the custom archive can be fully decompressed/read.
    tool = executable('pg_restore')
    run_tool([tool, '--list', str(path)], settings)
    run_tool([tool, '--file', os.devnull, str(path)], settings)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'bytes': path.stat().st_size, 'sha256': digest, 'archive_verified': True,
            'restore_tested': False}


def create_backup(settings, *, folder=BACKUPS, state_path=STATE, keep=7):
    if not 1 <= keep <= 100:
        raise ValueError('Retention must be 1–100')
    folder, state_path = Path(folder).resolve(), Path(state_path)
    folder.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(folder / 'backup.lock') as lock:
        if lock.file is None:
            return {'status': 'busy'}
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        name = f'pq-{stamp}-{uuid4().hex[:8]}.dump'
        target, partial = folder / name, folder / (name + '.partial')
        state = read_json(state_path)
        try:
            run_tool([executable('pg_dump'), '--format=custom', '--no-owner', '--no-privileges',
                      '--host', settings.db_host, '--port', str(settings.db_port),
                      '--username', settings.db_user, '--dbname', settings.db_name,
                      '--file', str(partial)], settings)
            info = verify_archive(partial, settings)
            os.replace(partial, target)
            state.update(status='ok', filename=name, finished_at=datetime.now(timezone.utc).isoformat(),
                         **info)
            state.pop('error', None)
            write_json(state_path, state)
            # Only files named by our successful-backup manifests are eligible
            # for retention. Never enumerate arbitrary files for deletion.
            manifest = target.with_suffix('.json')
            write_json(manifest, {**state, 'managed_by':'prismquant-backup-v1'})
            manifests = []
            for entry in folder.glob('pq-*.json'):
                data = read_json(entry)
                if data.get('managed_by') == 'prismquant-backup-v1' and Path(data.get('filename','')).name == data.get('filename'):
                    manifests.append((entry, data))
            manifests.sort(key=lambda item: item[1].get('finished_at', ''), reverse=True)
            for entry, data in manifests[keep:]:
                old = (folder / data['filename']).resolve()
                if old.parent == folder and old.suffix == '.dump' and old != target:
                    old.unlink(missing_ok=True)
                    entry.unlink(missing_ok=True)
            return state
        except Exception:
            partial.unlink(missing_ok=True)
            state.update(status='failed', attempted_at=datetime.now(timezone.utc).isoformat(),
                         error='备份失败；上次成功备份保留，请检查工具、数据库和磁盘空间。')
            write_json(state_path, state)
            return state


def main():
    parser = argparse.ArgumentParser(description='本机备份与归档校验，不覆盖或恢复当前数据库')
    parser.add_argument('action', choices=('create','status','verify'))
    parser.add_argument('--file', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'create': result = create_backup(Settings())
        elif args.action == 'status': result = read_json(STATE) or {'status':'never'}
        else:
            if not args.file: parser.error('verify requires --file')
            info = verify_archive(args.file, Settings())
            manifest = read_json(args.file.with_suffix('.json'))
            if manifest.get('managed_by') == 'prismquant-backup-v1' and manifest.get('sha256') != info['sha256']:
                raise ValueError('Archive hash differs from successful backup manifest')
            result = {'status':'ok', 'manifest_matched': bool(manifest.get('managed_by') == 'prismquant-backup-v1'), **info}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('status') == 'ok' else 1
    except Exception:
        print('备份工具执行失败；未显示连接凭证，未恢复或覆盖数据库。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
