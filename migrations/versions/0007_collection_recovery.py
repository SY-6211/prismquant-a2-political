from alembic import op
import sqlalchemy as sa

revision = '0007_collection_recovery'
down_revision = '0006_collection_schedule'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('collection_schedule', sa.Column('failure_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('collection_schedule', sa.Column('retry_not_before', sa.DateTime(timezone=True)))
    op.add_column('collection_schedule', sa.Column('pause_reason', sa.Text()))
    op.create_check_constraint('ck_schedule_failures', 'collection_schedule', 'failure_count>=0')
    op.add_column('collection_runs', sa.Column('recovered_at', sa.DateTime(timezone=True)))
    op.add_column('collection_runs', sa.Column('recovery_note', sa.Text()))


def downgrade():
    op.drop_column('collection_runs', 'recovery_note')
    op.drop_column('collection_runs', 'recovered_at')
    op.drop_constraint('ck_schedule_failures', 'collection_schedule', type_='check')
    for name in ('pause_reason', 'retry_not_before', 'failure_count'):
        op.drop_column('collection_schedule', name)
