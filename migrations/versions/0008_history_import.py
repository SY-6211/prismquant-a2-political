"""Identify manual history runs and archive listing metadata accurately."""
from alembic import op

revision = '0008_history_import'
down_revision = '0007_collection_recovery'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('ck_archive_post_details_title_origin', 'archive_post_details', type_='check')
    op.create_check_constraint('ck_archive_post_details_title_origin', 'archive_post_details', "title_origin IN ('archive_rss','archive_listing')")
    op.drop_constraint('ck_collection_runs_mode', 'collection_runs', type_='check')
    op.create_check_constraint('ck_collection_runs_mode', 'collection_runs', "mode IN ('manual_batch_import','automatic_batch_import','manual_history_import')")


def downgrade():
    # Refuse a lossy downgrade rather than relabel historical evidence.
    op.drop_constraint('ck_archive_post_details_title_origin', 'archive_post_details', type_='check')
    op.create_check_constraint('ck_archive_post_details_title_origin', 'archive_post_details', "title_origin = 'archive_rss'")
    op.drop_constraint('ck_collection_runs_mode', 'collection_runs', type_='check')
    op.create_check_constraint('ck_collection_runs_mode', 'collection_runs', "mode IN ('manual_batch_import','automatic_batch_import')")
