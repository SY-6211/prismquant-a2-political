import json,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.main import create_app
from app.api.module_info import protected
from app.stack import maintain,port_available
from app.restore_check import cleanup
from app import build_delivery


class DeploymentContracts(unittest.TestCase):
    def test_production_operations_are_protected_but_private_auth_is_separate(self):
        client=TestClient(create_app(Settings(_env_file=None,environment='production',console_mode='research',keyword_admin_token='a'*40,source_catalog_admin_token='b'*40)))
        self.addCleanup(client.close)
        for path in ('/api/v1/system-status','/api/v1/collection-runs','/api/v1/web-pilot/summary','/api/v1/monitor-speed','/api/v1/source-catalog'):
            result=client.get(path);self.assertEqual(result.status_code,403);self.assertEqual(result.headers['cache-control'],'no-store')
        self.assertEqual(client.get('/api/v1/agent-chat/me').status_code,401)
        for path,token in (('/api/v1/collection-runs?limit=0','a'*40),('/api/v1/source-catalog?limit=0','b'*40)):
            self.assertEqual(client.get(path,headers={'Authorization':'Bearer '+token}).status_code,422)

    def test_public_metadata_has_no_key_or_configuration_values(self):
        client=TestClient(create_app(Settings(_env_file=None,environment='production',console_mode='research',db_password='private_database_password',openrouter_api_key='private_model_key')))
        self.addCleanup(client.close)
        result=client.get('/api/v1/module-info');self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['console_mode'],'research')
        self.assertNotIn('private_database_password',result.text);self.assertNotIn('private_model_key',result.text)
        schema=client.get('/openapi.json').json()
        self.assertEqual(schema['paths']['/api/v1/system-status']['get']['security'],[{'OperationsAdmin':[]}])
        self.assertEqual(schema['paths']['/api/v1/agent-chat/me']['get']['security'],[{'ChatUserToken':[]}])
        self.assertFalse(protected('/api/v1/collection-runs-other'))

    def test_private_runtime_receipts_stay_bounded_public_input(self):
        client=TestClient(create_app(Settings(_env_file=None,environment='production')));self.addCleanup(client.close)
        self.assertEqual(client.post('/api/v1/monitor-speed/receipts',json={}).status_code,422)

    def test_supervisor_refuses_remote_bind_and_never_seizes_an_occupied_port(self):
        with self.assertRaises(ValueError):port_available('0.0.0.0',8000)
        launch=Mock();state,child=maintain(SimpleNamespace(host='127.0.0.1',port=8000),{},None,'t',clock=lambda:10,available=lambda *_:False,launcher=launch)
        self.assertEqual(state['status'],'port_in_use');self.assertIsNone(child);launch.assert_not_called()

    def test_crashed_child_restarts_after_backoff_only(self):
        settings=SimpleNamespace(host='127.0.0.1',port=8000);dead=Mock(returncode=9);dead.poll.return_value=9
        launch=Mock(return_value=Mock(pid=123))
        state,child=maintain(settings,{},dead,'t',clock=lambda:10,launcher=launch)
        self.assertEqual(state['status'],'backoff');self.assertIsNone(child);launch.assert_not_called()
        state,child=maintain(settings,state,None,'t',clock=lambda:12,available=lambda *_:True,launcher=launch,healthy=lambda *_:True)
        self.assertEqual(state['status'],'running');self.assertEqual(child.pid,123);self.assertEqual(state['restart_count'],1)

    def test_unhealthy_live_child_is_not_force_killed_or_replaced(self):
        child=Mock(pid=12);child.poll.return_value=None;launch=Mock()
        state,value=maintain(SimpleNamespace(),{},child,'t',launcher=launch,healthy=lambda *_:False)
        self.assertIs(value,child);child.terminate.assert_not_called();child.kill.assert_not_called();launch.assert_not_called()
        self.assertEqual(state['status'],'starting_or_unhealthy')

    def test_launch_error_has_bounded_retry(self):
        state,child=maintain(SimpleNamespace(host='127.0.0.1',port=8000),{},None,'t',clock=lambda:1,available=lambda *_:True,launcher=Mock(side_effect=OSError('private details')))
        self.assertEqual(state['status'],'launch_failed');self.assertEqual(state['next_restart_at'],31);self.assertNotIn('private',str(state))

    def test_restore_cleanup_rejects_unowned_or_external_paths(self):
        with tempfile.TemporaryDirectory() as tmp,patch('app.restore_check.ROOT',Path(tmp)/'runs'):
            identity='a'*32;target=Path(tmp)/'runs'/identity;target.mkdir(parents=True)
            with self.assertRaises(ValueError):cleanup(target,identity)
            (target/'owner.json').write_text(json.dumps({'identity':identity}))
            with self.assertRaises(ValueError):cleanup(Path(tmp),identity)
            cleanup(target,identity);self.assertFalse(target.exists())


class DeliveryPackageContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)/'source';self.root.mkdir()
        for name in build_delivery.ROOT_FILES:(self.root/name).write_text('{}' if name.endswith('.json') else 'Portable source fixture',encoding='utf8')
        for name in build_delivery.DIRECTORIES:(self.root/name).mkdir()
        (self.root/'app/example.py').write_text('VALUE = 1')

    def test_private_files_are_excluded_and_hashes_verified(self):
        (self.root/'.env').write_text('PRIVATE_PASSWORD=never_package')
        (self.root/'.runtime').mkdir();(self.root/'.runtime/private.json').write_text('private history')
        target=Path(self.temp.name)/'release.zip';result=build_delivery.build(self.root,target)
        manifest=build_delivery.verify(target);self.assertEqual(result['files'],len(manifest['files']))
        self.assertFalse(any(x['path'].startswith('.runtime') or x['path']=='.env' for x in manifest['files']))
        with self.assertRaises(ValueError):build_delivery.build(self.root,target)

    def test_credential_values_and_informal_docs_block_release(self):
        (self.root/'app/example.py').write_text('TOKEN="'+'sk-or-v1-'+'a'*64+'"')
        with self.assertRaises(ValueError):build_delivery.build(self.root,Path(self.temp.name)/'a.zip')
        (self.root/'app/example.py').write_text('VALUE=1');(self.root/'README.md').write_text('同学说明',encoding='utf8')
        with self.assertRaises(ValueError):build_delivery.build(self.root,Path(self.temp.name)/'b.zip')

    def test_unlisted_archive_member_rejected(self):
        target=Path(self.temp.name)/'release.zip';build_delivery.build(self.root,target)
        with zipfile.ZipFile(target,'a') as z:z.writestr('political-monitor/private.txt','extra')
        with self.assertRaises(ValueError):build_delivery.verify(target)
