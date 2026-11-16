"""Add observational timing only; old events remain unmeasured."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0009_collection_timing'
down_revision = '0008_history_import'
branch_labels = None
depends_on = None


def upgrade():
    for table in ('collection_runs', 'collection_run_items'):
        op.add_column(table, sa.Column('timing', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
        op.create_check_constraint(f'ck_{table}_timing_object', table, "jsonb_typeof(timing)='object'")


def downgrade():
    for table in ('collection_run_items', 'collection_runs'):
        op.drop_constraint(f'ck_{table}_timing_object', table, type_='check')
        op.drop_column(table, 'timing')
