"""Add embed settings to agents table

Revision ID: 014_add_embed_settings
Revises: 013_change_enabled_tools_to_json
Create Date: 2025-11-29

This migration adds fields for embeddable voice widget support:
- public_id: Short URL-safe ID for public access
- embed_enabled: Whether embedding is allowed
- allowed_domains: List of domains allowed to embed
- embed_settings: Widget customization options
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "014_embed_settings"
down_revision: Union[str, Sequence[str], None] = "013_tools_to_json"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add public_id column - unique, short URL-safe ID for public access
    op.add_column(
        "agents",
        sa.Column(
            "public_id",
            sa.String(32),
            nullable=True,
            unique=True,
            comment="Short URL-safe ID for public embed access (e.g., ag_xK9mN2pQ)",
        ),
    )
    op.create_index("ix_agents_public_id", "agents", ["public_id"], unique=True)

    # Add embed_enabled column - whether embedding is allowed
    op.add_column(
        "agents",
        sa.Column(
            "embed_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="true",
            comment="Whether widget embedding is enabled for this agent",
        ),
    )

    # Add allowed_domains column - JSON array of allowed domains
    op.add_column(
        "agents",
        sa.Column(
            "allowed_domains",
            sa.JSON(),
            nullable=False,
            server_default="[]",
            comment="List of domains allowed to embed this agent (supports wildcards)",
        ),
    )

    # Add embed_settings column - JSON object for widget customization
    op.add_column(
        "agents",
        sa.Column(
            "embed_settings",
            sa.JSON(),
            nullable=False,
            server_default='{"theme": "auto", "position": "bottom-right", "primary_color": "#6366f1", "greeting_message": "Hi! How can I help you today?", "button_text": "Talk to us"}',
            comment="Widget customization settings (theme, position, colors, etc.)",
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "embed_settings")
    op.drop_column("agents", "allowed_domains")
    op.drop_column("agents", "embed_enabled")
    op.drop_index("ix_agents_public_id", table_name="agents")
    op.drop_column("agents", "public_id")
