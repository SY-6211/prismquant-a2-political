"""Explicit finite web QA allowances and durable, at-most-once execution jobs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
revision='0020_web_qa'
down_revision='0019_source_catalog'
branch_labels=None
depends_on=None
def upgrade():
    op.create_table('agent_web_qa_grants',
        sa.Column('grant_id',pg.UUID(as_uuid=True),primary_key=True),sa.Column('approval',sa.String(120),nullable=False,unique=True),
        sa.Column('user_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_chat_users.user_id'),nullable=False),
        sa.Column('cap',sa.BigInteger(),nullable=False),sa.Column('max_requests',sa.Integer(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),sa.Column('closed_at',sa.DateTime(timezone=True)),
        sa.CheckConstraint('cap>0 AND cap<=10000000000 AND max_requests BETWEEN 1 AND 100'),sa.CheckConstraint('expires_at>created_at'))
    op.create_table('agent_web_qa_jobs',
        sa.Column('run_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_qa_runs.run_id'),primary_key=True),
        sa.Column('grant_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_web_qa_grants.grant_id'),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),sa.Column('reserved',sa.BigInteger(),nullable=False),sa.Column('actual',sa.BigInteger()),
        sa.Column('state',sa.Text(),nullable=False,server_default='queued'),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('started_at',sa.DateTime(timezone=True)),sa.Column('sent_at',sa.DateTime(timezone=True)),sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('queued','running','completed','abstained','rejected','failed','unknown')"),
        sa.CheckConstraint('reserved>0 AND (actual IS NULL OR actual>=0)'),sa.CheckConstraint("request_hash ~ '^[a-f0-9]{64}$'"))
    op.create_index('ix_web_qa_grant_jobs','agent_web_qa_jobs',['grant_id','created_at'])
def downgrade():
    op.drop_table('agent_web_qa_jobs');op.drop_table('agent_web_qa_grants')
