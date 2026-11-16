"""Private user identities and durable conversations; no model execution."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision='0017_agent_chat'
down_revision='0016_agent_analysis'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('agent_chat_users',
        sa.Column('user_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('display_name',sa.String(80),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('disabled_at',sa.DateTime(timezone=True)),
        sa.CheckConstraint('length(btrim(display_name)) BETWEEN 1 AND 80'))
    op.create_table('agent_chat_tokens',
        sa.Column('token_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('user_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_chat_users.user_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('token_digest',sa.String(64),nullable=False,unique=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('revoked_at',sa.DateTime(timezone=True)),
        sa.CheckConstraint("token_digest ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint('expires_at>created_at'))
    op.create_index('ix_chat_tokens_user','agent_chat_tokens',['user_id'])
    op.create_table('agent_chat_conversations',
        sa.Column('conversation_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('user_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_chat_users.user_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('client_request_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('creation_hash',sa.String(64),nullable=False),
        sa.Column('title',sa.String(120),nullable=False),
        sa.Column('person_id',pg.UUID(as_uuid=True),sa.ForeignKey('people.person_id',ondelete='RESTRICT')),
        sa.Column('revision',sa.Integer(),nullable=False,server_default='1'),
        sa.Column('message_count',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('archived',sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('user_id','client_request_id'),
        sa.CheckConstraint("creation_hash ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint('length(btrim(title)) BETWEEN 1 AND 120'),
        sa.CheckConstraint('revision>=1 AND message_count BETWEEN 0 AND 1000'))
    op.create_index('ix_chat_conversations_owner_created','agent_chat_conversations',['user_id','created_at','conversation_id'])
    op.create_table('agent_chat_messages',
        sa.Column('message_id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('conversation_id',pg.UUID(as_uuid=True),sa.ForeignKey('agent_chat_conversations.conversation_id',ondelete='RESTRICT'),nullable=False),
        sa.Column('client_request_id',pg.UUID(as_uuid=True),nullable=False),
        sa.Column('position',sa.Integer(),nullable=False),
        sa.Column('role',sa.Text(),nullable=False),
        sa.Column('content',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('conversation_id','position'),
        sa.UniqueConstraint('conversation_id','client_request_id'),
        sa.CheckConstraint('position BETWEEN 1 AND 1000'),
        sa.CheckConstraint("role='user'"),
        sa.CheckConstraint('length(btrim(content)) BETWEEN 1 AND 4000'))
    # Assistant messages, answers and citations will be introduced with the answering
    # unit, rather than letting clients impersonate an assistant in this storage API.
    op.execute("""CREATE FUNCTION pq_chat_message_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Chat messages are append-only'; END; $$""")
    op.execute('CREATE TRIGGER agent_chat_message_immutable BEFORE UPDATE OR DELETE ON agent_chat_messages '
               'FOR EACH ROW EXECUTE FUNCTION pq_chat_message_immutable()')


def downgrade():
    op.drop_table('agent_chat_messages')
    op.execute('DROP FUNCTION pq_chat_message_immutable()')
    op.drop_table('agent_chat_conversations');op.drop_table('agent_chat_tokens');op.drop_table('agent_chat_users')
