"""Local dense-vector collections, immutable chunks and atomic version indexing."""
from alembic import op

revision='0023_knowledge'
down_revision='0022_reminders'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('''
    CREATE TABLE kb_profiles (
      profile_id varchar(64) PRIMARY KEY, definition jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CHECK(profile_id ~ '^[a-f0-9]{64}$'));
    CREATE TABLE kb_documents (
      profile_id varchar(64) NOT NULL REFERENCES kb_profiles(profile_id),
      version_id uuid NOT NULL,event_id uuid NOT NULL,person_id uuid NOT NULL REFERENCES people(person_id),
      source_id uuid NOT NULL REFERENCES sources(source_id),snapshot_hash varchar(64) NOT NULL,
      body_sha256 varchar(64) NOT NULL, state text NOT NULL DEFAULT 'queued',
      chunk_count integer NOT NULL DEFAULT 0 CHECK(chunk_count>=0),
      lease_token uuid, claimed_at timestamptz, failure_code text,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),indexed_at timestamptz,
      PRIMARY KEY(profile_id,version_id),FOREIGN KEY(event_id,version_id) REFERENCES event_versions(event_id,version_id),
      CHECK(state IN ('queued','indexing','ready','skipped','failed')),
      CHECK(snapshot_hash ~ '^[a-f0-9]{64}$' AND body_sha256 ~ '^[a-f0-9]{64}$'));
    CREATE FUNCTION pq_kb_unit_vector(v real[]) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$
      SELECT coalesce(array_ndims(v)=1 AND array_lower(v,1)=1 AND array_length(v,1)=384
        AND NOT EXISTS (SELECT 1 FROM unnest(v) x WHERE x IS NULL OR x IN ('NaN'::real,'Infinity'::real,'-Infinity'::real))
        AND (SELECT sum(x::double precision*x::double precision) FROM unnest(v) x) BETWEEN 0.998001 AND 1.002001,false)
    $$;
    CREATE TABLE kb_chunks (
      chunk_id uuid PRIMARY KEY,profile_id varchar(64) NOT NULL,version_id uuid NOT NULL,
      start_offset integer NOT NULL CHECK(start_offset>=0),end_offset integer NOT NULL,
      text text NOT NULL,token_count integer NOT NULL CHECK(token_count BETWEEN 1 AND 384),
      text_sha256 varchar(64) NOT NULL,embedding real[] NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(profile_id,version_id) REFERENCES kb_documents(profile_id,version_id),
      UNIQUE(profile_id,version_id,start_offset,end_offset),
      CHECK(end_offset>start_offset AND char_length(text)=end_offset-start_offset),
      CHECK(text_sha256 ~ '^[a-f0-9]{64}$'),CHECK(pq_kb_unit_vector(embedding)));
    CREATE INDEX kb_docs_scope ON kb_documents(profile_id,person_id,state);
    CREATE INDEX kb_chunk_text ON kb_chunks(profile_id,text_sha256);
    CREATE TABLE kb_state (
      singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),enabled boolean NOT NULL DEFAULT false,
      profile_id varchar(64) REFERENCES kb_profiles(profile_id), checked_at timestamptz,pause_reason text);
    INSERT INTO kb_state(singleton) VALUES(true);
    CREATE FUNCTION pq_kb_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      RAISE EXCEPTION 'Knowledge model profiles and chunks are immutable'; END; $$;
    CREATE TRIGGER kb_profiles_immutable BEFORE UPDATE OR DELETE ON kb_profiles
      FOR EACH ROW EXECUTE FUNCTION pq_kb_immutable();
    CREATE TRIGGER kb_chunks_immutable BEFORE UPDATE OR DELETE ON kb_chunks
      FOR EACH ROW EXECUTE FUNCTION pq_kb_immutable();
    ''')

def downgrade():
    for name in ('kb_state','kb_chunks','kb_documents','kb_profiles'):op.drop_table(name)
    op.execute('DROP FUNCTION pq_kb_immutable();DROP FUNCTION pq_kb_unit_vector(real[])')
