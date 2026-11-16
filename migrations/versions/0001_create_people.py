"""Create the people table only; do not seed public figures."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0001_create_people"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("person_id", pg.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name_original", sa.Text(), nullable=False),
        sa.Column("name_zh", sa.Text(), nullable=True),
        sa.Column("aliases", pg.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("country_codes", pg.ARRAY(sa.String(2)), server_default=sa.text("'{}'::varchar(2)[]"), nullable=False),
        sa.Column("monitoring_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("person_id", name="pk_people"),
        sa.CheckConstraint("btrim(name_original) <> ''", name="ck_people_name_original_nonblank"),
        sa.CheckConstraint("name_zh IS NULL OR btrim(name_zh) <> ''", name="ck_people_name_zh_nonblank"),
        sa.CheckConstraint("array_position(aliases, NULL) IS NULL", name="ck_people_aliases_no_null"),
        sa.CheckConstraint("array_position(country_codes, NULL) IS NULL", name="ck_people_country_codes_no_null"),
    )
    # Keep updated_at correct even for writes made outside an ORM.
    op.execute("""
        CREATE FUNCTION pq_people_touch_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = clock_timestamp();
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER people_touch_updated_at
        BEFORE UPDATE ON people
        FOR EACH ROW EXECUTE FUNCTION pq_people_touch_updated_at()
    """)


def downgrade() -> None:
    # Explicit rollback removes the table and any data it contains.
    op.drop_table("people")
    op.execute("DROP FUNCTION pq_people_touch_updated_at()")
