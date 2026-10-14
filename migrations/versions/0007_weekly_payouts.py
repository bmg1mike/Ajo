"""Weekly payouts: one claimed row per circle and week; each opportunity's eligibility, kind and priority

Existing opportunities keep a null `eligibility`, kind `other` and priority 100 until the next startup
syncs data/opportunities.json.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('weekly_payouts',
    sa.Column('circle_id', sa.Uuid(), nullable=False),
    sa.Column('week_index', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('summary', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['circle_id'], ['circles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('circle_id', 'week_index')
    )
    with op.batch_alter_table('opportunities') as batch:
        batch.add_column(sa.Column('eligibility', sa.Text(), nullable=True))
        batch.add_column(sa.Column('kind', sa.Enum('scholarship', 'loan', 'exam', 'resource', 'other',
                                                   name='opportunity_kind', native_enum=False, create_constraint=True),
                                   server_default='other', nullable=False))
        batch.add_column(sa.Column('priority', sa.Integer(), server_default='100', nullable=False))


def downgrade():
    with op.batch_alter_table('opportunities') as batch:
        batch.drop_column('priority')
        batch.drop_column('kind')
        batch.drop_column('eligibility')
    op.drop_table('weekly_payouts')
