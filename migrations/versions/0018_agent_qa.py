"""Frozen private QA inputs, single execution claims and append-only answers."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision='0018_agent_qa'
down_revision='0017_agent_chat'
branch_labels=None
depends_on=None


def upgrade():
    op.create_unique_constraint('uq_chat_message_conversation_id','agent_chat_messages',['conversation_id','message_id'])
    op.create_table('agent_qa_runs',
        sa.Column('run_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('conversation_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('message_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('client_request_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),
        sa.Column('request_spec',pg.JSONB(),nullable=False),
        sa.Column('context',pg.JSONB(),nullable=False),
        sa.Column('context_hash',sa.String(64),nullable=False),
        sa.Column('model_id',sa.Text(),nullable=False),
        sa.Column('prompt_version',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['conversation_id','message_id'],['agent_chat_messages.conversation_id','agent_chat_messages.message_id'],ondelete='RESTRICT'),
        sa.UniqueConstraint('conversation_id','client_request_id'),
        sa.CheckConstraint("request_hash ~ '^[a-f0-9]{64}$' AND context_hash ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint("jsonb_typeof(context)='object' AND jsonb_typeof(request_spec)='object'"))
    op.create_index('ix_qa_conversation_created','agent_qa_runs',['conversation_id','created_at','run_id'])
    op.create_table('agent_qa_executions',
        sa.Column('run_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_qa_runs.run_id',ondelete='RESTRICT'),primary_key=True),
        sa.Column('request_hash',sa.String(64),nullable=False),
        sa.Column('claimed_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()))
    op.create_table('agent_qa_results',
        sa.Column('result_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('run_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_qa_runs.run_id',ondelete='RESTRICT'),nullable=False,unique=True),
        sa.Column('status',sa.Text(),nullable=False),
        sa.Column('execution_kind',sa.Text(),nullable=False),
        sa.Column('answer',pg.JSONB()),
        sa.Column('response_hash',sa.String(64),nullable=False),
        sa.Column('receipt',pg.JSONB(),nullable=False),
        sa.Column('failure_code',sa.String(80)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('completed','abstained','rejected','failed','unknown')"),
        sa.CheckConstraint("execution_kind IN ('openrouter','synthetic_fixture','local_abstention')"),
        sa.CheckConstraint("(status IN ('completed','abstained'))=(answer IS NOT NULL)"),
        sa.CheckConstraint("response_hash ~ '^[a-f0-9]{64}$'"))
    op.execute("""CREATE FUNCTION pq_qa_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'QA history is append-only'; END; $$""")
    for table in ('agent_qa_runs','agent_qa_executions','agent_qa_results'):
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION pq_qa_immutable()')


def downgrade():
    op.drop_table('agent_qa_results');op.drop_table('agent_qa_executions');op.drop_table('agent_qa_runs')
    op.execute('DROP FUNCTION pq_qa_immutable()')
    op.drop_constraint('uq_chat_message_conversation_id','agent_chat_messages',type_='unique')
