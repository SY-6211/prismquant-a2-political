"""Finite automatic analysis policies and a durable, deduplicated work queue."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision='0021_auto_analysis'
down_revision='0020_web_qa'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('agent_auto_grants',
        sa.Column('grant_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('approval',sa.String(120),nullable=False,unique=True),
        sa.Column('person_id',pg.UUID(as_uuid=True),sa.ForeignKey('people.person_id'),nullable=False),
        sa.Column('cap',sa.BigInteger(),nullable=False),sa.Column('max_requests',sa.Integer(),nullable=False),
        sa.Column('epoch',pg.UUID(as_uuid=True),nullable=False),sa.Column('cursor_position',sa.BigInteger(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),sa.Column('closed_at',sa.DateTime(timezone=True)),
        sa.Column('pause_reason',sa.String(80)),
        sa.CheckConstraint('cap>0 AND cap<=10000000000 AND max_requests BETWEEN 1 AND 100'),
        sa.CheckConstraint('cursor_position>=0 AND expires_at>created_at'))
    op.create_table('agent_auto_jobs',
        sa.Column('job_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('grant_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_auto_grants.grant_id'),nullable=False),
        sa.Column('event_id',pg.UUID(as_uuid=True),nullable=False),sa.Column('version_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('person_id',pg.UUID(as_uuid=True),sa.ForeignKey('people.person_id'),nullable=False),
        sa.Column('model_id',sa.Text(),nullable=False),sa.Column('prompt_version',sa.Text(),nullable=False),
        sa.Column('state',sa.Text(),nullable=False,server_default='queued'),
        sa.Column('run_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_analysis_runs.run_id')),
        sa.Column('reserved',sa.BigInteger()),sa.Column('actual',sa.BigInteger()),sa.Column('request_hash',sa.String(64)),
        sa.Column('failure_code',sa.String(80)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('started_at',sa.DateTime(timezone=True)),sa.Column('sent_at',sa.DateTime(timezone=True)),
        sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(['event_id','version_id'],['event_versions.event_id','event_versions.version_id']),
        sa.UniqueConstraint('version_id','model_id','prompt_version'),
        sa.CheckConstraint("state IN ('queued','preparing','prepared','running','completed','cached','skipped','rejected','failed','unknown')"),
        sa.CheckConstraint('(reserved IS NULL OR reserved>0) AND (actual IS NULL OR actual>=0)'))
    op.create_index('ix_auto_jobs_state_created','agent_auto_jobs',['state','created_at'])

def downgrade():
    op.drop_table('agent_auto_jobs');op.drop_table('agent_auto_grants')
