"""Delivery expiry: a queued message that may only be sent before a deadline (peer nudges)

Nudges queued before this revision have no expiry, so the upgrade backfills it: a nudge may be
delivered only on the virtual day it was sent. The day comes from the nudge row its outbox key
(`nudge:<nudge id>`) names, or, if that row is gone, from the queued row's own virtual timestamp,
which the tap set at the same moment.

Revision ID: 0006
Revises: 0005
"""
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

NUDGE_KEY_PREFIX = 'nudge:'

deliveries = sa.table('outbound_deliveries', sa.column('id', sa.Uuid()), sa.column('deduplication_key', sa.Text()),
                      sa.column('created_at', sa.DateTime(timezone=True)),
                      sa.column('expires_at', sa.DateTime(timezone=True)))
nudges = sa.table('nudges', sa.column('id', sa.Uuid()), sa.column('virtual_day', sa.Date()))


def _end_of_day(day):
    """Matches app.core.nudges.end_of_day: the first virtual moment after the nudge's day."""
    return datetime.combine(day + timedelta(days=1), time(), tzinfo=UTC)


def _day(moment):
    # SQLite returns stored timestamps without a zone, in UTC.
    return (moment.astimezone(UTC) if moment.tzinfo else moment).date()


def _nudge_id(key):
    try:
        return UUID(key.removeprefix(NUDGE_KEY_PREFIX))
    except ValueError:
        return None


def upgrade():
    with op.batch_alter_table('outbound_deliveries') as batch:
        batch.add_column(sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.select(deliveries.c.id, deliveries.c.deduplication_key, deliveries.c.created_at)
                        .where(deliveries.c.deduplication_key.like(f'{NUDGE_KEY_PREFIX}%'),
                               deliveries.c.expires_at.is_(None))).all()
    nudge_ids = {nudge_id for _, key, _ in rows if (nudge_id := _nudge_id(key)) is not None}
    days = dict(bind.execute(sa.select(nudges.c.id, nudges.c.virtual_day).where(nudges.c.id.in_(nudge_ids))).all()) \
        if nudge_ids else {}
    for delivery_id, key, created_at in rows:
        day = days.get(_nudge_id(key)) or _day(created_at)
        bind.execute(sa.update(deliveries).where(deliveries.c.id == delivery_id)
                     .values(expires_at=_end_of_day(day)))


def downgrade():
    with op.batch_alter_table('outbound_deliveries') as batch:
        batch.drop_column('expires_at')
