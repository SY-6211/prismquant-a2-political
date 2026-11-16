"""Append-only grounded analysis inputs/results; never change source content."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision='0016_agent_analysis'
down_revision='0015_annotations'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('agent_analysis_runs',
        sa.Column('run_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('request_id',sa.String(100),nullable=False,unique=True),
        sa.Column('event_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('version_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('person_id',pg.UUID(as_uuid=True),sa.ForeignKey('people.person_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('model_id',sa.Text(),nullable=False),
        sa.Column('prompt_version',sa.Text(),nullable=False),
        sa.Column('context',pg.JSONB(),nullable=False),
        sa.Column('context_hash',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.ForeignKeyConstraint(['event_id','version_id'],['event_versions.event_id','event_versions.version_id'],ondelete='RESTRICT'),
        sa.CheckConstraint("jsonb_typeof(context)='object'"),
        sa.CheckConstraint("context_hash ~ '^[a-f0-9]{64}$'"))
    op.create_index('ix_agent_runs_person_created','agent_analysis_runs',['person_id','created_at'])
    op.create_table('agent_analysis_results',
        sa.Column('result_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('run_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_analysis_runs.run_id',ondelete='RESTRICT'),nullable=False,unique=True),
        sa.Column('execution_kind',sa.Text(),nullable=False),
        sa.Column('status',sa.Text(),nullable=False),
        sa.Column('analysis',pg.JSONB()),
        sa.Column('failure_code',sa.String(80)),
        sa.Column('response_hash',sa.String(64),nullable=False),
        sa.Column('receipt',pg.JSONB(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.CheckConstraint("execution_kind IN ('openrouter','provided_unverified','synthetic_fixture')"),
        sa.CheckConstraint("status IN ('completed','rejected','failed','unknown')"),
        sa.CheckConstraint("(status='completed' AND analysis IS NOT NULL) OR (status<>'completed' AND analysis IS NULL)"),
        sa.CheckConstraint("response_hash ~ '^[a-f0-9]{64}$'"))
    op.execute("""CREATE FUNCTION pq_agent_analysis_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Agent analysis history is append-only'; END; $$""")
    for table in ('agent_analysis_runs','agent_analysis_results'):
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} '
                   'FOR EACH ROW EXECUTE FUNCTION pq_agent_analysis_immutable()')


def downgrade():
    op.drop_table('agent_analysis_results');op.drop_table('agent_analysis_runs')
    op.execute('DROP FUNCTION pq_agent_analysis_immutable()')
