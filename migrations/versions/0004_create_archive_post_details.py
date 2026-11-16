"""Create the empty archive detail table; do not read or import posts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0004_archive_post_details"
down_revision = "0003_create_public_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "archive_post_details",
        sa.Column("record_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("archive_author", sa.Text(), nullable=False),
        sa.Column("archive_handle", sa.Text(), nullable=False),
        sa.Column("archive_post_type", sa.Text(), nullable=False),
        sa.Column("archive_removed_raw", sa.Text(), nullable=True),
        sa.Column("title_origin", sa.Text(), server_default=sa.text("'archive_rss'"), nullable=False),
        sa.Column("page_post_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("page_post_published_raw", sa.Text(), nullable=True),
        sa.Column("archive_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_capture_raw", sa.Text(), nullable=True),
        sa.Column("image_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("video_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("inline_quotes", pg.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("quoted_posts", pg.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("archive_media_notes", pg.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("record_id", name="pk_archive_post_details"),
        sa.ForeignKeyConstraint(
            ["record_id"], ["public_records.record_id"],
            name="fk_archive_post_details_record", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("btrim(archive_author) <> ''", name="ck_archive_post_details_author_nonblank"),
        sa.CheckConstraint("btrim(archive_handle) <> ''", name="ck_archive_post_details_handle_nonblank"),
        sa.CheckConstraint("btrim(parser_version) <> ''", name="ck_archive_post_details_parser_nonblank"),
        sa.CheckConstraint("archive_post_type IN ('original', 'quote', 'retruth')", name="ck_archive_post_details_post_type"),
        sa.CheckConstraint("title_origin = 'archive_rss'", name="ck_archive_post_details_title_origin"),
        sa.CheckConstraint("image_count >= 0", name="ck_archive_post_details_images_nonnegative"),
        sa.CheckConstraint("video_count >= 0", name="ck_archive_post_details_videos_nonnegative"),
        sa.CheckConstraint("jsonb_typeof(inline_quotes) = 'array'", name="ck_archive_post_details_inline_array"),
        sa.CheckConstraint("jsonb_typeof(quoted_posts) = 'array'", name="ck_archive_post_details_quoted_array"),
        sa.CheckConstraint("jsonb_typeof(archive_media_notes) = 'array'", name="ck_archive_post_details_media_array"),
        sa.CheckConstraint("snapshot_hash ~ '^[0-9a-f]{64}$'", name="ck_archive_post_details_sha256_format"),
    )


def downgrade() -> None:
    # Explicit rollback drops archive detail snapshots. Never run automatically.
    op.drop_table("archive_post_details")
