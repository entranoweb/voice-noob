"""caller audio and per-workspace audio retention

Adds ``caller_audio`` (one row per recorded side of a caller call; the bytes
are in the audio store, not the database) and ``workspaces.audio_retention_days``
(how long that audio is kept; 30 for every existing workspace).

Revision ID: e3a7c9d41f68
Revises: d2f5a8c31b47
Create Date: 2026-11-25 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3a7c9d41f68'
down_revision: Union[str, Sequence[str], None] = 'd2f5a8c31b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'workspaces',
        sa.Column(
            'audio_retention_days',
            sa.Integer(),
            nullable=False,
            server_default='30',
            comment='Days call audio is kept before the retention job deletes it',
        ),
    )
    op.create_table(
        'caller_audio',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('run_pk', sa.Uuid(), nullable=False),
        sa.Column('call_id', sa.Uuid(), nullable=False),
        sa.Column('track', sa.String(length=16), nullable=False),
        sa.Column('storage_key', sa.String(length=120), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('duration_s', sa.Float(), nullable=False),
        sa.Column('sample_rate', sa.Integer(), nullable=False),
        sa.Column('channels', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('expired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['run_pk'], ['caller_runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['call_id'], ['caller_calls.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('call_id', 'track', name='uq_caller_audio_call_track'),
    )
    op.create_index('ix_caller_audio_workspace_id', 'caller_audio', ['workspace_id'])
    op.create_index('ix_caller_audio_run_pk', 'caller_audio', ['run_pk'])
    op.create_index('ix_caller_audio_call_id', 'caller_audio', ['call_id'])


def downgrade() -> None:
    # The files in the audio store are not touched: a downgrade cannot reach
    # them through the database it is removing. Delete the audio directory
    # separately if the audio is to go.
    op.drop_index('ix_caller_audio_call_id', table_name='caller_audio')
    op.drop_index('ix_caller_audio_run_pk', table_name='caller_audio')
    op.drop_index('ix_caller_audio_workspace_id', table_name='caller_audio')
    op.drop_table('caller_audio')
    op.drop_column('workspaces', 'audio_retention_days')
