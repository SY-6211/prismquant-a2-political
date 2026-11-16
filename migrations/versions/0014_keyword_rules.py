"""Immutable keyword drafts, preview receipts and publication audit."""
from alembic import op

revision='0014_keyword_rules'
down_revision='0013_content_stream'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE keyword_policies (
        rule_id uuid PRIMARY KEY, revision integer NOT NULL CHECK(revision>0),
        published_version uuid, created_at timestamptz NOT NULL DEFAULT clock_timestamp());
        CREATE TABLE keyword_versions (
        version_id uuid PRIMARY KEY, rule_id uuid NOT NULL REFERENCES keyword_policies(rule_id),
        number integer NOT NULL CHECK(number>0), definition jsonb NOT NULL,
        digest varchar(64) NOT NULL, note text NOT NULL, actor text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(rule_id,number), UNIQUE(rule_id,version_id));
        ALTER TABLE keyword_policies ADD CONSTRAINT keyword_published_owner
        FOREIGN KEY(rule_id,published_version) REFERENCES keyword_versions(rule_id,version_id);
        CREATE TABLE keyword_previews (
        preview_id uuid PRIMARY KEY, version_id uuid NOT NULL REFERENCES keyword_versions(version_id),
        engine_version text NOT NULL, report jsonb NOT NULL, actor text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp());
        CREATE INDEX keyword_preview_version ON keyword_previews(version_id,created_at DESC);
        CREATE TABLE keyword_publications (
        publication_id uuid PRIMARY KEY, rule_id uuid NOT NULL, version_id uuid NOT NULL,
        preview_id uuid NOT NULL REFERENCES keyword_previews(preview_id), actor text NOT NULL, note text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY(rule_id,version_id) REFERENCES keyword_versions(rule_id,version_id));
        CREATE FUNCTION pq_keyword_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'Keyword history is append-only'; END; $$;
        CREATE TRIGGER keyword_versions_immutable BEFORE UPDATE OR DELETE ON keyword_versions
        FOR EACH ROW EXECUTE FUNCTION pq_keyword_immutable();
        CREATE TRIGGER keyword_previews_immutable BEFORE UPDATE OR DELETE ON keyword_previews
        FOR EACH ROW EXECUTE FUNCTION pq_keyword_immutable();
        CREATE TRIGGER keyword_publications_immutable BEFORE UPDATE OR DELETE ON keyword_publications
        FOR EACH ROW EXECUTE FUNCTION pq_keyword_immutable();''')


def downgrade():
    op.execute('ALTER TABLE keyword_policies DROP CONSTRAINT keyword_published_owner')
    for table in ('keyword_publications','keyword_previews','keyword_versions','keyword_policies'):op.drop_table(table)
    op.execute('DROP FUNCTION pq_keyword_immutable()')
