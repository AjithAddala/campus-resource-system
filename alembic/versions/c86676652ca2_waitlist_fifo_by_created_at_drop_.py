"""waitlist FIFO by created_at: drop position, add unique and order index

Revision ID: c86676652ca2
Revises: 268c10da1da4
Create Date: 2026-08-17 09:58:52.482226

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c86676652ca2'
down_revision: Union[str, None] = '268c10da1da4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("waitlist_entries", "position")

    op.create_unique_constraint(
        "waitlist_unique", "waitlist_entries", ["student_id", "course_offering_id"]
    )

    op.create_index(
        "ix_waitlist_entries_offering_created",
        "waitlist_entries",
        ["course_offering_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_waitlist_entries_offering_created", table_name="waitlist_entries"
    )
    op.drop_constraint("waitlist_unique", "waitlist_entries", type_="unique")

    op.add_column(
        "waitlist_entries", sa.Column("position", sa.Integer(), nullable=True)
    )
    op.execute(
        "UPDATE waitlist_entries w "
        "SET position = sub.rn FROM ("
        "  SELECT id, ROW_NUMBER() OVER ("
        "    PARTITION BY course_offering_id ORDER BY created_at, id"
        "  ) AS rn FROM waitlist_entries"
        ") sub WHERE sub.id = w.id"
    )
    op.alter_column("waitlist_entries", "position", nullable=False)
