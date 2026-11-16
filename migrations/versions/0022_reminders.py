"""Versioned keyword hints, cooldown grouping and auditable corrections."""
from alembic import op

revision='0022_reminders'
down_revision='0021_auto_analysis'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('''
    CREATE TABLE reminder_state (
      singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), enabled boolean NOT NULL DEFAULT false,
      epoch uuid, cursor_position bigint NOT NULL DEFAULT 0 CHECK(cursor_position>=0),
      checked_at timestamptz, pause_reason text, stats jsonb NOT NULL DEFAULT '{}');
    INSERT INTO reminder_state(singleton) VALUES(true);
    CREATE TABLE reminder_channels (
      rule_id uuid PRIMARY KEY REFERENCES keyword_policies(rule_id), enabled boolean NOT NULL DEFAULT true,
      cooldown_seconds integer NOT NULL CHECK(cooldown_seconds BETWEEN 0 AND 86400),
      dedup_seconds integer NOT NULL CHECK(dedup_seconds BETWEEN 60 AND 604800),
      preset_key text UNIQUE, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CHECK(dedup_seconds>=cooldown_seconds));
    CREATE TABLE reminders (
      reminder_id uuid PRIMARY KEY, rule_id uuid NOT NULL, rule_version_id uuid NOT NULL,
      event_id uuid NOT NULL, version_id uuid NOT NULL, person_id uuid NOT NULL REFERENCES people(person_id),
      priority text NOT NULL CHECK(priority IN ('low','medium','high')), body_hash varchar(64) NOT NULL,
      status text NOT NULL CHECK(status IN ('active','pending_review','grouped','duplicate','superseded','corrected')),
      group_id uuid REFERENCES reminders(reminder_id), revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
      evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(rule_id,version_id), FOREIGN KEY(rule_id,rule_version_id) REFERENCES keyword_versions(rule_id,version_id),
      FOREIGN KEY(event_id,version_id) REFERENCES event_versions(event_id,version_id));
    CREATE INDEX reminders_order ON reminders(priority,created_at DESC,reminder_id DESC);
    CREATE INDEX reminders_group ON reminders(rule_id,person_id,created_at DESC);
    CREATE INDEX reminders_body ON reminders(rule_id,person_id,body_hash,created_at DESC);
    CREATE TABLE reminder_actions (
      action_id uuid PRIMARY KEY, reminder_id uuid NOT NULL REFERENCES reminders(reminder_id),
      actor text NOT NULL, decision text NOT NULL CHECK(decision IN ('approve','false_positive','restore','supersede')),
      note text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp());
    CREATE TABLE reminder_feedback (
      feedback_id uuid PRIMARY KEY, reminder_id uuid NOT NULL REFERENCES reminders(reminder_id),
      user_id uuid NOT NULL REFERENCES agent_chat_users(user_id), client_request_id uuid NOT NULL,
      verdict text NOT NULL CHECK(verdict IN ('false_positive','useful')), note text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(user_id,client_request_id));
    CREATE FUNCTION pq_reminder_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      RAISE EXCEPTION 'Reminder audit is append-only'; END; $$;
    CREATE TRIGGER reminder_actions_immutable BEFORE UPDATE OR DELETE ON reminder_actions
      FOR EACH ROW EXECUTE FUNCTION pq_reminder_audit_immutable();
    CREATE TRIGGER reminder_feedback_immutable BEFORE UPDATE OR DELETE ON reminder_feedback
      FOR EACH ROW EXECUTE FUNCTION pq_reminder_audit_immutable();
    CREATE FUNCTION pq_reminder_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Reminder history cannot be deleted'; END IF;
      IF (to_jsonb(NEW)-ARRAY['status','revision','updated_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','revision','updated_at'])
      THEN RAISE EXCEPTION 'Reminder evidence is immutable'; END IF; RETURN NEW; END; $$;
    CREATE TRIGGER reminder_evidence_immutable BEFORE UPDATE OR DELETE ON reminders
      FOR EACH ROW EXECUTE FUNCTION pq_reminder_evidence_immutable();
    ''')

def downgrade():
    for name in ('reminder_feedback','reminder_actions','reminders','reminder_channels','reminder_state'):op.drop_table(name)
    op.execute('DROP FUNCTION pq_reminder_evidence_immutable(); DROP FUNCTION pq_reminder_audit_immutable()')
