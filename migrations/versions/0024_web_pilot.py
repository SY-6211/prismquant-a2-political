"""Finite web extraction audit; no automatic collection or model spending."""
from alembic import op
revision='0024_web_pilot'
down_revision='0023_knowledge'
branch_labels=None
depends_on=None
def upgrade():
    op.execute("ALTER TABLE sources DROP CONSTRAINT ck_sources_fetch_method")
    op.execute("ALTER TABLE sources ADD CONSTRAINT ck_sources_fetch_method CHECK(fetch_method IN ('rss','web_index'))")
    op.execute('''
    CREATE TABLE web_documents(
      document_id uuid PRIMARY KEY,source_key text NOT NULL,url text NOT NULL,
      title text NOT NULL,body_sha256 varchar(64) NOT NULL,html_sha256 varchar(64) NOT NULL,
      document_hash varchar(64) NOT NULL,extraction jsonb NOT NULL,
      first_observed_at timestamptz NOT NULL,
      UNIQUE(source_key,url,document_hash),
      CHECK(source_key IN ('fed','eia','treasury')),
      CHECK(body_sha256 ~ '^[a-f0-9]{64}$' AND html_sha256 ~ '^[a-f0-9]{64}$' AND document_hash ~ '^[a-f0-9]{64}$'),
      CHECK(char_length(extraction->>'body_text') BETWEEN 160 AND 100000));
    CREATE TABLE web_pilot_runs(
      run_id uuid PRIMARY KEY,source_key text NOT NULL,started_at timestamptz NOT NULL,
      finished_at timestamptz NOT NULL,status text NOT NULL,report jsonb NOT NULL,
      CHECK(status IN ('completed','completed_with_issues','failed')),
      CHECK(finished_at>=started_at));
    CREATE TABLE web_document_links(
      document_id uuid PRIMARY KEY REFERENCES web_documents(document_id),
      record_id uuid NOT NULL REFERENCES public_records(record_id),
      event_id uuid NOT NULL,version_id uuid NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(event_id,version_id) REFERENCES event_versions(event_id,version_id));
    CREATE INDEX ix_web_documents_recent ON web_documents(first_observed_at DESC,document_id);
    CREATE INDEX ix_web_pilot_runs_recent ON web_pilot_runs(finished_at DESC,run_id);
    CREATE FUNCTION pq_web_pilot_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Web extraction and pilot audit are append-only'; END; $$;
    CREATE TRIGGER web_documents_immutable BEFORE UPDATE OR DELETE ON web_documents
      FOR EACH ROW EXECUTE FUNCTION pq_web_pilot_immutable();
    CREATE TRIGGER web_pilot_runs_immutable BEFORE UPDATE OR DELETE ON web_pilot_runs
      FOR EACH ROW EXECUTE FUNCTION pq_web_pilot_immutable();
    CREATE TRIGGER web_document_links_immutable BEFORE UPDATE OR DELETE ON web_document_links
      FOR EACH ROW EXECUTE FUNCTION pq_web_pilot_immutable();
    ''')
def downgrade():
    # Refuse a downgrade that would mislabel an existing HTML source as RSS.
    op.execute("DO $$ BEGIN IF EXISTS(SELECT 1 FROM sources WHERE fetch_method='web_index') THEN RAISE EXCEPTION 'Remove HTML source dependencies explicitly before downgrade'; END IF; END $$")
    op.execute('DROP TABLE web_document_links;DROP TABLE web_pilot_runs;DROP TABLE web_documents;DROP FUNCTION pq_web_pilot_immutable()')
    op.execute('ALTER TABLE sources DROP CONSTRAINT ck_sources_fetch_method')
    op.execute("ALTER TABLE sources ADD CONSTRAINT ck_sources_fetch_method CHECK(fetch_method='rss')")
