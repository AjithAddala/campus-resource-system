"""Seed the database with the fixture set every benchmark and demo assumes.

Run inside the container:

    docker compose exec app python scripts/seed.py
    docker compose exec app python scripts/seed.py --reset

Without `--reset` it refuses to run against a non-empty database rather
than half-inserting and dying on a UNIQUE violation. With it, every table
is truncated and identity sequences restart at 1, so ids are reproducible
across runs — which matters because the benchmarks assert on *which*
rows were affected, not just how many.

Deliberate numbers, not arbitrary ones:

  offering capacity 50   Benchmark 1 fires 500 concurrent registrations
                         at it and expects exactly 50 to succeed.
  two GPU clusters       Benchmark 2 needs one student issuing two
                         concurrent 2-GPU requests at DIFFERENT clusters,
                         so both must have >= 2 free units. That test is
                         the whole argument for the user-row lock.
  (STUDENT, GPU) = 2     so the third GPU unit is the quota rejection.
"""
import argparse
import sys
from pathlib import Path

# Running a file inside scripts/ puts scripts/ on sys.path, not the repo
# root, so `app` would not import. Same job as alembic.ini's prepend_sys_path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argon2 import PasswordHasher  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database.base import Base  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Course,
    CourseOffering,
    GPUCluster,
    Role,
    ResourceType,
    RoleQuota,
    Room,
    User,
)

# One password for all three seeded accounts. Fine here and nowhere else:
# this database is thrown away and rebuilt on every benchmark run.
SEED_PASSWORD = "campus123"


def wipe(db) -> None:
    """TRUNCATE every mapped table, sequences included.

    Driven off `Base.metadata` rather than a hardcoded list so a table
    added later cannot be silently missed. `alembic_version` is not in
    the metadata, so the migration state survives — this resets the data,
    never the schema.
    """
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    db.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def is_empty(db) -> bool:
    return db.query(User).count() == 0 and db.query(RoleQuota).count() == 0


def seed(db) -> None:
    ph = PasswordHasher()

    # --- users, one per role ------------------------------------------
    # Hashed here rather than through core/security.py, which does not
    # exist yet: argon2 encodes its parameters into the hash string, so
    # PasswordHasher().verify() accepts these regardless of the settings
    # A later chooses. This does not block on A's Deadline 2 column.
    student = User(
        name="Seed Student",
        email="student@iitk.ac.in",
        password_hash=ph.hash(SEED_PASSWORD),
        role=Role.STUDENT,
    )
    faculty = User(
        name="Seed Faculty",
        email="faculty@iitk.ac.in",
        password_hash=ph.hash(SEED_PASSWORD),
        role=Role.FACULTY,
    )
    admin = User(
        name="Seed Admin",
        email="admin@iitk.ac.in",
        password_hash=ph.hash(SEED_PASSWORD),
        role=Role.ADMIN,
    )
    db.add_all([student, faculty, admin])
    # Needed before the offering: instructor_id is NOT NULL and there is
    # no relationship configured, so the id has to exist to be assigned.
    db.flush()

    # --- resources ----------------------------------------------------
    # Room and GPUCluster are joined-table inheritance: constructing one
    # writes BOTH the `resources` row and the subclass row, and sets
    # resource_type from polymorphic_identity. Never hand-write
    # `resources` here.
    cluster_a = GPUCluster(name="GPU Cluster A", gpu_count=8, allocated=0)
    cluster_b = GPUCluster(name="GPU Cluster B", gpu_count=4, allocated=0)
    room_101 = Room(name="LHC-101", building="Lecture Hall Complex", capacity=60)
    room_202 = Room(name="RM-202", building="Faculty Building", capacity=30)
    db.add_all([cluster_a, cluster_b, room_101, room_202])

    # --- catalogue + section ------------------------------------------
    course = Course(code="CS641", name="Modern Cryptography")
    db.add(course)
    db.flush()

    # capacity and instructor_id belong to the OFFERING, not the course
    # (revision 268c10da1da4). Times are zero-padded "HH:MM" strings —
    # "9:00" would break lexicographic comparison in the Deadline 4
    # schedule-overlap check.
    offering = CourseOffering(
        course_id=course.id,
        instructor_id=faculty.id,
        semester="AUTUMN",
        year=2026,
        start_time="09:00",
        end_time="10:30",
        days="MWF",
        capacity=50,
        enrolled_count=0,
    )
    db.add(offering)

    # --- quota policy -------------------------------------------------
    # NULL means unlimited, which is why ADMIN is None and not a large
    # number. (FACULTY, COURSE) is deliberately ABSENT rather than set:
    # course registration is STUDENT-only, so the pair is unreachable
    # behind the 403. That makes "no row" and "row with max_units = NULL"
    # two different things, and the quota helper must not conflate them.
    quotas = [
        (Role.STUDENT, ResourceType.GPU, 2),
        (Role.FACULTY, ResourceType.GPU, 10),
        (Role.ADMIN, ResourceType.GPU, None),
        (Role.STUDENT, ResourceType.ROOM, 2),
        (Role.FACULTY, ResourceType.ROOM, 5),
        (Role.ADMIN, ResourceType.ROOM, None),
        (Role.STUDENT, ResourceType.COURSE, 6),
        (Role.ADMIN, ResourceType.COURSE, None),
    ]
    db.add_all(
        [
            RoleQuota(role=role, resource_type=rtype, max_units=units)
            for role, rtype, units in quotas
        ]
    )

    db.commit()

    print("seeded:")
    for u in (student, faculty, admin):
        print(f"  user    {u.id}  {u.email:<24} {u.role.value}")
    for c in (cluster_a, cluster_b):
        print(f"  gpu     {c.id}  {c.name:<24} {c.gpu_count} units, {c.allocated} allocated")
    for r in (room_101, room_202):
        print(f"  room    {r.id}  {r.name:<24} {r.building}, seats {r.capacity}")
    print(f"  course  {course.id}  {course.code:<24} {course.name}")
    print(
        f"  offer   {offering.id}  {offering.semester} {offering.year:<19}"
        f" {offering.days} {offering.start_time}-{offering.end_time},"
        f" capacity {offering.capacity}"
    )
    print(f"  quotas  {len(quotas)} rows  ((FACULTY, COURSE) intentionally absent)")
    print(f"\npassword for all three accounts: {SEED_PASSWORD}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="TRUNCATE every table first. Required if the database is not empty.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.reset:
            wipe(db)
        elif not is_empty(db):
            print(
                "database is not empty — refusing to seed.\n"
                "re-run with --reset to truncate every table first.",
                file=sys.stderr,
            )
            return 1
        seed(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
