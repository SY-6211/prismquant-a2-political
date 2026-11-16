"""Create source configuration and person/source relationships without seed data."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0002_create_sources"
down_revision = "0001_create_people"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("source_id", pg.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("publisher_name", sa.Text(), nullable=False),
        sa.Column("provenance_type", sa.Text(), nullable=False),
        sa.Column("fetch_method", sa.Text(), server_default=sa.text("'rss'"), nullable=False),
        sa.Column("feed_url", sa.Text(), nullable=False),
        sa.Column("homepage_url", sa.Text(), nullable=True),
        sa.Column("poll_interval_seconds", sa.Integer(), server_default=sa.text("300"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("source_id", name="pk_sources"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_sources_name_nonblank"),
        sa.CheckConstraint("btrim(publisher_name) <> ''", name="ck_sources_publisher_nonblank"),
        sa.CheckConstraint("provenance_type IN ('official', 'third_party_archive')", name="ck_sources_provenance_type"),
        sa.CheckConstraint("fetch_method = 'rss'", name="ck_sources_fetch_method"),
        sa.CheckConstraint("feed_url ~ '^https?://[^[:space:]]+$'", name="ck_sources_feed_url_http"),
        sa.CheckConstraint("homepage_url IS NULL OR homepage_url ~ '^https?://[^[:space:]]+$'", name="ck_sources_homepage_url_http"),
        sa.CheckConstraint("poll_interval_seconds > 0", name="ck_sources_interval_positive"),
    )
    op.execute("""
        CREATE FUNCTION pq_sources_touch_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = clock_timestamp();
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER sources_touch_updated_at
        BEFORE UPDATE ON sources
        FOR EACH ROW EXECUTE FUNCTION pq_sources_touch_updated_at()
    """)
    op.create_table(
        "person_sources",
        sa.Column("person_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint("person_id", "source_id", name="pk_person_sources"),
        sa.ForeignKeyConstraint(["person_id"], ["people.person_id"], name="fk_person_sources_person", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.source_id"], name="fk_person_sources_source", ondelete="RESTRICT"),
    )
    op.create_index("ix_person_sources_source_id", "person_sources", ["source_id"])


def downgrade() -> None:
    # Explicit rollback deletes source settings and links, but never people.
    op.drop_index("ix_person_sources_source_id", table_name="person_sources")
    op.drop_table("person_sources")
    op.drop_table("sources")
    op.execute("DROP FUNCTION pq_sources_touch_updated_at()")
