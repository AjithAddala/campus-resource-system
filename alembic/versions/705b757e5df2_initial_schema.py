"""initial schema

Revision ID: 705b757e5df2
Revises: 
Create Date: 2026-08-15 15:42:42.486300

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '705b757e5df2'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('courses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('capacity', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_courses_code'), 'courses', ['code'], unique=False)
    op.create_table('resources',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('resource_type', sa.Enum('GPU', 'ROOM', 'COURSE', name='resource_type_enum'), nullable=False),
    sa.Column('status', sa.Enum('AVAILABLE', 'BLOCKED', name='resource_status_enum'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_resources_resource_type'), 'resources', ['resource_type'], unique=False)
    op.create_table('role_quotas',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('role', sa.Enum('STUDENT', 'FACULTY', 'ADMIN', name='role_enum'), nullable=False),
    sa.Column('resource_type', sa.Enum('GPU', 'ROOM', 'COURSE', name='resource_type_enum'), nullable=False),
    sa.Column('max_units', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('role', 'resource_type', name='role_quota_unique')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.Enum('STUDENT', 'FACULTY', 'ADMIN', name='role_enum'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_table('course_offerings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('course_id', sa.Integer(), nullable=False),
    sa.Column('semester', sa.String(length=20), nullable=False),
    sa.Column('year', sa.Integer(), nullable=False),
    sa.Column('start_time', sa.String(length=5), nullable=False),
    sa.Column('end_time', sa.String(length=5), nullable=False),
    sa.Column('days', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('gpu_clusters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gpu_count', sa.Integer(), nullable=False),
    sa.Column('allocated', sa.Integer(), nullable=False),
    sa.CheckConstraint('allocated >= 0 AND allocated <= gpu_count', name='gpu_capacity_sane'),
    sa.ForeignKeyConstraint(['id'], ['resources.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('idempotency_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('endpoint', sa.String(length=255), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('response_body', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('status_code', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key', 'user_id', name='idempotency_key_user_unique')
    )
    op.create_table('reservations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('resource_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('start_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'CANCELLED', name='reservation_status_enum'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('rooms',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('building', sa.String(length=255), nullable=False),
    sa.Column('capacity', sa.Integer(), nullable=False),
    sa.CheckConstraint('capacity > 0', name='room_capacity_positive'),
    sa.ForeignKeyConstraint(['id'], ['resources.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('enrollments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('course_offering_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'DROPPED', 'WAITLISTED', name='enrollment_status_enum'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['course_offering_id'], ['course_offerings.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('student_id', 'course_offering_id', name='enrollment_unique')
    )
    op.create_table('gpu_reservations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gpu_cluster_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('gpu_count', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'CANCELLED', name='reservation_status_enum'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['gpu_cluster_id'], ['gpu_clusters.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('waitlist_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('student_id', sa.Integer(), nullable=False),
    sa.Column('course_offering_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['course_offering_id'], ['course_offerings.id'], ),
    sa.ForeignKeyConstraint(['student_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('waitlist_entries')
    op.drop_table('gpu_reservations')
    op.drop_table('enrollments')
    op.drop_table('rooms')
    op.drop_table('reservations')
    op.drop_table('idempotency_keys')
    op.drop_table('gpu_clusters')
    op.drop_table('course_offerings')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_table('role_quotas')
    op.drop_index(op.f('ix_resources_resource_type'), table_name='resources')
    op.drop_table('resources')
    op.drop_index(op.f('ix_courses_code'), table_name='courses')
    op.drop_table('courses')

    for enum_name in (
        "enrollment_status_enum",
        "reservation_status_enum",
        "resource_status_enum",
        "resource_type_enum",
        "role_enum",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name};")
