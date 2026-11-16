"""Commit-ordered, bounded content notification log (no historical replay seed)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0013_content_stream'
down_revision = '0012_ecb_discovery'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('content_stream_clock',
        sa.Column('singleton', sa.Boolean(), primary_key=True, server_default=sa.true()),
        sa.Column('epoch', pg.UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('position', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
        sa.CheckConstraint('singleton AND position >= 0'))
    op.execute('INSERT INTO content_stream_clock (singleton) VALUES (true)')
    op.create_table('content_changes',
        sa.Column('position', sa.BigInteger(), primary_key=True),
        sa.Column('event_id', pg.UUID(as_uuid=True), sa.ForeignKey('monitor_events.event_id'), nullable=False),
        sa.Column('version_id', pg.UUID(as_uuid=True), sa.ForeignKey('event_versions.version_id'), nullable=False),
        sa.Column('record_id', pg.UUID(as_uuid=True), sa.ForeignKey('public_records.record_id')),
        sa.Column('person_id', pg.UUID(as_uuid=True), sa.ForeignKey('people.person_id')),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('review_status', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.CheckConstraint('position > 0'),
        sa.CheckConstraint("kind IN ('new','revision') AND review_status IN ('accepted','needs_review')"))


def downgrade():
    op.drop_table('content_changes')
    op.drop_table('content_stream_clock')
