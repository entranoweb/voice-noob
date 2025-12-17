"""Add campaign scheduler and disposition fields

Revision ID: e9a148e30e35
Revises: 3016f91d53b4
Create Date: 2025-12-01 21:30:14.187414

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e9a148e30e35'
down_revision: Union[str, Sequence[str], None] = '3016f91d53b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add scheduler fields to campaigns
    op.add_column('campaigns', sa.Column('calling_hours_start', sa.Time(), nullable=True, comment='Start of daily calling window (e.g., 09:00)'))
    op.add_column('campaigns', sa.Column('calling_hours_end', sa.Time(), nullable=True, comment='End of daily calling window (e.g., 17:00)'))
    op.add_column('campaigns', sa.Column('calling_days', postgresql.ARRAY(sa.Integer()), nullable=True, comment='Days of week to call (0=Mon, 6=Sun). Null means all days.'))
    op.add_column('campaigns', sa.Column('timezone', sa.String(length=50), nullable=True, comment='Timezone for calling hours'))

    # Add disposition fields to campaign_contacts
    op.add_column('campaign_contacts', sa.Column('disposition', sa.String(length=50), nullable=True, comment='Call disposition/outcome code'))
    op.add_column('campaign_contacts', sa.Column('disposition_notes', sa.Text(), nullable=True, comment='Notes about the call outcome'))
    op.add_column('campaign_contacts', sa.Column('callback_requested_at', sa.DateTime(timezone=True), nullable=True, comment='When callback was requested'))
    op.create_index(op.f('ix_campaign_contacts_disposition'), 'campaign_contacts', ['disposition'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Remove disposition fields from campaign_contacts
    op.drop_index(op.f('ix_campaign_contacts_disposition'), table_name='campaign_contacts')
    op.drop_column('campaign_contacts', 'callback_requested_at')
    op.drop_column('campaign_contacts', 'disposition_notes')
    op.drop_column('campaign_contacts', 'disposition')

    # Remove scheduler fields from campaigns
    op.drop_column('campaigns', 'timezone')
    op.drop_column('campaigns', 'calling_days')
    op.drop_column('campaigns', 'calling_hours_end')
    op.drop_column('campaigns', 'calling_hours_start')
