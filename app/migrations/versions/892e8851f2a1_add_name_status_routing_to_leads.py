"""add_name_status_routing_to_leads

Revision ID: 892e8851f2a1
Revises: dfcba14c573f
Create Date: 2026-04-16 15:54:48.102926

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '892e8851f2a1'
down_revision: Union[str, None] = 'dfcba14c573f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # We do 'pass' because we already added these columns manually.
    # Running this script will now just 'stamp' the DB with this version ID.
    pass

def downgrade() -> None:
    # Likewise, we leave this empty to maintain current state.
    pass
