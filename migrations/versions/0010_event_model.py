"""Add source channels, native events and append-only content versions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0010_event_model'
down_revision = '0009_collection_timing'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'source_channels',
        sa.Column('channel_id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('source_id', pg.UUID(as_uuid=True), sa.ForeignKey('sources.source_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('transport', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('source_id', 'transport'),
        sa.CheckConstraint("transport IN ('rss', 'web_index', 'official_api', 'licensed_stream', 'manual_import')"),
    )
    op.create_table(
        'monitor_events',
        sa.Column('event_id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('platform', sa.Text(), nullable=False),
        sa.Column('native_id', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('platform', 'native_id'),
        sa.CheckConstraint("platform IN ('ecb', 'truth_social', 'source_record')"),
        sa.CheckConstraint("btrim(native_id) <> ''"),
    )
    op.create_table(
        'event_versions',
        sa.Column('version_id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('event_id', pg.UUID(as_uuid=True), sa.ForeignKey('monitor_events.event_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('channel_id', pg.UUID(as_uuid=True), sa.ForeignKey('source_channels.channel_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('person_id', pg.UUID(as_uuid=True), sa.ForeignKey('people.person_id', ondelete='RESTRICT')),
        sa.Column('provenance_type', sa.Text(), nullable=False),
        sa.Column('verification_status', sa.Text(), nullable=False),
        sa.Column('review_status', sa.Text(), nullable=False),
        sa.Column('payload', pg.JSONB(), nullable=False),
        sa.Column('snapshot_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('event_id', 'version_number'),
        sa.UniqueConstraint('event_id', 'channel_id', 'snapshot_hash'),
        sa.UniqueConstraint('event_id', 'version_id'),
        sa.CheckConstraint('version_number > 0'),
        sa.CheckConstraint("provenance_type IN ('official', 'third_party_archive')"),
        sa.CheckConstraint("verification_status IN ('verified_page_attribution', 'archive_only', 'media_quote_only', 'needs_review')"),
        sa.CheckConstraint("review_status IN ('accepted', 'needs_review')"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'"),
        sa.CheckConstraint("snapshot_hash ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("verification_status <> 'verified_page_attribution' OR person_id IS NOT NULL"),
    )
    op.create_index('ix_event_versions_person', 'event_versions', ['person_id'])
    op.create_table(
        'event_observations',
        sa.Column('observation_id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('event_id', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('version_id', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('observation_kind', sa.Text(), nullable=False),
        sa.Column('metadata', pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(['event_id', 'version_id'], ['event_versions.event_id', 'event_versions.version_id'], ondelete='RESTRICT'),
        sa.CheckConstraint("observation_kind IN ('live_fetch', 'legacy_projection')"),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'"),
    )
    op.create_index('ix_event_observations_latest', 'event_observations', ['event_id', sa.text('observed_at DESC'), sa.text('observation_id DESC')])
    op.create_table(
        'event_record_links',
        sa.Column('record_id', pg.UUID(as_uuid=True), sa.ForeignKey('public_records.record_id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('event_id', pg.UUID(as_uuid=True), nullable=False),
        sa.Column('version_id', pg.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(['event_id', 'version_id'], ['event_versions.event_id', 'event_versions.version_id'], ondelete='RESTRICT'),
    )
    # Versions cannot be accidentally overwritten by a future updater.
    op.execute("""CREATE FUNCTION pq_event_versions_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'Event versions are append-only'; END; $$""")
    op.execute("""CREATE TRIGGER event_versions_immutable BEFORE UPDATE OR DELETE ON event_versions
        FOR EACH ROW EXECUTE FUNCTION pq_event_versions_immutable()""")


def downgrade():
    # Explicit rollback destroys the new event history; never called automatically.
    op.drop_table('event_record_links')
    op.drop_table('event_observations')
    op.drop_table('event_versions')
    op.execute('DROP FUNCTION pq_event_versions_immutable()')
    op.drop_table('monitor_events')
    op.drop_table('source_channels')
