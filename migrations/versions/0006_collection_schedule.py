"""Allow automatic run history and persist per-source scheduling timestamps."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0006_collection_schedule'
down_revision = '0005_collection_runs'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('ck_collection_runs_mode', 'collection_runs', type_='check')
    op.create_check_constraint('ck_collection_runs_mode', 'collection_runs', "mode IN ('manual_batch_import','automatic_batch_import')")
    op.create_table(
        'collection_schedule',
        sa.Column('source_id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_finished_at', sa.DateTime(timezone=True)),
        sa.Column('last_run_id', pg.UUID(as_uuid=True)),
        sa.CheckConstraint("source IN ('ecb','trump')", name='ck_collection_schedule_source'),
        sa.CheckConstraint('last_finished_at IS NULL OR last_finished_at>=last_attempt_at', name='ck_collection_schedule_time'),
    )


def downgrade():
    # Refuse rather than relabel or delete any automatic history on rollback.
    op.create_check_constraint('ck_collection_runs_manual_rollback', 'collection_runs', "mode='manual_batch_import'")
    op.drop_table('collection_schedule')
    op.drop_constraint('ck_collection_runs_mode', 'collection_runs', type_='check')
    op.drop_constraint('ck_collection_runs_manual_rollback', 'collection_runs', type_='check')
    op.create_check_constraint('ck_collection_runs_mode', 'collection_runs', "mode='manual_batch_import'")
