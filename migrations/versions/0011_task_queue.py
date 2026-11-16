"""Durable bounded work queues, attempt history and explicit recovery notes."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0011_task_queue'
down_revision = '0010_event_model'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'collection_tasks',
        sa.Column('task_id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('source_id', pg.UUID(as_uuid=True), sa.ForeignKey('sources.source_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('lane', sa.Text(), nullable=False),
        sa.Column('idempotency_key', sa.String(64), nullable=False),
        sa.Column('payload', pg.JSONB(), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('failure_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('max_failures', sa.Integer(), nullable=False, server_default=sa.text('3')),
        sa.Column('lease_token', pg.UUID(as_uuid=True)),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True)),
        sa.Column('worker_id', pg.UUID(as_uuid=True)),
        sa.Column('last_code', sa.Text()),
        sa.Column('result', pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('source_id', 'kind', 'idempotency_key'),
        sa.CheckConstraint("(kind='collect_source' AND lane='raw') OR (kind='history_item' AND lane='history') "
                           "OR (kind='media_parse' AND lane='media') OR (kind='ai_analyze' AND lane='analysis')"),
        sa.CheckConstraint("status IN ('queued','running','retry','succeeded','dead','needs_review','cancelled')"),
        sa.CheckConstraint("jsonb_typeof(payload)='object' AND jsonb_typeof(result)='object'"),
        sa.CheckConstraint('priority BETWEEN 0 AND 100 AND attempt_count >= 0 AND failure_count >= 0 AND max_failures BETWEEN 1 AND 10'),
        sa.CheckConstraint("idempotency_key ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("(status='running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL AND worker_id IS NOT NULL) "
                           "OR (status<>'running' AND lease_token IS NULL AND lease_expires_at IS NULL AND worker_id IS NULL)"),
    )
    op.create_index('ix_tasks_claim', 'collection_tasks', ['lane', 'status', 'available_at', sa.text('priority DESC'), 'created_at'])
    op.create_index('uq_tasks_active_collect', 'collection_tasks', ['source_id'], unique=True,
                    postgresql_where=sa.text("kind='collect_source' AND status IN ('queued','running','retry')"))
    op.create_table(
        'task_attempts',
        sa.Column('attempt_id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', pg.UUID(as_uuid=True), sa.ForeignKey('collection_tasks.task_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('attempt_number', sa.Integer(), nullable=False),
        sa.Column('lease_token', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('worker_id', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('code', sa.Text()),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.Column('run_id', pg.UUID(as_uuid=True), sa.ForeignKey('collection_runs.run_id', ondelete='RESTRICT')),
        sa.UniqueConstraint('task_id', 'attempt_number'),
        sa.CheckConstraint('attempt_number > 0'),
        sa.CheckConstraint("status IN ('running','succeeded','deferred','retry','dead','needs_review','abandoned','cancelled')"),
        sa.CheckConstraint("(status='running' AND finished_at IS NULL) OR (status<>'running' AND finished_at IS NOT NULL)"),
    )
    op.create_table(
        'task_reviews',
        sa.Column('review_id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('task_id', pg.UUID(as_uuid=True), sa.ForeignKey('collection_tasks.task_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('length(btrim(note)) BETWEEN 1 AND 500'),
    )


def downgrade():
    # Explicit rollback discards task history, never performed by a worker.
    op.drop_table('task_reviews')
    op.drop_table('task_attempts')
    op.drop_table('collection_tasks')
