"""Persist manual batch run summaries and bounded metadata, never post bodies."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0005_collection_runs'
down_revision = '0004_archive_post_details'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'collection_runs',
        sa.Column('run_id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('source', sa.Text(), nullable=False),
        # Snapshot identities intentionally have no FK: history survives config removal.
        sa.Column('source_id', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('person_id', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('mode', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='running'),
        sa.Column('batch_limit', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.Column('stop_reason', sa.Text()),
        sa.Column('report_complete', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('source_enabled', sa.Boolean()),
        sa.Column('monitoring_enabled', sa.Boolean()),
        sa.Column('link_enabled', sa.Boolean()),
        sa.Column('selection', pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('summary', pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("source IN ('ecb','trump')", name='ck_collection_runs_source'),
        sa.CheckConstraint("mode = 'manual_batch_import'", name='ck_collection_runs_mode'),
        sa.CheckConstraint("status IN ('running','completed','completed_with_issues','no_candidates','no_new_candidates','stopped','interrupted')", name='ck_collection_runs_status'),
        sa.CheckConstraint('batch_limit BETWEEN 1 AND 5', name='ck_collection_runs_limit'),
        sa.CheckConstraint("(status='running' AND finished_at IS NULL) OR (status<>'running' AND finished_at IS NOT NULL AND finished_at>=started_at)", name='ck_collection_runs_lifecycle'),
        sa.CheckConstraint("jsonb_typeof(selection)='object' AND jsonb_typeof(summary)='object'", name='ck_collection_runs_json'),
    )
    op.create_index('ix_collection_runs_started', 'collection_runs', ['started_at', 'run_id'])
    op.create_table(
        'collection_run_items',
        sa.Column('run_id', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('phase', sa.Text(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('url', sa.Text()),
        sa.Column('title', sa.Text()),
        sa.Column('record_id', pg.UUID(as_uuid=True)),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text()),
        sa.Column('write_outcome_unknown', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint('run_id', 'phase', 'position'),
        sa.ForeignKeyConstraint(['run_id'], ['collection_runs.run_id'], ondelete='CASCADE'),
        sa.CheckConstraint("phase IN ('precheck','detail')", name='ck_collection_run_items_phase'),
        sa.CheckConstraint('position>=0', name='ck_collection_run_items_position'),
        sa.CheckConstraint("status IN ('skipped_existing','skipped_unsupported','needs_review','inserted','already_exists','failed','interrupted','not_attempted')", name='ck_collection_run_items_status'),
    )


def downgrade():
    op.drop_table('collection_run_items')
    op.drop_index('ix_collection_runs_started', table_name='collection_runs')
    op.drop_table('collection_runs')
