"""Persistent bounded ECB index discovery, public-response cache and candidates."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0012_ecb_discovery'
down_revision = '0011_task_queue'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ecb_discovery_state',
        sa.Column('source_id',pg.UUID(as_uuid=True),sa.ForeignKey('sources.source_id',ondelete='RESTRICT'),primary_key=True),
        sa.Column('index_ready',sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column('rss_ready',sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column('index_base',sa.Text()),
        sa.Column('index_window',pg.JSONB(),nullable=False,server_default=sa.text("'[]'::jsonb")),
        sa.Column('coverage',pg.JSONB(),nullable=False,server_default=sa.text("'{}'::jsonb")),
        sa.Column('last_host_request_at',sa.DateTime(timezone=True)),
        sa.Column('host_delay_seconds',sa.Integer(),nullable=False,server_default=sa.text('5')),
        sa.Column('last_scan_at',sa.DateTime(timezone=True)),
        sa.Column('retry_not_before',sa.DateTime(timezone=True)),
        sa.Column('failure_count',sa.Integer(),nullable=False,server_default=sa.text('0')),
        sa.Column('pause_reason',sa.Text()),
        sa.Column('last_report',pg.JSONB(),nullable=False,server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint('host_delay_seconds BETWEEN 5 AND 60 AND failure_count>=0'),
        sa.CheckConstraint("jsonb_typeof(index_window)='array' AND jsonb_typeof(coverage)='object' AND jsonb_typeof(last_report)='object'"))
    op.create_table('ecb_http_cache',
        sa.Column('source_id',pg.UUID(as_uuid=True),sa.ForeignKey('sources.source_id',ondelete='RESTRICT'),primary_key=True),
        sa.Column('url',sa.Text(),primary_key=True),
        sa.Column('body',sa.LargeBinary(),nullable=False),
        sa.Column('etag',sa.Text()),sa.Column('last_modified',sa.Text()),
        sa.Column('checked_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('fresh_until',sa.DateTime(timezone=True),nullable=False),
        sa.Column('headers',pg.JSONB(),nullable=False),
        sa.CheckConstraint('octet_length(body)<=8388608'),
        sa.CheckConstraint("url LIKE 'https://www.ecb.europa.eu/%' AND jsonb_typeof(headers)='object'"))
    op.create_table('ecb_discovery_candidates',
        sa.Column('source_id',pg.UUID(as_uuid=True),sa.ForeignKey('sources.source_id',ondelete='RESTRICT'),primary_key=True),
        sa.Column('url',sa.Text(),primary_key=True),
        sa.Column('first_channel',sa.Text(),nullable=False),
        sa.Column('channel_metadata',pg.JSONB(),nullable=False),
        sa.Column('first_seen_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('last_seen_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('status',sa.Text(),nullable=False),
        sa.Column('record_id',pg.UUID(as_uuid=True),sa.ForeignKey('public_records.record_id',ondelete='RESTRICT')),
        sa.Column('last_reason',sa.Text()),
        sa.CheckConstraint("first_channel IN ('web_index','rss')"),
        sa.CheckConstraint("status IN ('baseline','pending','saved','needs_review')"),
        sa.CheckConstraint("jsonb_typeof(channel_metadata)='object'"))
    op.create_index('ix_ecb_candidates_pending','ecb_discovery_candidates',['source_id','status','first_seen_at'])


def downgrade():
    op.drop_table('ecb_discovery_candidates')
    op.drop_table('ecb_http_cache')
    op.drop_table('ecb_discovery_state')
