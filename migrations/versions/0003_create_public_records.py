"""Create the public content table only; do not fetch or import content."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0003_create_public_records"
down_revision = "0002_create_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_records",
        sa.Column("record_id", pg.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), server_default=sa.text("'unconfirmed'"), nullable=False),
        sa.Column("provenance_type", sa.Text(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("retrieved_url", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("page_date", sa.Date(), nullable=True),
        sa.Column("published_raw", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("speaker_evidence", sa.Text(), nullable=True),
        sa.Column("verification_status", sa.Text(), server_default=sa.text("'needs_review'"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("record_id", name="pk_public_records"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.source_id"], name="fk_public_records_source", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["person_id"], ["people.person_id"], name="fk_public_records_person", ondelete="RESTRICT"),
        sa.UniqueConstraint("source_id", "retrieved_url", name="uq_public_records_source_url"),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_public_records_title_nonblank"),
        sa.CheckConstraint("btrim(body_text) <> ''", name="ck_public_records_body_nonblank"),
        sa.CheckConstraint("language IS NULL OR btrim(language) <> ''", name="ck_public_records_language_nonblank"),
        sa.CheckConstraint("content_type IN ('public_statement', 'institution_announcement', 'media_quote', 'unconfirmed')", name="ck_public_records_content_type"),
        sa.CheckConstraint("provenance_type IN ('official', 'third_party_archive')", name="ck_public_records_provenance_type"),
        sa.CheckConstraint("verification_status IN ('verified_page_attribution', 'archive_only', 'media_quote_only', 'needs_review')", name="ck_public_records_verification_status"),
        sa.CheckConstraint("retrieved_url ~ '^https?://[^[:space:]]+$'", name="ck_public_records_retrieved_url_http"),
        sa.CheckConstraint("original_url IS NULL OR original_url ~ '^https?://[^[:space:]]+$'", name="ck_public_records_original_url_http"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_public_records_sha256_format"),
        sa.CheckConstraint(
            "verification_status <> 'verified_page_attribution' OR "
            "(person_id IS NOT NULL AND speaker_evidence IS NOT NULL AND btrim(speaker_evidence) <> '')",
            name="ck_public_records_verified_speaker_evidence",
        ),
    )
    op.create_index("ix_public_records_person_id", "public_records", ["person_id"])


def downgrade() -> None:
    # Explicit rollback deletes saved content. Never run automatically.
    op.drop_index("ix_public_records_person_id", table_name="public_records")
    op.drop_table("public_records")
