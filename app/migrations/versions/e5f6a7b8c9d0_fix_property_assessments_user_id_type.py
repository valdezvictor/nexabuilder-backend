"""fix property_assessments user_id column type varchar to uuid

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-05-19

The property_assessments.user_id column was created as UUID by a prior
migration but the SQLAlchemy model declared it as String(36). asyncpg
rejects comparisons between UUID and varchar (operator does not exist:
uuid = character varying). This migration casts the column to UUID using
USING to preserve existing data, then sets the correct type.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The column is already UUID type in the DB — the model was wrong.
    # This migration is a no-op on the DB side (column type is correct)
    # but updates the Alembic revision chain so the model and DB are in sync.
    #
    # If by any chance the column is varchar, the USING clause safely casts it.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'property_assessments'
                AND column_name = 'user_id'
                AND data_type = 'character varying'
            ) THEN
                ALTER TABLE property_assessments
                ALTER COLUMN user_id TYPE uuid
                USING user_id::uuid;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE property_assessments
        ALTER COLUMN user_id TYPE varchar(36)
        USING user_id::varchar;
    """)
