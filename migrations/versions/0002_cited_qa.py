"""Cited QA: question feed events and QA evaluation columns

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

OLD_FEED_TYPES = ('checkin', 'answered', 'missed', 'nudge_sent', 'payout')
NEW_FEED_TYPES = OLD_FEED_TYPES + ('question',)


def _feed_type_check(values):
    return "type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade():
    with op.batch_alter_table('qa_log') as batch:
        batch.add_column(sa.Column('top_score', sa.Float(), nullable=True))
        batch.add_column(sa.Column('cited_chunk_ids', sa.JSON().with_variant(postgresql.ARRAY(sa.Uuid()), 'postgresql'),
                                   nullable=True))
    with op.batch_alter_table('feed_events') as batch:
        batch.drop_constraint('feed_type', type_='check')
        batch.create_check_constraint('feed_type', _feed_type_check(NEW_FEED_TYPES))


def downgrade():
    op.execute(sa.text("DELETE FROM feed_events WHERE type = 'question'"))
    with op.batch_alter_table('feed_events') as batch:
        batch.drop_constraint('feed_type', type_='check')
        batch.create_check_constraint('feed_type', _feed_type_check(OLD_FEED_TYPES))
    with op.batch_alter_table('qa_log') as batch:
        batch.drop_column('cited_chunk_ids')
        batch.drop_column('top_score')
