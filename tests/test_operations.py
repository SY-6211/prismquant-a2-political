import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from fastapi.testclient import TestClient
from app.main import create_app
from app.core.config import Settings
from app.service_guard import cycle
from app.backup_database import create_backup


class OperationsTests(unittest.TestCase):
    def test_stop_intent_is_rechecked_under_worker_control_lock(self):
        from app.scheduler_service import start
        with patch('app.scheduler_service.FileLock') as lock,patch('app.scheduler_service.read_json',return_value={'enabled':False}),patch('app.scheduler_service.launch_worker') as launch:
            lock.return_value.__enter__.return_value.file=True
            result=start(None,guarded=True)
        self.assertEqual(result['status'],'guard_disabled');launch.assert_not_called()

    def test_daily_backup_is_not_repeated_each_guard_cycle(self):
        with patch('app.service_guard.worker_status',return_value={'active':True,'status':'running'}),patch('app.service_guard.read_json',return_value={}),patch('app.service_guard.create_backup',return_value={'status':'ok'}) as backup:
            state=cycle(None,{},clock=lambda:100);cycle(None,state,clock=lambda:101)
        self.assertEqual(backup.call_count,1);self.assertEqual(state['next_backup_at'],86500)

    def test_retention_keeps_only_requested_managed_archives(self):
        def dump(arguments,*args,**kwargs):
            Path(arguments[-1]).write_bytes(b'archive')
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);(folder/'unrelated.dump').write_bytes(b'keep')
            with patch('app.backup_database.executable',return_value='pg_dump'),patch('app.backup_database.run_tool',side_effect=dump),patch('app.backup_database.verify_archive',return_value={'bytes':7,'sha256':'hash','archive_verified':True,'restore_tested':False}):
                for _ in range(4):
                    result=create_backup(Settings(_env_file=None),folder=folder,state_path=folder/'state.json',keep=2)
                    self.assertEqual(result['status'],'ok')
            self.assertEqual(len(list(folder.glob('pq-*.dump'))),2)
            self.assertTrue((folder/'unrelated.dump').exists())

    def test_dead_collector_restarts_but_live_or_stale_worker_is_never_killed(self):
        for worker in ({'active':True,'status':'busy_or_stale'},{'active':False,'status':'stopped'}):
            with patch('app.service_guard.worker_status',return_value=worker),patch('app.service_guard.start_worker',return_value={'active':True,'status':'running'}) as start,patch('app.service_guard.read_json',return_value={'status':'ok','finished_at':'2030-01-01T00:00:00+00:00'}):
                state=cycle(None,{},clock=lambda:100)
            self.assertEqual(start.call_count,0 if worker['active'] else 1)
            if not worker['active']:self.assertEqual(state['restart_count'],1)

    def test_failed_start_has_bounded_retry_delay(self):
        with patch('app.service_guard.worker_status',return_value={'active':False}),patch('app.service_guard.start_worker',return_value={'status':'start_blocked'}) as start,patch('app.service_guard.read_json',return_value={'status':'ok','finished_at':'2030-01-01T00:00:00+00:00'}):
            state=cycle(None,{},clock=lambda:100);cycle(None,state,clock=lambda:101)
        self.assertEqual(start.call_count,1);self.assertEqual(state['next_restart_at'],110)

    def test_backup_failure_keeps_previous_backup_and_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);(folder/'important.txt').write_text('keep')
            (folder/'old.dump').write_bytes(b'keep')
            with patch('app.backup_database.executable',return_value='pg_dump'),patch('app.backup_database.run_tool',side_effect=RuntimeError('DO_NOT_EXPOSE_PASSWORD')):
                result=create_backup(Settings(_env_file=None),folder=folder,state_path=folder/'state.json')
            self.assertEqual(result['status'],'failed')
            self.assertEqual((folder/'old.dump').read_bytes(),b'keep')
            self.assertTrue((folder/'important.txt').exists())
            self.assertNotIn('DO_NOT_EXPOSE_PASSWORD',json.dumps(result))

    def test_status_endpoint_redacts_local_paths_and_process_ids(self):
        with patch('app.api.operations.create_read_engine',side_effect=ValueError('PASSWORD')),patch('app.api.operations.collector_status',return_value={'status':'running','active':True,'pid':999,'password':'secret'}),patch('app.api.operations.guard_status',return_value={'status':'stopped','active':False}),patch('app.api.operations.read_json',return_value={'status':'ok','filename':'private.dump','sha256':'secret'}):
            with TestClient(create_app(Settings(_env_file=None))) as client:
                response=client.get('/api/v1/system-status')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['database']['status'],'unavailable')
        for value in ('PASSWORD','secret','private.dump','999'):self.assertNotIn(value,response.text)
