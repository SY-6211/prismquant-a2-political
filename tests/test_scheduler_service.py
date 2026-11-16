import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.scheduler_service import FileLock, launch_worker, preflight, start, status, stop, worker, write_json


class SchedulerServiceTests(unittest.TestCase):
    def test_launch_uses_hidden_windows_process_creation(self):
        import base64
        with patch('app.scheduler_service.subprocess.run', return_value=MagicMock(returncode=0)) as run:
            launch_worker('test-token')
        script = base64.b64decode(run.call_args.args[0][-1]).decode('utf-16-le')
        self.assertIn('Win32_ProcessStartup', script)
        self.assertIn('ShowWindow=[uint16]0', script)
        self.assertIn('test-token', script)

    def test_launch_failure_is_reported(self):
        with patch('app.scheduler_service.subprocess.run', return_value=MagicMock(returncode=1)), self.assertRaises(RuntimeError):
            launch_worker('test-token')

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.runtime = Path(self.folder.name)

    def test_file_lock_prevents_second_owner_and_releases(self):
        path = self.runtime / 'test.lock'
        with FileLock(path) as first:
            self.assertIsNotNone(first.file)
            with FileLock(path) as second:
                self.assertIsNone(second.file)
        with FileLock(path) as third:
            self.assertIsNotNone(third.file)

    def test_stale_pid_file_without_lock_is_stopped(self):
        write_json(self.runtime/'state.json', {'pid': 1, 'status':'running'})
        self.assertFalse(status(self.runtime)['active'])
        self.assertEqual(stop(self.runtime)['status'], 'stopped')

    def test_active_worker_stale_heartbeat_is_not_reported_healthy(self):
        write_json(self.runtime/'state.json', {'status':'running', 'heartbeat_at':(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()})
        with FileLock(self.runtime/'worker.lock'):
            self.assertEqual(status(self.runtime)['status'], 'busy_or_stale')

    def test_worker_cooperative_stop_leaves_final_state_and_compact_log(self):
        def run(settings, sources):
            write_json(self.runtime/'stop.json', {'worker_id':'test-token'})
            return {'items':[{'source':'ecb','status':'disabled','detail':'DO_NOT_LOG_BODY'}]}
        with patch('app.scheduler_service.Settings'):
            self.assertEqual(worker('test-token', self.runtime, run_tick=run), 0)
        state = status(self.runtime)
        self.assertEqual(state['cycle_count'], 1)
        self.assertEqual(state['status'], 'stopped')
        self.assertNotIn('DO_NOT_LOG_BODY', (self.runtime/'worker.log').read_text(encoding='utf-8'))

    def test_wrong_stop_token_does_not_stop_new_worker(self):
        write_json(self.runtime/'stop.json', {'worker_id':'old-token'})
        calls = []
        def run(*_):
            calls.append(1)
            write_json(self.runtime/'stop.json', {'worker_id':'new-token'})
            return {'items':[]}
        with patch('app.scheduler_service.Settings'):
            worker('new-token', self.runtime, run_tick=run)
        self.assertEqual(len(calls), 1)

    def test_unexpected_error_logs_no_exception_details(self):
        def broken(*_):
            raise ValueError('SECRET')
        with patch('app.scheduler_service.Settings'):
            self.assertEqual(worker('error-token', self.runtime, run_tick=broken), 1)
        self.assertFalse(status(self.runtime)['active'])
        self.assertNotIn('SECRET', (self.runtime/'worker.log').read_text(encoding='utf-8'))

    def test_start_blocks_bad_preflight_or_enabled_sources(self):
        with patch('app.scheduler_service.preflight', return_value={'status':'ready','eligible_count':1}), patch('app.scheduler_service.subprocess.Popen') as process:
            self.assertEqual(start(None,self.runtime)['status'], 'start_blocked')
            process.assert_not_called()
        with patch('app.scheduler_service.preflight', return_value={'status':'needs_review','eligible_count':0}):
            self.assertEqual(start(None,self.runtime)['status'], 'start_blocked')

    def test_start_existing_worker_does_not_spawn_duplicate(self):
        write_json(self.runtime/'state.json', {'worker_id':'existing','status':'running','heartbeat_at':datetime.now(timezone.utc).isoformat()})
        with FileLock(self.runtime/'worker.lock'), patch('app.scheduler_service.subprocess.Popen') as process:
            self.assertEqual(start(None,self.runtime)['worker_id'], 'existing')
            process.assert_not_called()
