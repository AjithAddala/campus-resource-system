"""move capacity to course_offerings, add instructor_id and enrolled_count

Revision ID: 268c10da1da4
Revises: e0fbfe421403
Create Date: 2026-08-17 09:49:50.554337

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '268c10da1da4'
down_revision: Union[str, None] = 'e0fbfe421403'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # capacity: courses -> course_offerings. Added nullable, backfilled from
    # the parent course, then tightened — so this is safe on a populated DB.
    op.add_column(
        "course_offerings", sa.Column("capacity", sa.Integer(), nullable=True)
    )
    op.execute(
        "UPDATE course_offerings o "
        "SET capacity = c.capacity "
        "FROM courses c WHERE c.id = o.course_id"
    )
    op.alter_column("course_offerings", "capacity", nullable=False)
    op.drop_column("courses", "capacity")

    # instructor_id has no backfill value, so NOT NULL only holds because
    # course_offerings is empty at this revision (verified: 0 rows).
    op.add_column(
        "course_offerings", sa.Column("instructor_id", sa.Integer(), nullable=False)
    )
    op.create_foreign_key(
        "fk_course_offerings_instructor_id_users",
        "course_offerings",
        "users",
        ["instructor_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_course_offerings_instructor_id"),
        "course_offerings",
        ["instructor_id"],
        unique=False,
    )

    # The counter locked by course registration. server_default so existing
    # rows get 0; the model's default=0 covers new inserts.
    op.add_column(
        "course_offerings",
        sa.Column(
            "enrolled_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )

    op.create_check_constraint(
        "offering_capacity_positive", "course_offerings", "capacity > 0"
    )
    op.create_check_constraint(
        "offering_enrollment_sane",
        "course_offerings",
        "enrolled_count >= 0 AND enrolled_count <= capacity",
    )


def downgrade() -> None:
    op.drop_constraint(
        "offering_enrollment_sane", "course_offerings", type_="check"
    )
    op.drop_constraint(
        "offering_capacity_positive", "course_offerings", type_="check"
    )
    op.drop_column("course_offerings", "enrolled_count")
    op.drop_index(
        op.f("ix_course_offerings_instructor_id"), table_name="course_offerings"
    )
    op.drop_constraint(
        "fk_course_offerings_instructor_id_users",
        "course_offerings",
        type_="foreignkey",
    )
    op.drop_column("course_offerings", "instructor_id")

    # Move capacity back. MAX over the course's offerings, since several
    # offerings collapse into one course row.
    op.add_column("courses", sa.Column("capacity", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE courses c "
        "SET capacity = sub.capacity FROM ("
        "  SELECT course_id, MAX(capacity) AS capacity"
        "  FROM course_offerings GROUP BY course_id"
        ") sub WHERE sub.course_id = c.id"
    )
    op.execute("UPDATE courses SET capacity = 0 WHERE capacity IS NULL")
    op.alter_column("courses", "capacity", nullable=False)
    op.drop_column("course_offerings", "capacity")
