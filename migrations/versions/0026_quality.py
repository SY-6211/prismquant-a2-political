"""Private feedback and frozen, separately reviewed QA evaluation cases."""
from alembic import op
revision = '0026_quality'
down_revision = '0025_monitor_speed'
branch_labels = depends_on = None


def upgrade():
    op.execute('''CREATE TABLE agent_answer_feedback (
      feedback_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES agent_chat_users(user_id),
      run_id uuid NOT NULL REFERENCES agent_qa_runs(run_id), result_id uuid NOT NULL REFERENCES agent_qa_results(result_id),
      client_request_id uuid NOT NULL, kind text NOT NULL CHECK(kind IN
      ('useful','unsupported','wrong_person','time_error','missed_context','irrelevant','other')),
      note text NOT NULL CHECK(char_length(note) BETWEEN 1 AND 1000),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(user_id,client_request_id));
    CREATE INDEX agent_feedback_latest ON agent_answer_feedback(user_id,run_id,created_at DESC,feedback_id DESC);
    CREATE TABLE agent_quality_cases (
      case_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES agent_chat_users(user_id),
      run_id uuid NOT NULL REFERENCES agent_qa_runs(run_id), result_id uuid NOT NULL REFERENCES agent_qa_results(result_id),
      packet jsonb NOT NULL, packet_hash varchar(64) NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(user_id,run_id));
    CREATE TABLE agent_quality_reviews (
      review_id uuid PRIMARY KEY, case_id uuid NOT NULL REFERENCES agent_quality_cases(case_id),
      reviewer text NOT NULL, revision integer NOT NULL CHECK(revision>0), label jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(case_id,reviewer,revision));
    CREATE TRIGGER feedback_immutable BEFORE UPDATE OR DELETE ON agent_answer_feedback FOR EACH ROW EXECUTE FUNCTION pq_annotation_immutable();
    CREATE TRIGGER quality_cases_immutable BEFORE UPDATE OR DELETE ON agent_quality_cases FOR EACH ROW EXECUTE FUNCTION pq_annotation_immutable();
    CREATE TRIGGER quality_reviews_immutable BEFORE UPDATE OR DELETE ON agent_quality_reviews FOR EACH ROW EXECUTE FUNCTION pq_annotation_immutable();''')


def downgrade():
    for table in ('agent_quality_reviews','agent_quality_cases','agent_answer_feedback'):
        op.drop_table(table)
