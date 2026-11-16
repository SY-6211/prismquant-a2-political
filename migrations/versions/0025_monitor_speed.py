"""Persistent discovery controls, baseline/frontier, stage evidence and browser acknowledgements."""
from alembic import op
revision='0025_monitor_speed'
down_revision='0024_web_pilot'
branch_labels=None
depends_on=None
def upgrade():
 op.execute('''
 CREATE TABLE speed_sources(
   source text PRIMARY KEY CHECK(source IN ('ecb','trump','fed','eia','treasury')),
   enabled boolean NOT NULL DEFAULT false,interval_seconds integer NOT NULL CHECK(interval_seconds BETWEEN 60 AND 3600),
   enabled_at timestamptz,baseline_ready boolean NOT NULL DEFAULT false,last_attempt_at timestamptz,last_finished_at timestamptz,
   next_attempt_at timestamptz,failures integer NOT NULL DEFAULT 0,pause_reason text,last_result jsonb NOT NULL DEFAULT '{}');
 INSERT INTO speed_sources(source,interval_seconds) VALUES('ecb',60),('trump',300),('fed',60),('eia',300),('treasury',120);
 CREATE TABLE speed_candidates(
   source text NOT NULL REFERENCES speed_sources(source),url text NOT NULL,title text NOT NULL,
   first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),is_baseline boolean NOT NULL,
   status text NOT NULL CHECK(status IN ('baseline','pending','staged','saved','needs_review')),
   checked_at timestamptz,document_id uuid REFERENCES web_documents(document_id),note text,
   PRIMARY KEY(source,url));
 CREATE TABLE speed_rounds(
   run_id uuid PRIMARY KEY,source text NOT NULL REFERENCES speed_sources(source),
   started_at timestamptz NOT NULL,finished_at timestamptz NOT NULL,report jsonb NOT NULL,
   CHECK(finished_at>=started_at));
 CREATE TABLE speed_deliveries(
   version_id uuid PRIMARY KEY REFERENCES event_versions(version_id),source text NOT NULL REFERENCES speed_sources(source),
   detected_at timestamptz NOT NULL,commit_returned_at timestamptz NOT NULL,
   discovery_to_commit_ms double precision NOT NULL CHECK(discovery_to_commit_ms>=0 AND discovery_to_commit_ms<=1800000),
   is_catchup boolean NOT NULL,publication_time_verified boolean NOT NULL DEFAULT false CHECK(NOT publication_time_verified));
 CREATE TABLE speed_receipts(
   session_id uuid NOT NULL,version_id uuid NOT NULL REFERENCES speed_deliveries(version_id),
   received_at timestamptz NOT NULL DEFAULT clock_timestamp(),notice_render_ms double precision NOT NULL CHECK(notice_render_ms BETWEEN 0 AND 60000),
   evidence_kind text NOT NULL DEFAULT 'client_reported_notice' CHECK(evidence_kind='client_reported_notice'),
   PRIMARY KEY(session_id,version_id));
 CREATE INDEX ix_speed_rounds_recent ON speed_rounds(source,finished_at DESC);
 CREATE INDEX ix_speed_pending ON speed_candidates(source,status,first_seen_at DESC);
 CREATE INDEX ix_speed_receipts_recent ON speed_receipts(received_at);
 ''')
def downgrade():
 op.execute('DROP TABLE speed_receipts;DROP TABLE speed_deliveries;DROP TABLE speed_rounds;DROP TABLE speed_candidates;DROP TABLE speed_sources')
