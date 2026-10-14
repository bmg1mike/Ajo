"""Simulation and feed: seeded demo circles, daily-run claims, feed cursors, delivery retries, reply events

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

OLD_FEED_TYPES = ('checkin', 'answered', 'missed', 'nudge_sent', 'payout', 'question', 'member_left')
NEW_FEED_TYPES = OLD_FEED_TYPES + ('reply',)


def _feed_type_check(values):
    return "type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade():
    op.create_table('simulations',
    sa.Column('circle_id', sa.Uuid(), nullable=False),
    sa.Column('seed', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['circle_id'], ['circles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('circle_id')
    )
    op.create_table('daily_runs',
    sa.Column('circle_id', sa.Uuid(), nullable=False),
    sa.Column('virtual_day', sa.Date(), nullable=False),
    sa.Column('trigger', sa.Enum('scheduler', 'simulation', name='run_trigger', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('lesson_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['circle_id'], ['circles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['lesson_id'], ['lessons.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('circle_id', 'virtual_day')
    )
    with op.batch_alter_table('circle_members') as batch:
        batch.add_column(sa.Column('feed_delivered_at', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('feed_cursor_id', sa.Uuid(), nullable=True))
    with op.batch_alter_table('outbound_deliveries') as batch:
        batch.add_column(sa.Column('attempts', sa.Integer(), server_default='0', nullable=False))
        batch.add_column(sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table('feed_events') as batch:
        batch.drop_constraint('feed_type', type_='check')
        batch.create_check_constraint('feed_type', _feed_type_check(NEW_FEED_TYPES))


def downgrade():
    op.execute(sa.text("DELETE FROM feed_events WHERE type = 'reply'"))
    with op.batch_alter_table('feed_events') as batch:
        batch.drop_constraint('feed_type', type_='check')
        batch.create_check_constraint('feed_type', _feed_type_check(OLD_FEED_TYPES))
    with op.batch_alter_table('outbound_deliveries') as batch:
        batch.drop_column('failed_at')
        batch.drop_column('attempts')
    with op.batch_alter_table('circle_members') as batch:
        batch.drop_column('feed_cursor_id')
        batch.drop_column('feed_delivered_at')
    op.drop_table('daily_runs')
    op.drop_table('simulations')
