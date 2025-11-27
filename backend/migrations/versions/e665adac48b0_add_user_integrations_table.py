"""add user_integrations table

Revision ID: e665adac48b0
Revises: 91acf3ffe096
Create Date: 2025-11-28 03:52:03.966199

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e665adac48b0'
down_revision: Union[str, Sequence[str], None] = '91acf3ffe096'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('user_integrations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=True, comment='Workspace this integration belongs to (null = user-level)'),
    sa.Column('integration_id', sa.String(length=100), nullable=False, comment="Integration slug (e.g., 'hubspot', 'slack')"),
    sa.Column('integration_name', sa.String(length=200), nullable=False, comment="Display name (e.g., 'HubSpot', 'Slack')"),
    sa.Column('credentials', sa.JSON(), nullable=False, comment='Encrypted credentials (access_token, api_key, etc.)'),
    sa.Column('is_active', sa.Boolean(), nullable=False, default=True, comment='Whether integration is currently active'),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True, comment='Last time integration was used'),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True, comment='OAuth token expiration (if applicable)'),
    sa.Column('refresh_token', sa.Text(), nullable=True, comment='OAuth refresh token (encrypted)'),
    sa.Column('integration_metadata', sa.JSON(), nullable=True, comment='Additional integration-specific metadata'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_integrations_integration_id'), 'user_integrations', ['integration_id'], unique=False)
    op.create_index(op.f('ix_user_integrations_user_id'), 'user_integrations', ['user_id'], unique=False)
    op.create_index(op.f('ix_user_integrations_workspace_id'), 'user_integrations', ['workspace_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_user_integrations_workspace_id'), table_name='user_integrations')
    op.drop_index(op.f('ix_user_integrations_user_id'), table_name='user_integrations')
    op.drop_index(op.f('ix_user_integrations_integration_id'), table_name='user_integrations')
    op.drop_table('user_integrations')
