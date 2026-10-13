from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.privacy import channel_hash, delete_learner
from app.models import (
    Answer, Channel, Circle, CircleMember, FeedEvent, FeedType, Goal,
    Learner, LLMCache, Nudge, OutboundDelivery, QALog, Question, Lesson,
    SyllabusTopic,
)

SALT = "synthetic-test-salt-with-32-characters"


def test_hash_is_salted_and_namespaced():
    hashed = channel_hash(Channel.TELEGRAM, "123", SALT)
    assert len(hashed) == 64
    assert hashed == channel_hash(Channel.TELEGRAM, "123", SALT)
    assert hashed != channel_hash(Channel.USSD, "123", SALT)
    assert hashed != channel_hash(Channel.TELEGRAM, "123", SALT + "other")
    assert "123" != hashed


@pytest.mark.parametrize("user_id,salt", [("", SALT), ("123", "")])
def test_hash_rejects_missing_identity_or_salt(user_id, salt):
    with pytest.raises(ValueError):
        channel_hash(Channel.WEB, user_id, salt)


@pytest.fixture
def learner_graph(sessions, clock):
    now = clock.now()
    with sessions.begin() as session:
        goal = Goal(code=f"test_{uuid4().hex}", title="Biology", syllabus_ref="demo.json")
        session.add(goal)
        session.flush()
        target = Learner(channel=Channel.TELEGRAM, channel_user_hash=channel_hash(Channel.TELEGRAM, uuid4().hex, SALT),
                         channel_chat_id=f"chat_{uuid4().hex}", first_name="SyntheticTarget", goal_id=goal.id, created_at=now)
        other = Learner(channel=Channel.WEB, channel_user_hash=channel_hash(Channel.WEB, uuid4().hex, SALT),
                        first_name="SyntheticOther", goal_id=goal.id, created_at=now)
        circle = Circle(goal_id=goal.id, name="Demo", start_date=now.date(), created_at=now)
        topic = SyllabusTopic(goal_id=goal.id, order_index=1, title="Cells", objectives="Learn cells")
        session.add_all([target, other, circle, topic])
        session.flush()
        lesson = Lesson(circle_id=circle.id, day_index=1, topic_id=topic.id, body={"en": "Synthetic lesson"},
                        cited_chunk_ids=[], posted_at=now)
        session.add(lesson)
        session.flush()
        question = Question(lesson_id=lesson.id, stem="Demo?", options={"A": "yes"}, correct_option="A",
                            explanation="Demo", cited_chunk_ids=[])
        session.add(question)
        session.flush()
        session.add_all([
            CircleMember(circle_id=circle.id, learner_id=target.id, joined_at=now),
            CircleMember(circle_id=circle.id, learner_id=other.id, joined_at=now),
            Answer(question_id=question.id, learner_id=target.id, chosen_option="A", is_correct=True, answered_at=now),
            Answer(question_id=question.id, learner_id=other.id, chosen_option="A", is_correct=True, answered_at=now),
            FeedEvent(circle_id=circle.id, type=FeedType.ANSWERED, actor_learner_id=target.id, payload={}, created_at=now),
            FeedEvent(circle_id=circle.id, type=FeedType.NUDGE_SENT, actor_learner_id=other.id,
                      payload={"nested": [{"recipient": str(target.id)}]}, created_at=now),
            FeedEvent(circle_id=circle.id, type=FeedType.CHECKIN, actor_learner_id=other.id, payload={}, created_at=now),
            Nudge(circle_id=circle.id, from_learner_id=other.id, to_learner_id=target.id, virtual_day=now.date(), created_at=now),
            Nudge(circle_id=circle.id, from_learner_id=target.id, to_learner_id=other.id, virtual_day=now.date(), created_at=now),
            QALog(learner_id=target.id, question_text="Synthetic?", retrieved_chunk_ids=[], answered=False, created_at=now),
            OutboundDelivery(learner_id=target.id, channel=target.channel, channel_chat_id=target.channel_chat_id,
                             payload={"text": "Hi"}, external_message_id="external-id", created_at=now),
            OutboundDelivery(learner_id=other.id, channel=other.channel, payload={"text": f"Member {target.id} checked in"}, created_at=now),
            OutboundDelivery(learner_id=other.id, channel=other.channel, payload={"text": "Generic"},
                             deduplication_key=f"notice:{target.id}", created_at=now),
            QALog(learner_id=other.id, question_text=f"Peer {target.id}", retrieved_chunk_ids=[], answered=False, created_at=now),
            LLMCache(prompt_hash=uuid4().hex, response={"text": target.first_name}, created_at=now),
        ])
        return target.id, other.id, circle.id, question.id, target.channel_chat_id


def test_deletion_requires_confirmation(sessions, learner_graph):
    target_id, *_ = learner_graph
    with sessions.begin() as session:
        with pytest.raises(ValueError, match="confirmation"):
            delete_learner(session, target_id)
        assert session.get(Learner, target_id) is not None


def test_deletion_removes_all_references_preserves_other_learner(sessions, learner_graph):
    target_id, other_id, circle_id, question_id, chat_id = learner_graph
    with sessions.begin() as session:
        assert delete_learner(session, target_id, confirmed=True)
    with sessions() as session:
        assert session.get(Learner, target_id) is None
        other = session.get(Learner, other_id)
        assert other is not None
        assert other.share_scores is False
        assert other.difficulty == 2
        assert session.get(Question, question_id) is not None
        assert session.get(CircleMember, (circle_id, target_id)) is None
        assert session.get(CircleMember, (circle_id, other_id)) is not None
        assert session.scalar(select(func.count()).select_from(Answer).where(Answer.learner_id == target_id)) == 0
        assert session.scalar(select(func.count()).select_from(Answer).where(Answer.learner_id == other_id)) == 1
        assert session.scalar(select(func.count()).select_from(Nudge).where(Nudge.circle_id == circle_id)) == 0
        assert session.scalar(select(func.count()).select_from(FeedEvent).where(FeedEvent.circle_id == circle_id)) == 1
        assert session.scalar(select(func.count()).select_from(QALog).where(QALog.learner_id == target_id)) == 0
        assert session.scalar(select(func.count()).select_from(OutboundDelivery).where(OutboundDelivery.learner_id.in_([target_id, other_id]))) == 0
        assert session.scalar(select(func.count()).select_from(OutboundDelivery).where(OutboundDelivery.channel_chat_id == chat_id)) == 0
        assert all("SyntheticTarget" not in str(row.response) for row in session.scalars(select(LLMCache)))
    with sessions.begin() as session:
        assert delete_learner(session, target_id, confirmed=True) is False


def test_deletion_is_atomic_with_caller_transaction(sessions, learner_graph):
    target_id, *_ = learner_graph
    with sessions() as session:
        delete_learner(session, target_id, confirmed=True)
        session.rollback()
    with sessions() as session:
        assert session.get(Learner, target_id) is not None
        assert session.scalar(select(func.count()).select_from(QALog).where(QALog.learner_id == target_id)) == 1


def test_simulated_learners_cannot_have_chat_ids(sessions, clock):
    with pytest.raises(IntegrityError):
        with sessions.begin() as session:
            session.add(Learner(channel=Channel.WEB, channel_user_hash=uuid4().hex, first_name="Synthetic",
                                channel_chat_id="forbidden", is_simulated=True, created_at=clock.now()))
