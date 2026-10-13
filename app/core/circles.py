"""Circle membership. The POC has one demo circle per goal; real matching is future work."""
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.models import Circle, CircleMember, FeedEvent, FeedType, Goal, Learner

DEMO_CIRCLE_NAME = "Ajo Demo Circle"
_NAMESPACE = UUID("5a0f8e52-1c39-4d1b-8c1e-7d0b6a3f9e21")


def demo_circle_id(goal: Goal) -> UUID:
    return uuid5(_NAMESPACE, f"demo-circle\x00{goal.code}")


def demo_circle(session: Session, goal: Goal, now: datetime) -> Circle:
    """The goal's demo circle, created on first use.

    Its ID is derived from the goal code and inserted with ON CONFLICT DO NOTHING, so concurrent
    first joins converge on one row instead of creating two circles. Milestone 5 seeds the
    personas into the same circle.
    """
    circle_id = demo_circle_id(goal)
    circle = session.get(Circle, circle_id)
    if circle is not None:
        return circle
    insert = postgresql.insert if session.get_bind().dialect.name == "postgresql" else sqlite.insert
    session.execute(insert(Circle).values(id=circle_id, goal_id=goal.id, name=DEMO_CIRCLE_NAME,
                                          start_date=now.date(), created_at=now)
                    .on_conflict_do_nothing(index_elements=["id"]))
    return session.get(Circle, circle_id)


def join(session: Session, circle: Circle, learner: Learner, now: datetime) -> CircleMember:
    member = session.get(CircleMember, (circle.id, learner.id))
    if member is None:
        member = CircleMember(circle_id=circle.id, learner_id=learner.id, joined_at=now)
        session.add(member)
        session.flush()
    return member


def member_first_names(session: Session, circle_id: UUID) -> list[str]:
    """First names only, in joining order: the circle never shows anything more by default."""
    return list(session.scalars(
        select(Learner.first_name).join(CircleMember, CircleMember.learner_id == Learner.id)
        .where(CircleMember.circle_id == circle_id).order_by(CircleMember.joined_at, Learner.first_name)
    ))


def learner_circle_ids(session: Session, learner_id: UUID) -> list[UUID]:
    return list(session.scalars(select(CircleMember.circle_id).where(CircleMember.learner_id == learner_id)))


def record_member_left(session: Session, circle_ids: list[UUID], now: datetime) -> None:
    """Anonymous feed events for a deleted learner: no actor and an empty payload."""
    session.add_all(FeedEvent(circle_id=circle_id, type=FeedType.MEMBER_LEFT, actor_learner_id=None, payload={},
                              created_at=now) for circle_id in circle_ids)
    session.flush()
