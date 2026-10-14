"""Web simulator: persona control by a pane, and the web channel's inbox

Existing learners get no `controlled_at`, so every persona keeps its automated behavior until a
pane takes control of it. The check constraint keeps the column for personas only.

SQLite cannot add a constraint to an existing table except by recreating it, and recreating
`learners` would drop it with foreign keys enabled, cascading deletes into every table that
references it. There the CHECK is attached to the added column instead, which SQLite allows.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

CONSTRAINT = 'controlled_persona_only'
CONDITION = 'is_simulated OR controlled_at IS NULL'


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        op.execute(f'ALTER TABLE learners ADD COLUMN controlled_at DATETIME CONSTRAINT {CONSTRAINT} CHECK ({CONDITION})')
    else:
        op.add_column('learners', sa.Column('controlled_at', sa.DateTime(timezone=True), nullable=True))
        op.create_check_constraint(CONSTRAINT, 'learners', CONDITION)
    op.create_table('web_messages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('learner_id', sa.Uuid(), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['learner_id'], ['learners.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_web_messages_learner_id'), 'web_messages', ['learner_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_web_messages_learner_id'), table_name='web_messages')
    op.drop_table('web_messages')
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE learners DROP COLUMN controlled_at')
    else:
        op.drop_constraint(CONSTRAINT, 'learners', type_='check')
        op.drop_column('learners', 'controlled_at')
