"""Telegram onboarding: onboarding progress, consent time and anonymous member_left events

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

OLD_FEED_TYPES = ('checkin', 'answered', 'missed', 'nudge_sent', 'payout', 'question')
NEW_FEED_TYPES = OLD_FEED_TYPES + ('member_left',)


def _feed_type_check(values):
    return "type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade():
    op.create_table('onboarding',
    sa.Column('channel_user_hash', sa.String(length=64), nullable=False),
    sa.Column('channel', sa.Enum('telegram', 'web', 'ussd', name='onboarding_channel', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('step', sa.Enum('language', 'name', 'goal', name='onboarding_step', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('language', sa.Enum('en', 'yo', 'pcm', name='onboarding_language', native_enum=False, create_constraint=True), server_default='en', nullable=False),
    sa.Column('first_name', sa.Text(), nullable=True),
    sa.Column('consented_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('channel_user_hash')
    )
    with op.batch_alter_table('learners') as batch:
        batch.add_column(sa.Column('consented_at', sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table('feed_events') as batch:
        batch.drop_constraint('feed_type', type_='check')
        batch.alter_column('type', type_=sa.String(length=11), existing_type=sa.String(length=10), existing_nullable=False)
        batch.create_check_constraint('feed_type', _feed_type_check(NEW_FEED_TYPES))


def downgrade():
    op.execute(sa.text("DELETE FROM feed_events WHERE type = 'member_left'"))
    with op.batch_alter_table('feed_events') as batch:
        batch.drop_constraint('feed_type', type_='check')
        batch.alter_column('type', type_=sa.String(length=10), existing_type=sa.String(length=11), existing_nullable=False)
        batch.create_check_constraint('feed_type', _feed_type_check(OLD_FEED_TYPES))
    with op.batch_alter_table('learners') as batch:
        batch.drop_column('consented_at')
    op.drop_table('onboarding')
