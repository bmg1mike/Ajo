"""Daily lessons: follow-up questions, translations, one answer per question, pre-generated lessons

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
UUID_LIST = sa.JSON().with_variant(postgresql.ARRAY(sa.Uuid()), 'postgresql')


def upgrade():
    op.create_table('pregenerated_lessons',
    sa.Column('topic_id', sa.Uuid(), nullable=False),
    sa.Column('content', JSON_TYPE, nullable=False),
    sa.Column('cited_chunk_ids', UUID_LIST, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['topic_id'], ['syllabus_topics.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('topic_id')
    )
    with op.batch_alter_table('lessons') as batch:
        batch.add_column(sa.Column('fallback_reason', sa.Text(), nullable=True))
    with op.batch_alter_table('questions') as batch:
        batch.add_column(sa.Column('parent_question_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('position', sa.Integer(), server_default='1', nullable=False))
        batch.add_column(sa.Column('translations', JSON_TYPE, server_default='{}', nullable=False))
        batch.create_foreign_key('questions_parent_question_id_fkey', 'questions', ['parent_question_id'], ['id'],
                                 ondelete='CASCADE')
        batch.create_index(batch.f('ix_questions_parent_question_id'), ['parent_question_id'], unique=False)
    with op.batch_alter_table('answers') as batch:
        batch.create_unique_constraint('one_answer_per_question', ['question_id', 'learner_id'])


def downgrade():
    with op.batch_alter_table('answers') as batch:
        batch.drop_constraint('one_answer_per_question', type_='unique')
    with op.batch_alter_table('questions') as batch:
        batch.drop_index(batch.f('ix_questions_parent_question_id'))
        batch.drop_constraint('questions_parent_question_id_fkey', type_='foreignkey')
        batch.drop_column('translations')
        batch.drop_column('position')
        batch.drop_column('parent_question_id')
    with op.batch_alter_table('lessons') as batch:
        batch.drop_column('fallback_reason')
    op.drop_table('pregenerated_lessons')
