"""Foundation schema

Revision ID: 0001
Revises: 
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import pgvector.sqlalchemy

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('clock_state',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('offset_seconds', sa.BigInteger(), server_default='0', nullable=False),
    sa.CheckConstraint('id = 1', name='singleton_clock'),
    sa.PrimaryKeyConstraint('id')
    )
    op.bulk_insert(sa.table("clock_state", sa.column("id", sa.Integer), sa.column("offset_seconds", sa.BigInteger)), [{"id": 1, "offset_seconds": 0}])
    op.create_table('goals',
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('exam_date', sa.Date(), nullable=True),
    sa.Column('country', sa.String(length=2), server_default='NG', nullable=False),
    sa.Column('syllabus_ref', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('llm_cache',
    sa.Column('prompt_hash', sa.String(length=64), nullable=False),
    sa.Column('response', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('prompt_hash')
    )
    op.create_table('opportunities',
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('country', sa.String(length=2), nullable=False),
    sa.Column('goal_codes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('deadline', sa.Date(), nullable=True),
    sa.Column('verified_on', sa.Date(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('sources',
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('publisher', sa.Text(), nullable=False),
    sa.Column('license', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('file_path', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('circles',
    sa.Column('goal_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_circles_goal_id'), 'circles', ['goal_id'], unique=False)
    op.create_table('learners',
    sa.Column('channel', sa.Enum('telegram', 'web', 'ussd', name='channel', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('channel_user_hash', sa.String(length=64), nullable=False),
    sa.Column('channel_chat_id', sa.Text(), nullable=True),
    sa.Column('first_name', sa.Text(), nullable=False),
    sa.Column('language', sa.Enum('en', 'yo', 'pcm', name='language', native_enum=False, create_constraint=True), server_default='en', nullable=False),
    sa.Column('goal_id', sa.Uuid(), nullable=True),
    sa.Column('is_simulated', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('share_scores', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('difficulty', sa.Integer(), server_default='2', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('NOT is_simulated OR channel_chat_id IS NULL', name='simulated_no_chat'),
    sa.CheckConstraint('difficulty BETWEEN 1 AND 3', name='learner_difficulty'),
    sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel_user_hash')
    )
    op.create_index(op.f('ix_learners_goal_id'), 'learners', ['goal_id'], unique=False)
    op.create_table('syllabus_topics',
    sa.Column('goal_id', sa.Uuid(), nullable=False),
    sa.Column('order_index', sa.Integer(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('objectives', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('goal_id', 'order_index', name='topic_order')
    )
    op.create_index(op.f('ix_syllabus_topics_goal_id'), 'syllabus_topics', ['goal_id'], unique=False)
    op.create_table('chunks',
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('section', sa.Text(), nullable=False),
    sa.Column('page', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=384).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('syllabus_topic_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['syllabus_topic_id'], ['syllabus_topics.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chunks_source_id'), 'chunks', ['source_id'], unique=False)
    op.create_index(op.f('ix_chunks_syllabus_topic_id'), 'chunks', ['syllabus_topic_id'], unique=False)
    op.create_table('circle_members',
    sa.Column('circle_id', sa.Uuid(), nullable=False),
    sa.Column('learner_id', sa.Uuid(), nullable=False),
    sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_checkin_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('streak_days', sa.Integer(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['circle_id'], ['circles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['learner_id'], ['learners.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('circle_id', 'learner_id')
    )
    op.create_table('feed_events',
    sa.Column('circle_id', sa.Uuid(), nullable=False),
    sa.Column('type', sa.Enum('checkin', 'answered', 'missed', 'nudge_sent', 'payout', name='feed_type', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('actor_learner_id', sa.Uuid(), nullable=True),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['actor_learner_id'], ['learners.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['circle_id'], ['circles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_feed_events_actor_learner_id'), 'feed_events', ['actor_learner_id'], unique=False)
    op.create_index(op.f('ix_feed_events_circle_id'), 'feed_events', ['circle_id'], unique=False)
    op.create_table('lessons',
    sa.Column('circle_id', sa.Uuid(), nullable=False),
    sa.Column('day_index', sa.Integer(), nullable=False),
    sa.Column('topic_id', sa.Uuid(), nullable=False),
    sa.Column('body', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('cited_chunk_ids', sa.JSON().with_variant(postgresql.ARRAY(sa.Uuid()), 'postgresql'), nullable=False),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['circle_id'], ['circles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['topic_id'], ['syllabus_topics.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('circle_id', 'day_index', name='circle_lesson_day')
    )
    op.create_index(op.f('ix_lessons_circle_id'), 'lessons', ['circle_id'], unique=False)
    op.create_table('nudges',
    sa.Column('circle_id', sa.Uuid(), nullable=False),
    sa.Column('from_learner_id', sa.Uuid(), nullable=False),
    sa.Column('to_learner_id', sa.Uuid(), nullable=False),
    sa.Column('virtual_day', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['circle_id'], ['circles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['from_learner_id'], ['learners.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['to_learner_id'], ['learners.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('to_learner_id', 'virtual_day', name='one_nudge_per_recipient_day')
    )
    op.create_index(op.f('ix_nudges_circle_id'), 'nudges', ['circle_id'], unique=False)
    op.create_index(op.f('ix_nudges_from_learner_id'), 'nudges', ['from_learner_id'], unique=False)
    op.create_index(op.f('ix_nudges_to_learner_id'), 'nudges', ['to_learner_id'], unique=False)
    op.create_table('outbound_deliveries',
    sa.Column('learner_id', sa.Uuid(), nullable=False),
    sa.Column('channel', sa.Enum('telegram', 'web', 'ussd', name='delivery_channel', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('channel_chat_id', sa.Text(), nullable=True),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('external_message_id', sa.Text(), nullable=True),
    sa.Column('deduplication_key', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['learner_id'], ['learners.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('deduplication_key')
    )
    op.create_index(op.f('ix_outbound_deliveries_learner_id'), 'outbound_deliveries', ['learner_id'], unique=False)
    op.create_table('qa_log',
    sa.Column('learner_id', sa.Uuid(), nullable=False),
    sa.Column('question_text', sa.Text(), nullable=False),
    sa.Column('retrieved_chunk_ids', sa.JSON().with_variant(postgresql.ARRAY(sa.Uuid()), 'postgresql'), nullable=False),
    sa.Column('answered', sa.Boolean(), nullable=False),
    sa.Column('fallback_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['learner_id'], ['learners.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_qa_log_learner_id'), 'qa_log', ['learner_id'], unique=False)
    op.create_table('questions',
    sa.Column('lesson_id', sa.Uuid(), nullable=False),
    sa.Column('stem', sa.Text(), nullable=False),
    sa.Column('options', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('correct_option', sa.String(length=8), nullable=False),
    sa.Column('explanation', sa.Text(), nullable=False),
    sa.Column('cited_chunk_ids', sa.JSON().with_variant(postgresql.ARRAY(sa.Uuid()), 'postgresql'), nullable=False),
    sa.Column('difficulty', sa.Integer(), server_default='2', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('difficulty BETWEEN 1 AND 3', name='question_difficulty'),
    sa.ForeignKeyConstraint(['lesson_id'], ['lessons.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_questions_lesson_id'), 'questions', ['lesson_id'], unique=False)
    op.create_table('answers',
    sa.Column('question_id', sa.Uuid(), nullable=False),
    sa.Column('learner_id', sa.Uuid(), nullable=False),
    sa.Column('chosen_option', sa.String(length=8), nullable=False),
    sa.Column('is_correct', sa.Boolean(), nullable=False),
    sa.Column('answered_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['learner_id'], ['learners.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['question_id'], ['questions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_answers_learner_id'), 'answers', ['learner_id'], unique=False)
    op.create_index(op.f('ix_answers_question_id'), 'answers', ['question_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_answers_question_id'), table_name='answers')
    op.drop_index(op.f('ix_answers_learner_id'), table_name='answers')
    op.drop_table('answers')
    op.drop_index(op.f('ix_questions_lesson_id'), table_name='questions')
    op.drop_table('questions')
    op.drop_index(op.f('ix_qa_log_learner_id'), table_name='qa_log')
    op.drop_table('qa_log')
    op.drop_index(op.f('ix_outbound_deliveries_learner_id'), table_name='outbound_deliveries')
    op.drop_table('outbound_deliveries')
    op.drop_index(op.f('ix_nudges_to_learner_id'), table_name='nudges')
    op.drop_index(op.f('ix_nudges_from_learner_id'), table_name='nudges')
    op.drop_index(op.f('ix_nudges_circle_id'), table_name='nudges')
    op.drop_table('nudges')
    op.drop_index(op.f('ix_lessons_circle_id'), table_name='lessons')
    op.drop_table('lessons')
    op.drop_index(op.f('ix_feed_events_circle_id'), table_name='feed_events')
    op.drop_index(op.f('ix_feed_events_actor_learner_id'), table_name='feed_events')
    op.drop_table('feed_events')
    op.drop_table('circle_members')
    op.drop_index(op.f('ix_chunks_syllabus_topic_id'), table_name='chunks')
    op.drop_index(op.f('ix_chunks_source_id'), table_name='chunks')
    op.drop_table('chunks')
    op.drop_index(op.f('ix_syllabus_topics_goal_id'), table_name='syllabus_topics')
    op.drop_table('syllabus_topics')
    op.drop_index(op.f('ix_learners_goal_id'), table_name='learners')
    op.drop_table('learners')
    op.drop_index(op.f('ix_circles_goal_id'), table_name='circles')
    op.drop_table('circles')
    op.drop_table('sources')
    op.drop_table('opportunities')
    op.drop_table('llm_cache')
    op.drop_table('goals')
    op.drop_table('clock_state')
