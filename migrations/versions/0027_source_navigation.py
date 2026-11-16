"""Permit append-only host-generated official source navigation results."""
from alembic import op

revision='0027_source_navigation'
down_revision='0026_quality'
branch_labels=None
depends_on=None


def upgrade():
    op.drop_constraint('agent_qa_results_execution_kind_check','agent_qa_results',type_='check')
    op.create_check_constraint('agent_qa_results_execution_kind_check','agent_qa_results',
                              "execution_kind IN ('openrouter','synthetic_fixture','local_abstention','local_source_navigation')")


def downgrade():
    # Existing navigation records are immutable history. A downgrade with such
    # records intentionally fails validation instead of deleting/relabeling them.
    op.drop_constraint('agent_qa_results_execution_kind_check','agent_qa_results',type_='check')
    op.create_check_constraint('agent_qa_results_execution_kind_check','agent_qa_results',
                              "execution_kind IN ('openrouter','synthetic_fixture','local_abstention')")
