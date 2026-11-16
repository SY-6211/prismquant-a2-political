import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from app.chat_model import CreateConversation,EditConversation,UserMessage,creation_hash,new_token,token_digest
from app.chat_admin import credential_path,provision,main
from app.core.config import Settings
from app.main import create_app
from app.repositories import chat as repo


class ChatContracts(unittest.TestCase):
    def setUp(self):
        self.client=TestClient(create_app(Settings(_env_file=None,environment='test')))
        self.addCleanup(self.client.close)
        self.token=new_token();self.headers={'Authorization':'Bearer '+self.token}

    def test_credentials_are_random_scoped_and_not_password_digests(self):
        a,b=new_token(),new_token();self.assertNotEqual(a,b)
        self.assertEqual(len(token_digest(a)),64);self.assertNotEqual(token_digest(a),a)
        for invalid in ('','sk-or-v1-private','pqchat_short','a'*50):
            with self.assertRaises(ValueError):token_digest(invalid)

    def test_no_anonymous_or_forged_identity_access(self):
        for path in ('/me','/conversations','/conversations/'+str(uuid4())+'/messages'):
            response=self.client.get('/api/v1/agent-chat'+path,headers={'X-User-ID':str(uuid4())})
            self.assertEqual(response.status_code,401);self.assertEqual(response.headers['cache-control'],'no-store')
        with patch('app.api.chat.repo.execute') as query:
            self.assertEqual(self.client.get('/api/v1/agent-chat/me',headers={'Authorization':'Bearer model-key'}).status_code,401)
            query.assert_not_called()

    def test_user_and_role_fields_cannot_be_supplied_and_error_omits_content(self):
        for body in ({'user_id':str(uuid4()),'title':'private-secret-text','client_request_id':str(uuid4())},
                     {'role':'assistant','content':'private-secret-text','client_request_id':str(uuid4())}):
            endpoint='/conversations' if 'title' in body else '/conversations/'+str(uuid4())+'/messages'
            response=self.client.post('/api/v1/agent-chat'+endpoint,headers=self.headers,json=body)
            self.assertEqual(response.status_code,422);self.assertNotIn('private-secret-text',response.text)
            self.assertEqual(response.headers['cache-control'],'no-store')

    def test_message_preserves_unicode_but_rejects_blank_and_oversize(self):
        original='  关税 😀 %_\n下一行  '
        self.assertEqual(UserMessage(client_request_id=uuid4(),content=original).content,original)
        for value in (' ','\n','x'*4001,'nul\x00value'):
            with self.assertRaises(ValidationError):UserMessage(client_request_id=uuid4(),content=value)
        for value in ('','\n','a'*121,'multi\nline'):
            with self.assertRaises(ValidationError):CreateConversation(client_request_id=uuid4(),title=value)
        with self.assertRaises(ValidationError):EditConversation(expected_revision=1)

    def test_creation_fingerprint_is_independent_of_python_uuid_representation(self):
        pid=uuid4()
        self.assertEqual(creation_hash('标题',pid),creation_hash('标题',str(pid)))
        self.assertNotEqual(creation_hash('标题',pid),creation_hash('另一标题',pid))

    def test_errors_are_bounded_and_do_not_expose_secrets(self):
        for error,expected in [(repo.Unauthorized(),401),(repo.Missing(),404),(repo.Conflict('会话已变化'),409),
                               (SQLAlchemyError('database-private-password'),503),(ValueError('private-key'),503)]:
            with patch('app.api.chat.repo.execute',side_effect=error):
                response=self.client.get('/api/v1/agent-chat/me',headers=self.headers)
                self.assertEqual(response.status_code,expected);self.assertEqual(response.headers['cache-control'],'no-store')
                self.assertNotIn('private-password',response.text);self.assertNotIn('private-key',response.text)

    def test_date_pagination_and_query_bounds(self):
        for suffix in ('?limit=101','?q='+('a'*101),'?since=2026-11-09T00:00:00','?since=2026-11-10T00:00:00Z&until=2026-11-09T00:00:00Z'):
            self.assertEqual(self.client.get('/api/v1/agent-chat/conversations'+suffix,headers=self.headers).status_code,422)
        response=self.client.get('/api/v1/agent-chat/conversations/'+str(uuid4())+'/messages?after=-1',headers=self.headers)
        self.assertEqual(response.status_code,422)

    def test_no_answer_write_registration_or_cookie_login_endpoint(self):
        schema=self.client.get('/openapi.json').json();paths=schema['paths']
        chat=[p for p in paths if p.startswith('/api/v1/agent-chat')]
        self.assertEqual(len(chat),13)
        self.assertNotIn('/api/v1/agent-chat/register',paths)
        self.assertEqual(self.client.post('/api/v1/agent-chat/answers',json={}).status_code,404)
        for p in chat:
            for spec in paths[p].values():self.assertEqual(spec.get('security'),[{'ChatUserToken':[]}])

    def test_cli_paths_and_exclusive_files_do_not_overwrite_or_print_token(self):
        with tempfile.TemporaryDirectory() as temporary,patch('app.chat_admin.PRIVATE',Path(temporary)),patch('app.chat_admin.PROJECT_ROOT',Path(temporary)):
            root=Path(temporary);existing=root/'credential.json';existing.write_text('keep',encoding='utf-8')
            with self.assertRaises(FileExistsError):provision(None,name='本机用户',path=existing)
            self.assertEqual(existing.read_text(),'keep')
            with self.assertRaises(ValueError):credential_path(root.parent/'outside-credential.json')
            with patch('app.chat_admin.repo.engine_for',side_effect=ValueError('secret')):
                with self.assertRaises(ValueError):provision(None,name='本机用户',path=root/'new.json')
            self.assertFalse((root/'new.json').exists())
        output=io.StringIO()
        with patch('sys.argv',['chat_admin','create-user','--name','用户']),patch('app.chat_admin.provision',side_effect=ValueError('secret-token')),redirect_stdout(output):
            self.assertEqual(main(),1)
        self.assertNotIn('secret-token',output.getvalue())


if __name__=='__main__':unittest.main()
