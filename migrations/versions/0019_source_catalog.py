"""Candidate directory and immutable review history, separate from live sources."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
revision='0019_source_catalog'
down_revision='0018_agent_qa'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('source_catalog_imports',
        sa.Column('import_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('snapshot_hash',sa.String(64),nullable=False,unique=True),
        sa.Column('manifest',pg.JSONB(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.CheckConstraint("snapshot_hash ~ '^[a-f0-9]{64}$'"))
    op.create_table('source_catalog_candidates',
        sa.Column('candidate_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('import_id',pg.UUID(as_uuid=True),sa.ForeignKey('source_catalog_imports.import_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('number',sa.Integer(),nullable=False),
        sa.Column('name',sa.Text(),nullable=False),sa.Column('source_type',sa.Text(),nullable=False),
        sa.Column('original',pg.JSONB(),nullable=False),sa.Column('triage',pg.JSONB(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.UniqueConstraint('import_id','number'),sa.CheckConstraint('number>0'))
    op.create_table('source_catalog_reviews',
        sa.Column('review_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('candidate_id',pg.UUID(as_uuid=True),sa.ForeignKey('source_catalog_candidates.candidate_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('revision',sa.Integer(),nullable=False),sa.Column('review',pg.JSONB(),nullable=False),
        sa.Column('actor',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.UniqueConstraint('candidate_id','revision'),sa.CheckConstraint('revision>0'))
    op.create_index('ix_catalog_import_number','source_catalog_candidates',['import_id','number'])
    op.execute("CREATE FUNCTION pq_catalog_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Catalog imports and reviews are append-only'; END; $$")
    for table in ('source_catalog_imports','source_catalog_candidates','source_catalog_reviews'):
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION pq_catalog_immutable()')

def downgrade():
    for table in ('source_catalog_reviews','source_catalog_candidates','source_catalog_imports'):op.drop_table(table)
    op.execute('DROP FUNCTION pq_catalog_immutable()')
