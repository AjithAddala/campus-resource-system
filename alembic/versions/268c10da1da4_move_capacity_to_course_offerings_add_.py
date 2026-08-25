"""move capacity to course_offerings, add instructor_id and enrolled_count

Revision ID: 268c10da1da4
Revises: e0fbfe421403
Create Date: 2026-08-17 09:49:50.554337

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '268c10da1da4'
down_revision: Union[str, None] = 'e0fbfe421403'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
