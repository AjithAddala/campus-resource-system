"""add room overlap exclusion constraint

Revision ID: e0fbfe421403
Revises: 705b757e5df2
Create Date: 2026-08-15 15:50:50.122029

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e0fbfe421403'
down_revision: Union[str, None] = '705b757e5df2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
    op.execute("""
        ALTER TABLE reservations
        ADD CONSTRAINT no_overlapping_room_reservations
        EXCLUDE USING gist (
            resource_id WITH =,
            tstzrange(start_time, end_time, '[)') WITH &&
        ) WHERE (status = 'ACTIVE');
    """)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE reservations "
        "DROP CONSTRAINT IF EXISTS no_overlapping_room_reservations;"
    )