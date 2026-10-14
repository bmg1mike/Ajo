from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, Float,
    ForeignKey, Integer, LargeBinary, String, Text, TypeDecorator, UniqueConstraint, Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Channel(StrEnum):
    TELEGRAM = "telegram"
    WEB = "web"
    USSD = "ussd"


class Language(StrEnum):
    EN = "en"
    YO = "yo"
    PCM = "pcm"


class FeedType(StrEnum):
    CHECKIN = "checkin"
    ANSWERED = "answered"
    MISSED = "missed"
    NUDGE_SENT = "nudge_sent"
    PAYOUT = "payout"
    QUESTION = "question"  # a learner chose to post an unanswered question to the circle
    MEMBER_LEFT = "member_left"  # anonymous: "A member left the circle." after /delete_me
    REPLY = "reply"  # a simulated member's canned reply, with a citation, to a circle question


class RunTrigger(StrEnum):
    SCHEDULER = "scheduler"
    SIMULATION = "simulation"


class OpportunityKind(StrEnum):
    SCHOLARSHIP = "scholarship"
    LOAN = "loan"
    EXAM = "exam"  # an official exam or admission step, such as UTME registration
    RESOURCE = "resource"  # free official study material
    OTHER = "other"


class OnboardingStep(StrEnum):
    LANGUAGE = "language"
    NAME = "name"
    GOAL = "goal"


def enum_type(enum, name):
    return Enum(enum, name=name, native_enum=False, create_constraint=True,
                values_callable=lambda items: [item.value for item in items])


JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class UUIDList(TypeDecorator):
    """uuid[] on PostgreSQL; a JSON list of strings elsewhere, returned as UUIDs either way."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(ARRAY(Uuid()) if dialect.name == "postgresql" else JSON())

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return [str(UUID(str(item))) for item in value]

    def process_result_value(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return [UUID(item) for item in value]


UUID_LIST = UUIDList()
TIMESTAMP = DateTime(timezone=True)


class Identified:
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)


class Goal(Identified, Base):
    __tablename__ = "goals"
    code: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(Text)
    exam_date: Mapped[date | None] = mapped_column(Date)
    country: Mapped[str] = mapped_column(String(2), default="NG", server_default="NG")
    syllabus_ref: Mapped[str] = mapped_column(Text)


class Learner(Identified, Base):
    __tablename__ = "learners"
    __table_args__ = (
        CheckConstraint("NOT is_simulated OR channel_chat_id IS NULL", name="simulated_no_chat"),
        CheckConstraint("difficulty BETWEEN 1 AND 3", name="learner_difficulty"),
    )
    channel: Mapped[Channel] = mapped_column(enum_type(Channel, "channel"))
    channel_user_hash: Mapped[str] = mapped_column(String(64), unique=True)
    channel_chat_id: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str] = mapped_column(Text)
    language: Mapped[Language] = mapped_column(enum_type(Language, "language"), default=Language.EN, server_default="en")
    goal_id: Mapped[UUID | None] = mapped_column(ForeignKey("goals.id", ondelete="RESTRICT"), index=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    share_scores: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    difficulty: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    # When the learner agreed to the consent message; null for simulated personas.
    consented_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class Onboarding(Base):
    """A channel user who has consented but not yet finished onboarding.

    Nothing is stored before consent. The row is keyed by the salted channel hash and is
    removed when the learner row is created, or by /delete_me.
    """
    __tablename__ = "onboarding"
    channel_user_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[Channel] = mapped_column(enum_type(Channel, "onboarding_channel"))
    step: Mapped[OnboardingStep] = mapped_column(enum_type(OnboardingStep, "onboarding_step"))
    language: Mapped[Language] = mapped_column(enum_type(Language, "onboarding_language"), default=Language.EN,
                                               server_default="en")
    first_name: Mapped[str | None] = mapped_column(Text)
    consented_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class Circle(Identified, Base):
    __tablename__ = "circles"
    goal_id: Mapped[UUID] = mapped_column(ForeignKey("goals.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class CircleMember(Base):
    __tablename__ = "circle_members"
    circle_id: Mapped[UUID] = mapped_column(ForeignKey("circles.id", ondelete="CASCADE"), primary_key=True)
    learner_id: Mapped[UUID] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    last_checkin_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    streak_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Feed cursor: the last circle event relayed to this member, as (created_at, id) so events sharing a
    # timestamp are never skipped; with no ID, every event up to feed_delivered_at was relayed.
    feed_delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    feed_cursor_id: Mapped[UUID | None] = mapped_column(Uuid())


class Simulation(Base):
    """A seeded demo circle: the seed makes persona behavior repeatable (spec section 9.2)."""
    __tablename__ = "simulations"
    circle_id: Mapped[UUID] = mapped_column(ForeignKey("circles.id", ondelete="CASCADE"), primary_key=True)
    seed: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class DailyRun(Base):
    """One row per circle and virtual day whose daily jobs ran, by the scheduler or /simulate_day.

    The primary key is the duplicate guard: a run claims its day with ON CONFLICT DO NOTHING.
    """
    __tablename__ = "daily_runs"
    circle_id: Mapped[UUID] = mapped_column(ForeignKey("circles.id", ondelete="CASCADE"), primary_key=True)
    virtual_day: Mapped[date] = mapped_column(Date, primary_key=True)
    trigger: Mapped[RunTrigger] = mapped_column(enum_type(RunTrigger, "run_trigger"))
    lesson_id: Mapped[UUID | None] = mapped_column(ForeignKey("lessons.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class Source(Identified, Base):
    __tablename__ = "sources"
    title: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str] = mapped_column(Text)
    license: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str] = mapped_column(Text)


class SyllabusTopic(Identified, Base):
    __tablename__ = "syllabus_topics"
    __table_args__ = (UniqueConstraint("goal_id", "order_index", name="topic_order"),)
    goal_id: Mapped[UUID] = mapped_column(ForeignKey("goals.id", ondelete="RESTRICT"), index=True)
    order_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    objectives: Mapped[str] = mapped_column(Text)


class Chunk(Identified, Base):
    __tablename__ = "chunks"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"), index=True)
    section: Mapped[str] = mapped_column(Text)
    page: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(384).with_variant(JSON(), "sqlite"))
    syllabus_topic_id: Mapped[UUID | None] = mapped_column(ForeignKey("syllabus_topics.id", ondelete="RESTRICT"), index=True)


class Lesson(Identified, Base):
    __tablename__ = "lessons"
    __table_args__ = (UniqueConstraint("circle_id", "day_index", name="circle_lesson_day"),)
    circle_id: Mapped[UUID] = mapped_column(ForeignKey("circles.id", ondelete="CASCADE"), index=True)
    day_index: Mapped[int] = mapped_column(Integer)
    topic_id: Mapped[UUID] = mapped_column(ForeignKey("syllabus_topics.id", ondelete="RESTRICT"))
    body: Mapped[dict] = mapped_column(JSON_TYPE)
    cited_chunk_ids: Mapped[list[UUID]] = mapped_column(UUID_LIST)
    posted_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    # Why live generation was rejected when the pre-generated lesson was used instead; null otherwise.
    fallback_reason: Mapped[str | None] = mapped_column(Text)


class Question(Identified, Base):
    __tablename__ = "questions"
    __table_args__ = (CheckConstraint("difficulty BETWEEN 1 AND 3", name="question_difficulty"),)
    lesson_id: Mapped[UUID] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    # The easier follow-up sent privately after a wrong answer points at the question it follows.
    parent_question_id: Mapped[UUID | None] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON_TYPE)
    correct_option: Mapped[str] = mapped_column(String(8))
    explanation: Mapped[str] = mapped_column(Text)
    cited_chunk_ids: Mapped[list[UUID]] = mapped_column(UUID_LIST)
    difficulty: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    # {"yo": {"stem": ..., "options": {...}, "explanation": ...}}; the columns above hold English.
    translations: Mapped[dict] = mapped_column(JSON_TYPE, default=dict, server_default="{}")


class Answer(Identified, Base):
    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("question_id", "learner_id", name="one_answer_per_question"),)
    question_id: Mapped[UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    learner_id: Mapped[UUID] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), index=True)
    chosen_option: Mapped[str] = mapped_column(String(8))
    is_correct: Mapped[bool] = mapped_column(Boolean)
    answered_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class PregeneratedLesson(Base):
    """A validated English lesson per topic, with validated translations: the fallback when generation fails.

    Corpus-derived, so it survives a demo reset; ingestion removes it if a chunk it cites is pruned.
    """
    __tablename__ = "pregenerated_lessons"
    topic_id: Mapped[UUID] = mapped_column(ForeignKey("syllabus_topics.id", ondelete="CASCADE"), primary_key=True)
    content: Mapped[dict] = mapped_column(JSON_TYPE)
    cited_chunk_ids: Mapped[list[UUID]] = mapped_column(UUID_LIST)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class FeedEvent(Identified, Base):
    __tablename__ = "feed_events"
    circle_id: Mapped[UUID] = mapped_column(ForeignKey("circles.id", ondelete="CASCADE"), index=True)
    type: Mapped[FeedType] = mapped_column(enum_type(FeedType, "feed_type"))
    actor_learner_id: Mapped[UUID | None] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), index=True)
    payload: Mapped[dict] = mapped_column(JSON_TYPE)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class Nudge(Identified, Base):
    __tablename__ = "nudges"
    __table_args__ = (UniqueConstraint("to_learner_id", "virtual_day", name="one_nudge_per_recipient_day"),)
    circle_id: Mapped[UUID] = mapped_column(ForeignKey("circles.id", ondelete="CASCADE"), index=True)
    from_learner_id: Mapped[UUID] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), index=True)
    to_learner_id: Mapped[UUID] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), index=True)
    virtual_day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class WeeklyPayout(Base):
    """One row per circle and week whose payout was queued (spec section 7.7).

    The primary key is the duplicate guard, beside the daily run's own claim. The week covers the
    lessons posted after `period_start` (the previous week's end; null for week 1) up to and including
    `period_end`. `summary` holds circle-level counts only, never a learner's ID or score, so the row
    is an anonymised aggregate that outlives /delete_me.
    """
    __tablename__ = "weekly_payouts"
    circle_id: Mapped[UUID] = mapped_column(ForeignKey("circles.id", ondelete="CASCADE"), primary_key=True)
    week_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    period_end: Mapped[datetime] = mapped_column(TIMESTAMP)
    summary: Mapped[dict] = mapped_column(JSON_TYPE)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class Opportunity(Identified, Base):
    """A hand-checked entry from data/opportunities.json, synced at startup; the file is the source of truth."""
    __tablename__ = "opportunities"
    title: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    country: Mapped[str] = mapped_column(String(2))
    goal_codes: Mapped[list[str]] = mapped_column(JSON_TYPE)
    deadline: Mapped[date | None] = mapped_column(Date)
    verified_on: Mapped[date] = mapped_column(Date)
    # Who may apply, in the provider's words; null only for rows synced before revision 0007.
    eligibility: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[OpportunityKind] = mapped_column(enum_type(OpportunityKind, "opportunity_kind"),
                                                  default=OpportunityKind.OTHER, server_default="other")
    # Payouts offer the lowest number first, then the nearest deadline: curators rank by value to the learner.
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")


class QALog(Identified, Base):
    __tablename__ = "qa_log"
    learner_id: Mapped[UUID] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), index=True)
    question_text: Mapped[str] = mapped_column(Text)
    retrieved_chunk_ids: Mapped[list[UUID]] = mapped_column(UUID_LIST)
    answered: Mapped[bool] = mapped_column(Boolean)
    fallback_reason: Mapped[str | None] = mapped_column(Text)
    # Evaluation aids: best retrieval similarity, and the citations of an accepted answer.
    top_score: Mapped[float | None] = mapped_column(Float)
    cited_chunk_ids: Mapped[list[UUID] | None] = mapped_column(UUID_LIST)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class ClockState(Base):
    __tablename__ = "clock_state"
    __table_args__ = (CheckConstraint("id = 1", name="singleton_clock"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offset_seconds: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class LLMCache(Base):
    __tablename__ = "llm_cache"
    # Hash the full provider/model/prompt configuration. Never store raw prompts.
    prompt_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    response: Mapped[dict] = mapped_column(JSON_TYPE)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class SpeechCache(Base):
    """Synthesized OGG/Opus audio of lesson text, keyed by a hash of model, language, text and encoding.

    Lessons name no learner, so nothing here refers to anyone; like the LLM cache, it survives a reset.
    """
    __tablename__ = "speech_cache"
    audio_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    audio: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class OutboundDelivery(Identified, Base):
    __tablename__ = "outbound_deliveries"
    learner_id: Mapped[UUID] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), index=True)
    channel: Mapped[Channel] = mapped_column(enum_type(Channel, "delivery_channel"))
    channel_chat_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON_TYPE)
    external_message_id: Mapped[str | None] = mapped_column(Text)
    deduplication_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Set when delivery gave up after repeated failures; such rows are not retried.
    failed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    # Virtual time from which the row must no longer be sent, e.g. the end of a nudge's day; null: never.
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
