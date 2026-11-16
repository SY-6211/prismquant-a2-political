"""Synthetic chat identities/history exist only in disposable PostgreSQL schemas."""
import os
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.chat_model import CreateConversation,EditConversation,UserMessage,new_token,token_digest
from app.core.config import Settings
from app.main import create_app
from app.repositories import chat as repo
from app.chat_admin import provision
from pathlib import Path
import test_agent_tools_db as fixture
REAL_EXECUTE=repo.execute


@unittest.skipUnless(os.environ.get('PQ_CHAT_DB_TESTS')=='1','Isolated chat database tests are opt-in')
class ChatDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixture.AgentToolDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixture.AgentToolDatabaseTests.drop_schema.__func__)

    def setUp(self):
        self.c=self.engine.connect();self.tx=self.c.begin()
        self.addCleanup(self.c.close);self.addCleanup(lambda:self.tx.rollback() if self.tx.is_active else None)
        self.a=repo.create_user(self.c,'合成用户甲');self.b=repo.create_user(self.c,'合成用户乙')
        self.ta,self.tb=new_token(),new_token()
        self.issued_a=repo.issue_token(self.c,self.a['user_id'],self.ta,30)
        self.issued_b=repo.issue_token(self.c,self.b['user_id'],self.tb,30)
        self.body=CreateConversation(client_request_id=uuid4(),title='合成政策研究 %_')
        self.conv=repo.create_conversation(self.c,self.a['user_id'],self.body)['conversation']
        self.client=TestClient(create_app(Settings(_env_file=None,environment='test')));self.addCleanup(self.client.close)
        def execute(settings,token,fn,*args):
            return fn(self.c,repo.authenticate(self.c,token)['user_id'],*args)
        self.patch=patch('app.api.chat.repo.execute',side_effect=execute);self.patch.start();self.addCleanup(self.patch.stop)
        self.ha={'Authorization':'Bearer '+self.ta};self.hb={'Authorization':'Bearer '+self.tb}
        self.url='/api/v1/agent-chat/conversations/'+str(self.conv['conversation_id'])

    def question(self,content='合成问题：关税政策如何变化？',request=None):
        return UserMessage(client_request_id=request or uuid4(),content=content)

    def test_owner_can_reopen_but_other_user_gets_same_404_as_missing(self):
        self.assertEqual(self.client.get(self.url,headers=self.ha).status_code,200)
        for method,url,body in [('GET',self.url,None),('GET',self.url+'/messages',None),('PATCH',self.url,{'expected_revision':1,'title':'窃改'}),('POST',self.url+'/messages',{'client_request_id':str(uuid4()),'content':'越权'})]:
            response=self.client.request(method,url,headers=self.hb,json=body)
            self.assertEqual(response.status_code,404)
            self.assertEqual(response.json(),self.client.get('/api/v1/agent-chat/conversations/'+str(uuid4()),headers=self.hb).json())
        self.assertEqual(self.client.get('/api/v1/agent-chat/conversations',headers=self.hb).json()['items'],[])
        self.assertEqual(repo.conversation(self.c,self.a['user_id'],self.conv['conversation_id'])['revision'],1)

    def test_duplicate_create_and_message_are_idempotent_but_conflicts_rejected(self):
        again=repo.create_conversation(self.c,self.a['user_id'],self.body)
        self.assertFalse(again['created']);self.assertEqual(again['conversation']['conversation_id'],self.conv['conversation_id'])
        with self.assertRaises(repo.Conflict):repo.create_conversation(self.c,self.a['user_id'],self.body.model_copy(update={'title':'changed'}))
        message=self.question();one=repo.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],message)
        two=repo.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],message)
        self.assertFalse(two['created']);self.assertEqual(one['message']['message_id'],two['message']['message_id'])
        self.assertEqual(two['answer_status'],'separate_history')
        with self.assertRaises(repo.Conflict):repo.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],message.model_copy(update={'content':'changed'}))
        self.assertEqual(repo.conversation(self.c,self.a['user_id'],self.conv['conversation_id'])['message_count'],1)

    def test_client_request_id_can_be_reused_by_a_different_owner_without_leaking(self):
        other=repo.create_conversation(self.c,self.b['user_id'],self.body)['conversation']
        self.assertNotEqual(other['conversation_id'],self.conv['conversation_id'])
        response=self.client.get('/api/v1/agent-chat/conversations?cursor='+str(self.conv['conversation_id']),headers=self.hb)
        self.assertEqual(response.status_code,404)

    def test_message_order_pagination_unicode_and_keyword_literal_search(self):
        for content in ('first😀','literal %_','last quote \' OR 1=1 --'):
            repo.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],self.question(content))
        page=repo.messages(self.c,self.a['user_id'],self.conv['conversation_id'],2)
        self.assertEqual([m['position'] for m in page['items']],[1,2]);self.assertEqual(page['next_after'],2)
        rest=repo.messages(self.c,self.a['user_id'],self.conv['conversation_id'],2,page['next_after'])
        self.assertEqual([m['position'] for m in rest['items']],[3]);self.assertIsNone(rest['next_after'])
        for term,expected in [('%_',1),('first😀',1),('not there',0),('\\not',0)]:
            self.assertEqual(len(repo.list_conversations(self.c,self.a['user_id'],query=term)),expected)
        # A literal SQL fragment only matches a conversation actually containing it.
        self.assertEqual(len(repo.list_conversations(self.c,self.a['user_id'],query="' OR 1=1 --")),1)

    def test_conversation_cursor_and_creation_date_filters(self):
        for i in range(3):repo.create_conversation(self.c,self.a['user_id'],CreateConversation(client_request_id=uuid4(),title='page '+str(i)))
        first=repo.list_conversations(self.c,self.a['user_id'],limit=2)
        rest=repo.list_conversations(self.c,self.a['user_id'],limit=2,cursor=first[1]['conversation_id'])
        self.assertEqual(len({r['conversation_id'] for r in first[:2]+rest}),4)
        now=datetime.now(timezone.utc)
        self.assertEqual(len(repo.list_conversations(self.c,self.a['user_id'],since=now+timedelta(days=1))),0)
        self.assertEqual(len(repo.list_conversations(self.c,self.a['user_id'],until=now+timedelta(days=1))),4)

    def test_archiving_and_optimistic_revision_preserve_old_questions(self):
        question=self.question();saved=repo.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],question)
        with self.assertRaises(repo.Conflict):repo.edit_conversation(self.c,self.a['user_id'],self.conv['conversation_id'],EditConversation(expected_revision=1,title='stale'))
        changed=repo.edit_conversation(self.c,self.a['user_id'],self.conv['conversation_id'],EditConversation(expected_revision=2,title='new name',archived=True))
        self.assertEqual(changed['revision'],3);self.assertEqual(repo.list_conversations(self.c,self.a['user_id']),[])
        self.assertEqual(len(repo.list_conversations(self.c,self.a['user_id'],archived=True)),1)
        self.assertEqual(repo.messages(self.c,self.a['user_id'],self.conv['conversation_id'])['items'][0]['message_id'],saved['message']['message_id'])
        self.assertFalse(repo.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],question)['created'])
        with self.assertRaises(repo.Conflict):repo.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],self.question())
        repo.edit_conversation(self.c,self.a['user_id'],self.conv['conversation_id'],EditConversation(expected_revision=3,archived=False))
        self.assertEqual(len(repo.list_conversations(self.c,self.a['user_id'])),1)

    def test_raw_credentials_are_not_stored_or_returned_and_revocation_works(self):
        stored=self.c.execute(text('SELECT token_digest FROM agent_chat_tokens WHERE token_id=:id'),{'id':self.issued_a['token_id']}).scalar_one()
        self.assertEqual(stored,token_digest(self.ta));self.assertNotEqual(stored,self.ta)
        result=self.client.get('/api/v1/agent-chat/me',headers=self.ha)
        self.assertEqual(result.json()['display_name'],'合成用户甲');self.assertNotIn(stored,result.text);self.assertNotIn(self.ta,result.text)
        repo.revoke_token(self.c,self.issued_a['token_id'])
        self.assertEqual(self.client.get('/api/v1/agent-chat/me',headers=self.ha).status_code,401)

    def test_expired_disabled_and_independent_tokens_are_enforced(self):
        spare=new_token();repo.issue_token(self.c,self.a['user_id'],spare,1)
        self.c.execute(text("UPDATE agent_chat_tokens SET created_at=now()-interval '2 days',expires_at=now()-interval '1 day' WHERE token_id=:id"),{'id':self.issued_a['token_id']})
        with self.assertRaises(repo.Unauthorized):repo.authenticate(self.c,self.ta)
        self.assertEqual(repo.authenticate(self.c,spare)['user_id'],self.a['user_id'])
        repo.disable_user(self.c,self.a['user_id'])
        with self.assertRaises(repo.Unauthorized):repo.authenticate(self.c,spare)
        self.assertEqual(repo.authenticate(self.c,self.tb)['user_id'],self.b['user_id'])

    def test_db_forbids_message_edits_deletion_and_assistant_impersonation(self):
        saved=repo.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],self.question())
        for sql in ("UPDATE agent_chat_messages SET content='changed' WHERE message_id=:id",'DELETE FROM agent_chat_messages WHERE message_id=:id'):
            with self.assertRaises(DBAPIError),self.c.begin_nested():self.c.execute(text(sql),{'id':saved['message']['message_id']})
        with self.assertRaises(DBAPIError),self.c.begin_nested():
            self.c.execute(text("INSERT INTO agent_chat_messages(message_id,conversation_id,client_request_id,position,role,content) VALUES (:id,:c,:request,2,'assistant','fake answer')"),{'id':uuid4(),'c':self.conv['conversation_id'],'request':uuid4()})

    def test_unknown_person_and_capacity_fail_without_partial_insert(self):
        with self.assertRaises(repo.Invalid):repo.create_conversation(self.c,self.a['user_id'],self.body.model_copy(update={'person_id':uuid4()}))
        self.c.execute(text('UPDATE agent_chat_conversations SET message_count=1000 WHERE conversation_id=:id'),{'id':self.conv['conversation_id']})
        with self.assertRaises(repo.Conflict):repo.append_user(self.c,self.a['user_id'],self.conv['conversation_id'],self.question())
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM agent_chat_messages WHERE conversation_id=:id'),{'id':self.conv['conversation_id']}).scalar_one(),0)

    def committed_conversation(self):
        # Independent transactions prove persistence and exercise real concurrent locks.
        with self.engine.begin() as c:
            user=repo.create_user(c,'合成独立连接用户');token=new_token();repo.issue_token(c,user['user_id'],token,1)
            conv=repo.create_conversation(c,user['user_id'],CreateConversation(client_request_id=uuid4(),title='合成持久会话'))['conversation']
            return user['user_id'],token,conv['conversation_id']

    def test_committed_history_survives_new_connections(self):
        owner,token,identity=self.committed_conversation();question=self.question()
        with self.engine.begin() as c:repo.append_user(c,repo.authenticate(c,token)['user_id'],identity,question)
        with self.engine.begin() as c:
            reopened=repo.messages(c,repo.authenticate(c,token)['user_id'],identity)
            self.assertEqual(reopened['items'][0]['content'],question.content)
            self.assertEqual(reopened['answer_status'],'separate_history')

    def test_provision_writes_private_credential_once_and_only_hash_in_database(self):
        with tempfile.TemporaryDirectory() as temporary,patch('app.chat_admin.PRIVATE',Path(temporary)),patch('app.chat_admin.PROJECT_ROOT',Path(temporary)),patch('app.chat_admin.repo.engine_for',return_value=self.engine):
            info=provision(None,name='合成签发用户',path=Path(temporary)/'private.json')
            saved=json.loads(Path(info['credential_file']).read_text(encoding='utf-8'))
            self.assertNotIn('access_token',info);self.assertFalse(info['secret_printed'])
            with self.engine.begin() as c:
                user=repo.authenticate(c,saved['access_token'])
                self.assertEqual(str(user['user_id']),info['user_id'])
                self.assertEqual(c.execute(text('SELECT token_digest FROM agent_chat_tokens WHERE token_id=:id'),{'id':saved['token_id']}).scalar_one(),token_digest(saved['access_token']))
            with self.assertRaises(FileExistsError):provision(None,name='合成签发用户',path=Path(info['credential_file']))

    def test_failure_rolls_back_message_and_metadata_in_one_transaction(self):
        owner,token,identity=self.committed_conversation()
        def fail_after_append(c,user_id):
            repo.append_user(c,user_id,identity,self.question())
            raise RuntimeError('synthetic interrupted operation')
        with patch('app.repositories.chat.engine_for',return_value=self.engine):
            with self.assertRaises(RuntimeError):REAL_EXECUTE(None,token,fail_after_append)
        with self.engine.begin() as c:
            value=repo.messages(c,owner,identity)
            self.assertEqual(value['items'],[]);self.assertEqual(value['conversation']['message_count'],0)
            self.assertEqual(value['conversation']['revision'],1)

    def test_concurrent_repeated_requests_save_exactly_once_and_distinct_requests_ordered(self):
        owner,token,identity=self.committed_conversation();question=self.question()
        def write(body):
            with self.engine.begin() as c:return repo.append_user(c,repo.authenticate(c,token)['user_id'],identity,body)
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(write,[question,question]))
        self.assertEqual(sum(r['created'] for r in result),1)
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(write,[self.question('one'),self.question('two')]))
        self.assertEqual({r['message']['position'] for r in result},{2,3})
        with self.engine.begin() as c:self.assertEqual(repo.conversation(c,owner,identity)['message_count'],3)

    def test_http_write_read_archive_flow_returns_no_model_answer(self):
        created=self.client.post('/api/v1/agent-chat/conversations',headers=self.ha,json={'client_request_id':str(uuid4()),'title':'合成HTTP会话'})
        self.assertEqual(created.status_code,200);url='/api/v1/agent-chat/conversations/'+created.json()['conversation']['conversation_id']
        body={'client_request_id':str(uuid4()),'content':'合成HTTP问题'}
        response=self.client.post(url+'/messages',headers=self.ha,json=body)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['answer_status'],'separate_history')
        self.assertFalse(self.client.post(url+'/messages',headers=self.ha,json=body).json()['created'])
        self.assertEqual(self.client.get(url+'/messages',headers=self.ha).json()['items'][0]['role'],'user')
        self.assertEqual(self.client.patch(url,headers=self.ha,json={'expected_revision':2,'archived':True}).status_code,200)
        self.assertEqual(self.client.get(url+'/messages',headers=self.ha).headers['cache-control'],'no-store')


if __name__=='__main__':unittest.main()

