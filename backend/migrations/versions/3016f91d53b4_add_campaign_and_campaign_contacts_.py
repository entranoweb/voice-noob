"""Add campaign and campaign_contacts tables

Revision ID: 3016f91d53b4
Revises: c1a2629e6aad
Create Date: 2025-12-01 18:17:35.862511

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '3016f91d53b4'
down_revision: Union[str, Sequence[str], None] = 'c1a2629e6aad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create campaigns table
    op.create_table('campaigns',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False, comment='Owner user ID'),
        sa.Column('workspace_id', sa.Uuid(), nullable=False, comment='Workspace this campaign belongs to'),
        sa.Column('agent_id', sa.Uuid(), nullable=False, comment='Agent that handles campaign calls'),
        sa.Column('name', sa.String(length=255), nullable=False, comment='Campaign name'),
        sa.Column('description', sa.Text(), nullable=True, comment='Campaign description'),
        sa.Column('status', sa.String(length=50), nullable=False, comment='Campaign status'),
        sa.Column('from_phone_number', sa.String(length=50), nullable=False, comment='Phone number to call from (E.164 format)'),
        sa.Column('scheduled_start', sa.DateTime(timezone=True), nullable=True, comment='When to start the campaign'),
        sa.Column('scheduled_end', sa.DateTime(timezone=True), nullable=True, comment='When to end the campaign'),
        sa.Column('calls_per_minute', sa.Integer(), nullable=False, comment='Max calls to initiate per minute'),
        sa.Column('max_concurrent_calls', sa.Integer(), nullable=False, comment='Max simultaneous active calls'),
        sa.Column('max_attempts_per_contact', sa.Integer(), nullable=False, comment='Max call attempts per contact'),
        sa.Column('retry_delay_minutes', sa.Integer(), nullable=False, comment='Minutes to wait before retry'),
        sa.Column('total_contacts', sa.Integer(), nullable=False, comment='Total contacts in campaign'),
        sa.Column('contacts_called', sa.Integer(), nullable=False, comment='Contacts that have been called'),
        sa.Column('contacts_completed', sa.Integer(), nullable=False, comment='Contacts with completed calls'),
        sa.Column('contacts_failed', sa.Integer(), nullable=False, comment='Contacts with failed calls'),
        sa.Column('total_call_duration_seconds', sa.Integer(), nullable=False, comment='Total duration of all calls'),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True, comment='When the campaign started running'),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True, comment='When the campaign completed'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_campaigns_agent_id'), 'campaigns', ['agent_id'], unique=False)
    op.create_index(op.f('ix_campaigns_status'), 'campaigns', ['status'], unique=False)
    op.create_index(op.f('ix_campaigns_user_id'), 'campaigns', ['user_id'], unique=False)
    op.create_index(op.f('ix_campaigns_workspace_id'), 'campaigns', ['workspace_id'], unique=False)

    # Create campaign_contacts junction table
    op.create_table('campaign_contacts',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('campaign_id', sa.Uuid(), nullable=False),
        sa.Column('contact_id', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False, comment='Contact status in this campaign'),
        sa.Column('attempts', sa.Integer(), nullable=False, comment='Number of call attempts'),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True, comment='When the last call was attempted'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True, comment='When to try next'),
        sa.Column('last_call_id', sa.Uuid(), nullable=True, comment='Most recent call record'),
        sa.Column('last_call_duration_seconds', sa.Integer(), nullable=False, comment='Duration of last call'),
        sa.Column('last_call_outcome', sa.String(length=50), nullable=True, comment='Outcome of last call'),
        sa.Column('priority', sa.Integer(), nullable=False, comment='Call priority (higher = sooner)'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['last_call_id'], ['call_records.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_campaign_contacts_campaign_id'), 'campaign_contacts', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_campaign_contacts_contact_id'), 'campaign_contacts', ['contact_id'], unique=False)
    op.create_index(op.f('ix_campaign_contacts_next_attempt_at'), 'campaign_contacts', ['next_attempt_at'], unique=False)
    op.create_index(op.f('ix_campaign_contacts_status'), 'campaign_contacts', ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_campaign_contacts_status'), table_name='campaign_contacts')
    op.drop_index(op.f('ix_campaign_contacts_next_attempt_at'), table_name='campaign_contacts')
    op.drop_index(op.f('ix_campaign_contacts_contact_id'), table_name='campaign_contacts')
    op.drop_index(op.f('ix_campaign_contacts_campaign_id'), table_name='campaign_contacts')
    op.drop_table('campaign_contacts')
    op.drop_index(op.f('ix_campaigns_workspace_id'), table_name='campaigns')
    op.drop_index(op.f('ix_campaigns_user_id'), table_name='campaigns')
    op.drop_index(op.f('ix_campaigns_status'), table_name='campaigns')
    op.drop_index(op.f('ix_campaigns_agent_id'), table_name='campaigns')
    op.drop_table('campaigns')
