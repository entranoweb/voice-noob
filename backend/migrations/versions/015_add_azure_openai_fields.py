"""Add Azure OpenAI fields to user_settings.

Revision ID: 015_azure_openai
Revises: c1a2629e6aad
Create Date: 2025-12-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "015_azure_openai"
down_revision: Union[str, Sequence[str], None] = "c1a2629e6aad"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add Azure OpenAI configuration fields."""
    op.add_column(
        "user_settings",
        sa.Column(
            "azure_openai_endpoint",
            sa.Text(),
            nullable=True,
            comment="Azure OpenAI endpoint URL",
        ),
    )
    op.add_column(
        "user_settings",
        sa.Column(
            "azure_openai_api_key",
            sa.Text(),
            nullable=True,
            comment="Azure OpenAI API key",
        ),
    )
    op.add_column(
        "user_settings",
        sa.Column(
            "azure_openai_deployment_name",
            sa.String(255),
            nullable=True,
            comment="Azure OpenAI deployment name (e.g., gpt-realtime)",
        ),
    )
    op.add_column(
        "user_settings",
        sa.Column(
            "openai_provider",
            sa.String(20),
            nullable=False,
            server_default="openai",
            comment="OpenAI provider: 'openai' or 'azure'",
        ),
    )


def downgrade() -> None:
    """Remove Azure OpenAI configuration fields."""
    op.drop_column("user_settings", "openai_provider")
    op.drop_column("user_settings", "azure_openai_deployment_name")
    op.drop_column("user_settings", "azure_openai_api_key")
    op.drop_column("user_settings", "azure_openai_endpoint")
