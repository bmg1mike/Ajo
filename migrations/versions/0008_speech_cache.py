"""Speech cache: synthesized lesson audio, so a repeated Listen tap needs no TTS model

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('speech_cache',
    sa.Column('audio_hash', sa.String(length=64), nullable=False),
    sa.Column('audio', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('audio_hash')
    )


def downgrade():
    op.drop_table('speech_cache')
