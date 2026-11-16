"""Sealed corpus, reviewer label history and immutable keyword baseline runs."""
from alembic import op
revision='0015_annotations'
down_revision='0014_keyword_rules'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE annotation_datasets (
      dataset_id uuid PRIMARY KEY,name text NOT NULL,state text NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','sealed')),
      manifest jsonb,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
    CREATE TABLE annotation_samples (
      sample_id uuid PRIMARY KEY,dataset_id uuid NOT NULL REFERENCES annotation_datasets(dataset_id),
      identity text NOT NULL,origin text NOT NULL CHECK(origin IN ('source','synthetic')),snapshot jsonb NOT NULL,
      event_key text,group_hint text,group_id text,split text CHECK(split IN ('train','dev','blind')),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(dataset_id,identity));
    CREATE INDEX annotation_samples_page ON annotation_samples(dataset_id,split,sample_id);
    CREATE TABLE annotation_labels (
      label_id uuid PRIMARY KEY,sample_id uuid NOT NULL REFERENCES annotation_samples(sample_id),reviewer text NOT NULL,
      revision integer NOT NULL CHECK(revision>0),kind text NOT NULL CHECK(kind IN ('label','adjudication')),
      provenance text NOT NULL CHECK(provenance IN ('human_verified','synthetic_fixture')),
      label jsonb NOT NULL,basis_digest text,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(sample_id,reviewer,revision));
    CREATE INDEX annotation_labels_latest ON annotation_labels(sample_id,reviewer,revision DESC);
    CREATE TABLE annotation_runs (
      run_id uuid PRIMARY KEY,dataset_id uuid NOT NULL REFERENCES annotation_datasets(dataset_id),
      split text NOT NULL CHECK(split IN ('train','dev')),report jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp());
    CREATE FUNCTION pq_annotation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      RAISE EXCEPTION 'Annotation history is append-only'; END; $$;
    CREATE TRIGGER annotation_labels_immutable BEFORE UPDATE OR DELETE ON annotation_labels FOR EACH ROW EXECUTE FUNCTION pq_annotation_immutable();
    CREATE TRIGGER annotation_runs_immutable BEFORE UPDATE OR DELETE ON annotation_runs FOR EACH ROW EXECUTE FUNCTION pq_annotation_immutable();
    CREATE FUNCTION pq_annotation_corpus_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF TG_TABLE_NAME='annotation_datasets' THEN
        IF TG_OP='DELETE' OR OLD.state='sealed' OR NEW.dataset_id<>OLD.dataset_id OR NEW.name<>OLD.name OR NEW.created_at<>OLD.created_at
          OR NEW.state<>'sealed' THEN RAISE EXCEPTION 'Dataset is immutable except sealing'; END IF;
      ELSE
        IF TG_OP='DELETE' OR (SELECT state FROM annotation_datasets WHERE dataset_id=COALESCE(NEW.dataset_id,OLD.dataset_id))<>'draft'
          THEN RAISE EXCEPTION 'Corpus is sealed'; END IF;
        IF TG_OP='UPDATE' AND (to_jsonb(NEW)-'group_id'-'split') IS DISTINCT FROM (to_jsonb(OLD)-'group_id'-'split')
          THEN RAISE EXCEPTION 'Sample content is immutable'; END IF;
      END IF;
      RETURN NEW;
    END; $$;
    CREATE TRIGGER annotation_dataset_guard BEFORE UPDATE OR DELETE ON annotation_datasets FOR EACH ROW EXECUTE FUNCTION pq_annotation_corpus_guard();
    CREATE TRIGGER annotation_sample_guard BEFORE INSERT OR UPDATE OR DELETE ON annotation_samples FOR EACH ROW EXECUTE FUNCTION pq_annotation_corpus_guard();''')


def downgrade():
    for table in ('annotation_runs','annotation_labels','annotation_samples','annotation_datasets'):op.drop_table(table)
    op.execute('DROP FUNCTION pq_annotation_corpus_guard(); DROP FUNCTION pq_annotation_immutable()')
