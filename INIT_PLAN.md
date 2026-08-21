# Campus Resource Allocation System

A concurrency-safe backend system for managing scarce campus resources
such as **rooms, GPU compute capacity, and course seats**.

The project is designed around a core systems problem:

> **How can a backend allocate limited resources correctly when many
> users make requests concurrently, without overbooking, violating
> allocation rules, or exceeding per-user entitlements?**

The system uses **FastAPI and PostgreSQL** as its core technologies and
focuses on transactional correctness, concurrency control, role-based
access, and load testing rather than building a large frontend.

------------------------------------------------------------------------

> ## Status of this document
>
> This is the **original project proposal**. It states the problem, the
> motivation, and the success criteria, and those are still current.
>
> Its schema, API, and plan sections have been **synced to the schema at
> head** (revision `1ca8b85b7626`) so they cannot be read as instructions
> to rebuild something that was deliberately removed. Where the built
> system diverges from the original design, the change is marked inline
> with **`CHANGED:`** and a pointer to the revision and to `DECISIONS.md`,
> which carries the reasoning.
>
> Three divergences are large enough to state here, because each one is a
> thing the original design says to build and the current system says not
> to:
>
> 1. **GPU reservations have no `start_time` / `end_time`.** They are
>    hold-until-release. A scalar `allocated` counter cannot answer an
>    interval question, and the two do not compose. Do not re-add them.
> 2. **A third correctness guarantee was added: exactly-once**, keyed on
>    the request via `UNIQUE(key, user_id)` on `idempotency_keys`. This
>    document was written when there were two invariants; there are three,
>    and the global lock order now begins with the idempotency key insert.
> 3. **Course seats belong to a `CourseOffering`, not a `Course`.**
>    Capacity, `enrolled_count`, and `instructor_id` all live on the
>    offering, which is also the row that registration locks.
>
> **Precedence when documents disagree:** the models and migrations are
> the truth; then `DECISIONS.md` (why, and what was reversed); then
> `ARCHITECTURE_AND_WORKFLOWS.md` (what the system is now); then
> `WORK_LOG.md` and `EXECUTION_PLAN.md`; then this file.

------------------------------------------------------------------------

## 1. Project Overview

The Campus Resource Allocation System provides a unified backend for
three campus resource-management problems, governed by three independent
classes of allocation rule:

-   **Capacity rules** — a resource cannot be allocated beyond its total
    capacity. *Keyed on the resource.*
-   **Entitlement rules** — a user cannot hold more than their role is
    permitted (per-role quotas). *Keyed on the user.*
-   **Exactly-once rules** — a retried request must not allocate twice.
    *Keyed on the request.*

### Room Reservations

Users can reserve rooms for a specific time interval.

The system must guarantee:

-   No two users can reserve the same room for overlapping intervals.
-   Adjacent reservations are allowed.
-   A cancelled reservation releases the time slot.

Example:

``` text
Room 101

10:00 ───────── 12:00   User A    ✓
11:00 ───────── 13:00   User B    ✗
12:00 ───────── 14:00   User C    ✓
```

### GPU Allocation

GPU clusters have a finite capacity.

For example:

``` text
GPU Cluster A
A6000 × 8
```

If users request:

``` text
User A → 3 GPUs
User B → 4 GPUs
User C → 2 GPUs
```

the system must allow A and B but reject C because only one GPU remains.

Capacity invariant:

``` text
allocated_gpus <= total_gpus
```

GPUs are additionally the **flagship resource for per-role quotas**: a
student may hold at most 2 GPU units at once, a faculty member at most
10. This limit is enforced independently of, and simultaneously with,
the capacity invariant.

### Course Registration

Courses have a fixed seat capacity.

For example:

``` text
CS641
Capacity = 50
```

If 500 students register concurrently, the system must ensure:

``` text
Successful registrations = 50
Over-allocation = 0
```

Additional rules include:

-   A student cannot register for the same course twice.
-   A student cannot register for courses with overlapping schedules.
-   A student cannot exceed the per-role course-load quota.
-   A waitlist can optionally be used when a course is full.

------------------------------------------------------------------------

# 2. Main Engineering Goal

This is not primarily a CRUD application.

The central problem is **concurrent resource allocation under three
simultaneous invariants**: a capacity invariant keyed on the *resource*,
an entitlement (quota) invariant keyed on the *user*, and an exactly-once
invariant keyed on the *request*.

Each is keyed on something different, so guarding one does nothing for
the others — that is the claim the whole project rests on.

A naïve implementation might do:

``` text
1. Check whether resource is available.
2. If available, create booking.
```

Under concurrency, this can fail:

``` text
              Resource capacity = 1

User A ───────┐
              ├── Check availability → AVAILABLE
User B ───────┘
              └── Check availability → AVAILABLE

User A → BOOK
User B → BOOK

Result:
allocated = 2
capacity  = 1

❌ OVER-ALLOCATION
```

The identical failure mode exists for quotas, but on a different key:

``` text
              Student GPU quota = 2

Req 1 (cluster X, 2 GPUs) ──┐
                            ├── held = 0 → 0 + 2 ≤ 2  OK
Req 2 (cluster Y, 2 GPUs) ──┘
                            └── held = 0 → 0 + 2 ≤ 2  OK

Both commit → held = 4

❌ QUOTA VIOLATION
```

Crucially, the two requests above lock **different** cluster rows, so a
resource-row lock never serializes them. The quota invariant needs its
own serialization point (see Section 14).

The system uses PostgreSQL transactions, row-level locking, and database
constraints to make allocation operations atomic and safe against both
failure modes.

------------------------------------------------------------------------

# 3. Architecture

The first version uses a **modular monolith** rather than microservices.

``` text
                         ┌───────────────────┐
                         │      Client       │
                         │  Swagger / HTTP   │
                         └─────────┬─────────┘
                                   │
                                   ▼
                         ┌───────────────────┐
                         │     FastAPI       │
                         │                   │
                         │  Authentication   │
                         │  Authorization    │
                         │  (RBAC + quotas)  │
                         │  Rooms            │
                         │  GPUs             │
                         │  Courses          │
                         │  Reservations     │
                         │  Waitlists        │
                         └─────────┬─────────┘
                                   │
                                   ▼
                         ┌───────────────────┐
                         │    SQLAlchemy     │
                         │      2.0          │
                         └─────────┬─────────┘
                                   │
                                   ▼
                         ┌───────────────────┐
                         │    PostgreSQL     │
                         │                   │
                         │ Transactions      │
                         │ Row Locks         │
                         │ Constraints       │
                         │ Indexes           │
                         └───────────────────┘
```

The database is the **source of truth** for resource availability,
reservations, and quota accounting.

Authorization (who may call an endpoint) is enforced at the FastAPI
boundary. Entitlement (how much a user may hold) is a correctness
invariant and is therefore enforced inside the database transaction, not
only in application code.

------------------------------------------------------------------------

# 4. Technology Stack

## Backend

-   **Python**
-   **FastAPI**
-   **Pydantic**
-   **SQLAlchemy 2.0 — sync, with psycopg. Not async.**
-   **Alembic**

**CHANGED — sync, deliberately.** `SELECT ... FOR UPDATE` semantics are
identical either way, and the only place real concurrency is needed is
the *test harness*, which is asyncio + httpx on the client side and
unaffected by a sync server. An async server would buy throughput we are
not measuring and double the debugging surface on the one thing we are.
Do not convert the server to async.

## Database

-   **PostgreSQL**

PostgreSQL is responsible for:

-   ACID transactions
-   Row-level locking
-   Unique constraints
-   Foreign keys
-   Check constraints
-   Time-range conflict prevention
-   Indexing

## Authentication and Authorization

-   JWT
-   OAuth2 password flow
-   **Argon2** for password hashing (chosen over bcrypt)
-   Role claim embedded in the access token
-   Role-based dependencies for endpoint authorization

The role travels in the token, so authorization needs no database
round-trip per request — which matters when 500 concurrent requests must
not add user-lookup noise to lock measurements. Accepted tradeoff: a role
change does not take effect until the token expires (60 minutes).

> **CHANGED at Deadline 3 — `require_role` reads the role from the
> database row, not from the claim.** The round-trip these paragraphs
> avoid was already being made: `get_current_user() -> User`, frozen at
> Deadline 1, loads the user on every authenticated request, so by the
> time `require_role` runs the row is in hand and there is no lookup left
> to save. With both copies available the fresher one wins, and the
> stale-role window is zero rather than the token's 60 minutes. The token
> is still the source of identity; it is just not what entitlement is
> read from. See `DECISIONS.md`, "The role is read from the database, not
> the claim".

## Testing

-   Pytest
-   HTTPX
-   Concurrent integration tests

## Load Testing

-   ~~Locust~~ — **cut on Deadline 1**; see Section 17. The asyncio + httpx
    concurrency harness carries the correctness claim.

## Development / Deployment

-   Docker
-   Docker Compose

### Optional future technologies

These are deliberately not part of the initial MVP:

-   Redis
-   Celery
-   Prometheus
-   Grafana
-   Kafka

They can be introduced later only when there is a genuine architectural
requirement.

------------------------------------------------------------------------

# 5. Why PostgreSQL?

PostgreSQL is central to this project because correctness is more
important than simply storing data.

Consider a course with 50 seats and hundreds of simultaneous
registration requests.

The backend must guarantee:

``` text
course_offerings.enrolled_count <= course_offerings.capacity
```

Similarly:

``` text
allocated_gpu_count <= gpu_capacity
```

and:

``` text
No overlapping reservations for the same room
```

and the per-user entitlement:

``` text
user_active_units(resource_type) <= role_quota(user.role, resource_type)
```

These guarantees can all be enforced using PostgreSQL's transactional
and constraint mechanisms.

------------------------------------------------------------------------

# 6. Concurrency Control

## Three serialization points

> **CHANGED:** this section originally described two invariants. A third —
> exactly-once — was added with the `idempotency_keys` table. See
> `DECISIONS.md`.

This system enforces three invariants that live on **different keys**:

``` text
Capacity invariant     → keyed on the resource → lock the resource row
Quota invariant        → keyed on the user     → lock the user row
Exactly-once invariant → keyed on the request  → UNIQUE(key, user_id) insert
```

Guarding one does nothing for the others, because each is a fact about a
different thing. A single allocation must satisfy all three, so it takes
two row locks plus a unique-constraint serialization point. To avoid
deadlock, the acquisition order is fixed globally:

``` text
1. INSERT the idempotency key  (UNIQUE violation → replay stored response)
2. Lock the user row           (SELECT ... FOR UPDATE)
3. Lock the resource row       (SELECT ... FOR UPDATE)
4. Check quota, then capacity
5. Insert reservation / update counters / store the response on the key row
6. COMMIT                       (key and allocation commit together)
```

The key insert and the allocation must land in the **same** transaction.
Split them and the bug moves rather than disappearing: commit the
allocation but lose the key and a retry double-books; store the key but
roll back the allocation and a retry returns a fake success.

Because every allocation path acquires locks in this same order, no two
transactions can hold one lock while waiting for the other in the
opposite order, so cyclic deadlocks cannot form.

## Row-Level Locking

For capacity-based resources, a transaction locks the relevant resource
row before checking and modifying allocation.

Conceptually:

``` sql
BEGIN;

-- 0. Exactly-once gate (keyed on request)
INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash)
VALUES (:key, :user_id, 'gpu.reserve', :hash);
-- UNIQUE violation → replay the stored response, return early

-- 1. Quota gate (keyed on user)
SELECT id FROM users
WHERE id = :user_id
FOR UPDATE;

-- (aggregate current active GPU units for this user, compare to quota)

-- 2. Capacity gate (keyed on resource)
SELECT *
FROM gpu_clusters
WHERE id = :cluster_id
FOR UPDATE;

-- Check available capacity

UPDATE gpu_clusters
SET allocated = allocated + 2
WHERE id = :cluster_id;

INSERT INTO gpu_reservations (...);   -- CHANGED: was `reservations`;
                                      -- GPU holds live in their own table

UPDATE idempotency_keys
SET response_body = :resp, status_code = 201
WHERE key = :key AND user_id = :user_id;

COMMIT;
```

If another request attempts to allocate the same resource concurrently,
PostgreSQL makes it wait for the cluster lock. If another request tries
to allocate *for the same user*, it waits for the user lock — even if it
targets a completely different resource. This prevents both stale
capacity reads and stale quota reads.

------------------------------------------------------------------------

# 7. Room Reservation Strategy

Room reservations are interval-based.

A reservation contains:

``` text
resource_id         -- CHANGED: FK to resources.id, not a `room_id`.
                    -- Joined-table inheritance means rooms.id IS resources.id,
                    -- so a reservation points at a resource without needing to
                    -- know which kind it is. Nothing at the database level stops
                    -- it naming a GPU cluster — the service layer checks that.
user_id
start_time
end_time
status
created_at
```

A conflict exists when:

``` text
new_start < existing_end
AND
new_end > existing_start
```

For PostgreSQL, range types and exclusion constraints can be used to
enforce non-overlapping reservations at the database level.

Conceptually:

``` text
Room 101

Reservation A:
[10:00, 12:00)

Reservation B:
[11:00, 13:00)

Overlap → reject

Reservation C:
[12:00, 14:00)

No overlap → allow
```

Using half-open intervals `[start, end)` allows a reservation ending at
12:00 and another beginning at 12:00 to coexist.

Room reservations are also subject to a per-role quota (e.g. maximum
number of concurrent active reservations per user), enforced by the same
mechanism described in Section 14.

------------------------------------------------------------------------

# 8. GPU Allocation Strategy

GPU resources are modeled as capacity-based resources.

Example:

``` text
GPU Cluster
----------------
Total:     8       -- gpu_count
Allocated: 6       -- running count of active units
```

A reservation contains:

``` text
gpu_cluster_id
user_id
gpu_count
status
created_at
```

> **CHANGED — GPU reservations are hold-until-release.** The original
> design gave this table `start_time` and `end_time` *and* a scalar
> `allocated` counter on the cluster. Those do not compose. If a booking
> is time-bounded, capacity is a question about intervals — "at 3pm, how
> many are allocated?" — and one integer cannot answer it. Two
> non-overlapping 8-GPU bookings (10–12 and 14–16) should both succeed;
> the counter says `8 + 8 > 8` and wrongly rejects the second. Worse in
> the other direction: nothing decrements when an interval ends, so quota
> is held forever.
>
> Resolved by dropping the intervals, not the counter — it matches how
> quota is already defined ("concurrently held units"), and rooms still
> carry the interval story. A reservation is held until it is explicitly
> released. See `DECISIONS.md`. **Do not re-add the timestamps.**

The three invariants that must hold simultaneously are:

``` text
Capacity:      gpu_clusters.allocated <= gpu_clusters.gpu_count
Quota:         sum(active GPU units held by user) <= role quota for GPU
Exactly-once:  a retried request allocates at most once
```

The allocation runs inside a single database transaction that inserts the
idempotency key, then locks the user row, then the cluster row — so no
concurrent capacity violation, quota violation, or double-booking on
retry is possible. The capacity invariant additionally has a database
backstop:

``` sql
CHECK (allocated >= 0 AND allocated <= gpu_count)   -- gpu_capacity_sane
```

The row lock is the mechanism; the constraint makes a locking bug fail
loudly instead of silently overselling.

------------------------------------------------------------------------

# 9. Course Registration Strategy

> **CHANGED — seats belong to an offering, not to a course.** The
> original design put `capacity` and the schedule on `Course`. A course is
> the catalogue entry and is stable across semesters, so a capacity there
> says "CS101 has 50 seats, forever, in every section simultaneously,"
> which is not a thing. Revision `268c10da1da4` split them.

A course is split across two tables:

``` text
Course              -- catalogue entry, stable across semesters
------
id
code                -- UNIQUE
name

CourseOffering      -- one section, in one semester
--------------
id
course_id
instructor_id       -- the section has an owner, the catalogue entry does not
semester
year
start_time          -- "HH:MM", zero-padded
end_time            -- "HH:MM", zero-padded
days                -- e.g. "MWF"
capacity            -- seats belong HERE
enrolled_count      -- the counter registration locks
```

Student registration creates an enrollment **against an offering**.

The critical invariant is:

``` text
course_offerings.enrolled_count <= course_offerings.capacity
```

`enrolled_count` exists so that registration has a **row to lock**.
Counting `enrollments` instead would mean counting rows in a table that
concurrent registrations are inserting into — there is nothing for
`FOR UPDATE` to take, and two registrations can both read 49 against a
capacity of 50. Locking the offering row is what makes the 500-concurrent
test winnable.

It is therefore derived state, and the rule that keeps it honest is:
**every write to `enrollments` happens in the same transaction as the
matching `enrolled_count` update.**

Registration must also enforce:

``` text
student cannot enroll twice
```

and:

``` text
student cannot have overlapping courses
```

and:

``` text
student's active enrollments <= role course-load quota
```

If the course is full, the student can optionally be added to a
waitlist.

------------------------------------------------------------------------

# 10. Waitlist

The optional waitlist follows FIFO ordering.

Example:

``` text
Course capacity = 2

Enrolled:
Student A
Student B

Waitlist:
1. Student C
2. Student D
3. Student E
```

If Student A drops:

``` text
Student C → promoted
```

not Student D or Student E.

The waitlist invariant is:

> Promotion occurs according to waitlist priority.

Promotion must respect the promoted student's course-load quota; if a
promotion would exceed the quota, the next eligible student is promoted
instead. This feature should only be implemented after the core
registration and concurrency logic is stable.

> **CHANGED — the numbering above is a display value, not a column.**
> `waitlist_entries.position` was dropped in revision `c86676652ca2`.
> FIFO order comes from `ORDER BY created_at, id`, and a position is
> computed at read time with `ROW_NUMBER()`.
>
> Storing it meant that promoting the first entry rewrote every remaining
> position for that offering — a write-amplified O(n) UPDATE inside the
> promotion transaction, while holding the offering lock, which is the
> exact place where holding a lock longer costs the most. It also forced a
> deferrable unique constraint, because `SET position = position - 1`
> transiently collides mid-UPDATE. Dropping the column removed all of it:
> a promotion now touches one row, there is no renumbering, so there is
> nothing to defer.
>
> Because `enrollments` has an unconditional `UNIQUE(student_id,
> course_offering_id)`, **promotion must UPDATE an existing enrollment
> row, not INSERT one** — a promoted student who previously dropped still
> owns a row.

The waitlist is item 5 on the pre-agreed cut order in `EXECUTION_PLAN.md`,
and is cut whole rather than half if the schedule slips.

------------------------------------------------------------------------

# 11. Database Model

This is the schema **as built**, at revision `1ca8b85b7626` — 12 tables.
Every `created_at` is `timestamptz`.

``` text
User
----
id
name                -- CHANGED: `name`, not `full_name`
email               -- UNIQUE, indexed
password_hash       -- CHANGED: `password_hash`, not `hashed_password`
role                -- ENUM: STUDENT | FACULTY | ADMIN
created_at

Resource            -- base table for anything bookable
--------
id
name
resource_type       -- ENUM: GPU | ROOM | COURSE  (polymorphic discriminator)
status              -- ENUM: AVAILABLE | BLOCKED  (admin-controlled)
                    -- CHANGED: no `location` column

Room                -- joined-table inheritance: rooms.id IS resources.id
----
id                  -- CHANGED: FK to resources.id, NOT a `resource_id` column
building
capacity            -- CHECK (capacity > 0)
                    -- CHANGED: no `room_number` column

GPUCluster          -- joined-table inheritance: gpu_clusters.id IS resources.id
----------
id                  -- CHANGED: FK to resources.id, NOT a `resource_id` column
gpu_count
allocated           -- running count of active GPU units; THE locked counter
                    -- CHECK (allocated >= 0 AND allocated <= gpu_count)
                    -- CHANGED: no `gpu_type` column

Course              -- catalogue entry; deliberately holds NO capacity
------
id
code                -- UNIQUE
name

CourseOffering      -- one section, in one semester
--------------
id
course_id           -- FK → courses.id
instructor_id       -- FK → users.id, NOT NULL, indexed
semester
year
start_time          -- "HH:MM" string, zero-padded
end_time            -- "HH:MM" string, zero-padded
days                -- e.g. "MWF"
capacity            -- CHECK (capacity > 0)
enrolled_count      -- THE locked counter
                    -- CHECK (enrolled_count >= 0 AND enrolled_count <= capacity)

Reservation         -- ROOMS ONLY. Interval-based.
-----------
id
resource_id         -- FK → resources.id
user_id             -- FK → users.id
start_time          -- timestamptz
end_time            -- timestamptz
status              -- ENUM: ACTIVE | CANCELLED
created_at

GPUReservation      -- hold-until-release. NO start_time / end_time.
--------------
id
gpu_cluster_id      -- CHANGED: `gpu_cluster_id`, not `cluster_id`
user_id
gpu_count
status              -- ENUM: ACTIVE | CANCELLED
created_at

Enrollment
----------
id
student_id
course_offering_id
status              -- ENUM: ACTIVE | DROPPED | WAITLISTED
created_at
UNIQUE (student_id, course_offering_id)

WaitlistEntry       -- FIFO order is created_at; there is no stored position
-------------
id
student_id
course_offering_id
created_at          -- CHANGED: `position` column dropped
UNIQUE (student_id, course_offering_id)

RoleQuota                       -- per-role entitlement policy, NOT per-user
---------
id
role                -- ENUM: STUDENT | FACULTY | ADMIN
resource_type       -- ENUM: GPU | ROOM | COURSE
max_units           -- e.g. 2 for (STUDENT, GPU); NULL = unlimited
UNIQUE (role, resource_type)

IdempotencyKey                  -- NEW: the exactly-once guarantee
--------------
id
key
user_id             -- FK → users.id
endpoint
request_hash        -- detects key reuse with a different body
response_body       -- JSONB, NULLABLE — filled in at commit
status_code         -- NULLABLE — filled in at commit
created_at
UNIQUE (key, user_id)
```

## Database-level constraints (defense in depth)

``` sql
-- capacity can never go negative or exceed total
ALTER TABLE gpu_clusters ADD CONSTRAINT gpu_capacity_sane
  CHECK (allocated >= 0 AND allocated <= gpu_count);

-- same invariant for course seats
ALTER TABLE course_offerings ADD CONSTRAINT offering_enrollment_sane
  CHECK (enrolled_count >= 0 AND enrolled_count <= capacity);

-- no duplicate enrollment, ever
ALTER TABLE enrollments ADD CONSTRAINT enrollment_unique
  UNIQUE (student_id, course_offering_id);

-- no overlapping room reservations, enforced by Postgres itself
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE reservations ADD CONSTRAINT no_overlapping_room_reservations
  EXCLUDE USING gist (
    resource_id WITH =,
    tstzrange(start_time, end_time, '[)') WITH &&
  ) WHERE (status = 'ACTIVE');
```

`'[)'` is half-open — inclusive start, exclusive end — which is what
allows the back-to-back `[10,12)` and `[12,14)` bookings of Section 7.
The `WHERE (status = 'ACTIVE')` makes it partial, so a cancelled
reservation does not block rebooking the slot it released.

## Indexes

Each one is tied to a specific hot-path query, not added speculatively:

``` text
ix_gpu_reservations_user_status  (user_id, status)              the quota SUM, under the user lock
ix_enrollments_offering_status   (course_offering_id, status)   class roster, enrolled_count reconciliation
ix_waitlist_entries_offering_created (course_offering_id, created_at)  promotion query, under the offering lock
ix_reservations_user_id          (user_id)                      "my reservations"
ix_course_offerings_instructor_id                               a faculty member's sections
ix_users_email (UNIQUE) · ix_courses_code (UNIQUE) · ix_resources_resource_type
```

Notes:

-   All five vocabularies (`Role`, `ResourceType`, `ResourceStatus`,
    `ReservationStatus`, `EnrollmentStatus`) are **native PostgreSQL enum
    types**, so invalid values cannot be stored. Two accepted costs:
    adding a value later needs a migration, and `op.drop_table` does not
    drop the type, so every downgrade must `DROP TYPE` explicitly or the
    migration chain is not re-runnable.
-   `RoleQuota` has **no foreign key to User**. It is policy keyed on
    `(role, resource_type)`, resolved through the user's role at request
    time.
-   `IdempotencyKey.response_body` and `.status_code` are nullable, and
    that is load-bearing. The sequence is: INSERT the key with a NULL
    response → do the booking → UPDATE the key with the response →
    COMMIT. The claim is staked before any work begins; the response is
    filled in once it is known.
-   **Enrollment uniqueness is unconditional**, which has a real
    consequence: a student who dropped still owns a row, so
    **re-registration and waitlist promotion must be UPDATEs, not
    INSERTs.** A partial unique index (`WHERE status='ACTIVE'`) was
    considered and rejected — it allows clean inserts but lets duplicate
    DROPPED rows accumulate and loses the hard guarantee.
-   Held units are computed by aggregation under the user-row lock, so
    they cannot drift. There is deliberately **no denormalized per-user
    counter**. A `quota_account(user_id, resource_type, units_held)` row
    with a `CHECK` is a documented later optimization, introduced only if
    load testing shows contention on the user row (see Section 14).

------------------------------------------------------------------------

# 12. API Design

Example API structure. Endpoints are annotated with the minimum role
required to call them.

## Authentication

``` text
POST /api/v1/auth/register        [public]
POST /api/v1/auth/login           [public]
GET  /api/v1/me                   [any authenticated]
GET  /api/v1/me/quota             [any authenticated]  -- limits + current usage
```

## Rooms

``` text
POST   /api/v1/rooms                          [ADMIN]
PATCH  /api/v1/rooms/{room_id}                [ADMIN]  -- modify availability/status
GET    /api/v1/rooms                          [any authenticated]
GET    /api/v1/rooms/{room_id}                [any authenticated]
GET    /api/v1/rooms/{room_id}/availability   [any authenticated]

POST   /api/v1/rooms/{room_id}/reservations   [STUDENT | FACULTY | ADMIN]
DELETE /api/v1/reservations/{reservation_id}  [owner | ADMIN]
```

## GPUs

``` text
POST   /api/v1/gpus                           [ADMIN]
PATCH  /api/v1/gpus/{gpu_id}                   [ADMIN]  -- modify capacity/status
GET    /api/v1/gpus                           [any authenticated]
GET    /api/v1/gpus/{gpu_id}/availability     [any authenticated]

POST   /api/v1/gpus/{gpu_id}/reservations     [STUDENT | FACULTY | ADMIN]
DELETE /api/v1/reservations/{reservation_id}  [owner | ADMIN]
```

`POST /gpus/{gpu_id}/reservations` takes an `Idempotency-Key` header and
a body of `{ "gpu_count": N }` — **no times**, because GPU holds are
hold-until-release. It is the only endpoint carrying idempotency keys.

## Courses

> **CHANGED — course write paths are scoped to an offering, not a
> course.** Capacity, `enrolled_count`, and the row that registration
> locks all live on `course_offerings` (revision `268c10da1da4`), and one
> course has many offerings. A route keyed on `course_id` therefore has no
> single row to lock, which is the whole mechanism. Read paths stay on
> `courses`, since browsing a catalogue is genuinely course-shaped.

``` text
POST   /api/v1/courses                                  [ADMIN | FACULTY]
GET    /api/v1/courses                                  [any authenticated]
GET    /api/v1/courses/{course_id}                      [any authenticated]
GET    /api/v1/courses/{course_id}/offerings            [any authenticated]

POST   /api/v1/offerings                                [ADMIN | FACULTY]
GET    /api/v1/offerings/{offering_id}                  [any authenticated]

POST   /api/v1/offerings/{offering_id}/register         [STUDENT]
DELETE /api/v1/offerings/{offering_id}/drop             [STUDENT]

GET    /api/v1/offerings/{offering_id}/waitlist         [any authenticated]
POST   /api/v1/offerings/{offering_id}/waitlist         [STUDENT]
DELETE /api/v1/offerings/{offering_id}/waitlist         [STUDENT]
```

Waitlist position is a **display value**, computed at read time as
`ROW_NUMBER() OVER (ORDER BY created_at, id)`. It is never stored.

## Error semantics

``` text
401  missing / invalid / expired JWT
403  authenticated but role not permitted
404  resource does not exist
409  CAPACITY_EXHAUSTED      the resource is full
409  QUOTA_EXCEEDED          caller is at their personal limit
409  room interval conflict
409  duplicate enrollment
422  malformed request body
422  IDEMPOTENCY_KEY_REUSED  same key, different body
200  idempotent replay       (original response, original status)
```

The two `409`s carry **distinct machine-readable codes** because the
caller's remedy differs: capacity means wait or try another cluster,
quota means release something you already hold.

## Administration (quota policy)

``` text
GET  /api/v1/admin/quotas                      [ADMIN]
PUT  /api/v1/admin/quotas/{role}/{resource}    [ADMIN]  -- set max_units
```

FastAPI automatically provides interactive API documentation through:

``` text
/docs
```

Authorization is not documentation-only: each protected endpoint
declares its required role through a dependency (Section 13), so an
under-privileged caller receives `403 Forbidden` before any business
logic runs.

------------------------------------------------------------------------

# 13. Authentication and Authorization (RBAC)

This section separates two concepts that are frequently conflated:

``` text
Authentication  → Who are you?          (is the JWT valid?)
Authorization   → What may you do?       (does your role permit this?)
```

## Roles

The system has three roles, stored as an enum on the user:

``` text
STUDENT
FACULTY
ADMIN
```

The role is written into the JWT at login, so authorization checks do
not require a database round-trip on every request (the token is the
source of identity; the database remains the source of truth for state).

> **CHANGED at Deadline 3 — `require_role` reads the role from the
> database row, not from the claim.** The round-trip these paragraphs
> avoid was already being made: `get_current_user() -> User`, frozen at
> Deadline 1, loads the user on every authenticated request, so by the
> time `require_role` runs the row is in hand and there is no lookup left
> to save. With both copies available the fresher one wins, and the
> stale-role window is zero rather than the token's 60 minutes. The token
> is still the source of identity; it is just not what entitlement is
> read from. See `DECISIONS.md`, "The role is read from the database, not
> the claim".

## Enforcement via dependencies

Authorization is implemented as a reusable FastAPI dependency rather
than scattered `if user.role == ...` checks inside handlers. This keeps
the policy declarative and centralized.

Conceptually:

``` python
def require_role(*allowed: Role):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user
    return dependency

# usage
@router.post("/rooms")
def create_room(payload: RoomCreate,
                admin: User = Depends(require_role(Role.ADMIN))):
    ...
```

`get_current_user` performs authentication (decode + verify the JWT);
`require_role(...)` performs authorization. Endpoints compose them.

## Permission matrix

``` text
Action                              STUDENT   FACULTY   ADMIN
--------------------------------------------------------------
Register / login                      ✓         ✓        ✓
View resources & availability         ✓         ✓        ✓
View own quota & usage                ✓         ✓        ✓
Reserve a room                        ✓         ✓        ✓
Reserve GPU capacity                  ✓         ✓        ✓
Cancel own reservation                ✓         ✓        ✓
Register for a course                 ✓         —        —
Create a course / offering            —         ✓        ✓
Create a resource                     —         —        ✓
Modify resource availability/status   —         —        ✓
Configure per-role quotas             —         —        ✓
Cancel ANY user's reservation         —         —        ✓
```

### Default quotas (seeded, admin-editable)

``` text
Resource   STUDENT   FACULTY   ADMIN
─────────────────────────────────────────
GPU           2         10     unlimited
ROOM          2          5     unlimited
COURSE        6          —     unlimited
```

Unlimited is stored as `max_units = NULL`, not as a large number.

## Requested feature: admin-only resource modification

The rule *"only an ADMIN may modify resource availability"* is enforced
by attaching `require_role(Role.ADMIN)` to every resource-mutating
endpoint (`POST`, `PATCH`, `DELETE` on rooms, GPUs, and resource
status). A non-admin token is rejected with `403` before the handler
executes.

When an admin reduces a resource's capacity below its currently
allocated amount, existing reservations are **not** retroactively
evicted; the lower capacity applies only to new allocations. This keeps
capacity changes safe and predictable.

## Security notes

-   Passwords are never stored directly; they are hashed with Argon2.
-   Authorization is enforced at the API boundary, but entitlement
    limits (quotas) are additionally enforced inside the database
    transaction — see Section 14. This is defense in depth: the API
    layer decides *who may attempt* an action; the database guarantees
    the *invariant* even under concurrency.

------------------------------------------------------------------------

# 14. Per-Role Allocation Quotas

This is the second requested feature and the second correctness
invariant of the system.

## What a quota is

A quota is the maximum number of **concurrently held units** of a given
resource type that a user of a given role may hold at once.

Examples:

``` text
(STUDENT, GPU)  → 2
(FACULTY, GPU)  → 10
(ADMIN,   GPU)  → unlimited
```

Quotas are stored in the `RoleQuota` policy table and are editable by
admins at runtime; they are not hard-coded in the application.

## The invariant

For every user and resource type:

``` text
sum(user's active units for resource_type)
        <= role_quota(user.role, resource_type)
```

"Active" means `status = 'ACTIVE'` — nothing more. Releasing or
cancelling a reservation frees the quota immediately.

> **CHANGED:** this originally read "and, for time-bounded resources,
> whose interval has not ended." Nothing expires on a timer any more.
> GPU holds run until released, which is why `ReservationStatus` has
> exactly two values and there is no `EXPIRED` — a third state would
> force a decision about whether it counts toward quota, and an update to
> every quota query.

## Why a resource lock is not enough

The capacity invariant is protected by locking the resource row. The
quota invariant is keyed on the **user**, so two allocations by the same
user on **different** resources never contend on a shared lock:

``` text
Student GPU quota = 2

T1: reserve 2 GPUs on cluster X   → lock cluster X
T2: reserve 2 GPUs on cluster Y   → lock cluster Y   (no contention with T1)

T1 reads user's held units = 0    → 0 + 2 ≤ 2  OK
T2 reads user's held units = 0    → 0 + 2 ≤ 2  OK   (T1 not yet committed)

Both commit → student holds 4     ❌ quota violated
```

This is the same lost-update pattern as over-allocation, but the naive
"just check before insert" version fails even when the resource-level
locking is perfectly correct.

## The fix: lock the user quota row first

Every quota-bearing allocation acquires a serialization point on the
user before it checks the quota:

``` sql
BEGIN;

-- Exactly-once gate first (see Section 6)
INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash) VALUES (...);

-- Serialize all of this user's quota-bearing allocations
SELECT id FROM users
WHERE id = :user_id
FOR UPDATE;

-- Now the held-count read is protected
SELECT COALESCE(SUM(gpu_count), 0)
FROM gpu_reservations
WHERE user_id = :user_id
  AND status = 'ACTIVE';        -- = current held units

-- if held + requested > role_quota → reject (409 QUOTA_EXCEEDED)

-- Capacity gate on the resource row (see Section 6)
SELECT * FROM gpu_clusters WHERE id = :cluster_id FOR UPDATE;
-- ... capacity check + update + insert + store response on the key row ...

COMMIT;
```

That `SUM` runs inside the hottest transaction *while holding the user
lock*, so every millisecond it costs is a millisecond every other request
from that user spends blocked. `ix_gpu_reservations_user_status` exists
for exactly this query.

Now T1 and T2 contend on the same user row. T2 blocks until T1 commits,
re-reads held units as 2, computes `2 + 2 = 4 > 2`, and is rejected.

## Lock ordering (deadlock avoidance)

Because a single allocation holds both the user lock and the resource
lock, the acquisition order is fixed globally: **idempotency key insert,
then user row, then resource row.** Every code path follows this order —
allocation, cancellation, course registration, and waitlist promotion
alike — so no cyclic wait can form. (See Section 6.)

## Scope for the MVP

-   **GPU quota is enforced rigorously** — it is the flagship because
    GPU units are finite and integer-valued, making the race clean to
    demonstrate and benchmark.
-   **Room and course quotas use the identical mechanism**
    (`RoleQuota` row + user-row lock + aggregate check). Rooms cap
    concurrent active reservations; courses cap active enrollments.
    They are items 2 and 3 on the pre-agreed cut order — *because* the
    mechanism is identical, cutting them costs a demonstration and no
    understanding, so long as the README says so.
-   The quota check reuses the same transaction that already performs
    the capacity check, so it adds one lock and one aggregate query — no
    new infrastructure.

## Earned complexity (not in the MVP)

The MVP recomputes held units by aggregation, which cannot drift out of
sync with reality. If load testing later shows the user-row lock is a
bottleneck, a denormalized `quota_account(user_id, resource_type,
units_held)` row with a `CHECK (units_held <= max_units)` constraint can
replace the aggregate. This is a measured optimization, introduced only
if the benchmark justifies it — not an assumption.

------------------------------------------------------------------------

# 15. Project Structure

A proposed project structure:

``` text
campus-resource-system/
│
├── app/
│   ├── main.py
│   │
│   ├── auth/
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schemas.py
│   │
│   ├── core/
│   │   ├── security.py        -- JWT, password hashing
│   │   ├── dependencies.py    -- get_current_user, require_role
│   │   └── config.py
│   │
│   ├── users/
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schemas.py
│   │
│   ├── quotas/                -- per-role limit policy + enforcement helper
│   │   ├── router.py          -- admin quota configuration
│   │   ├── service.py         -- quota lookup + enforcement helper
│   │   └── schemas.py
│   │
│   ├── idempotency/           -- NEW: key storage + replay helper
│   │   ├── service.py
│   │   └── schemas.py
│   │
│   ├── rooms/
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schemas.py
│   │
│   ├── gpus/
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schemas.py
│   │
│   ├── courses/
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schemas.py
│   │
│   ├── reservations/
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schemas.py
│   │
│   ├── database/
│   │   ├── session.py
│   │   └── base.py
│   │
│   └── models/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── concurrency/           -- capacity, quota, exactly-once, RBAC
│
├── alembic/                   -- owned exclusively by one person; see below
│
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## Three kinds of file — know which you are writing

``` text
Transaction owners   gpus/service.py, courses/service.py,
                     reservations/service.py
                     -> these call BEGIN / COMMIT

Helpers              quotas/service.py, idempotency/service.py
                     -> called INSIDE another transaction.
                        They NEVER open their own.

Boundary             routers, dependencies.py, schemas
                     -> decide who may attempt. Touch no business state.
```

If a helper opens its own transaction, the quota check commits separately
from the booking and the guarantee evaporates. This is why `quotas/` and
`idempotency/` have no `router.py` of their own for the enforcement path —
they are called by the modules that own the transaction.

Routers know HTTP; services know the domain. Transactions are therefore
tested by calling services directly, never through HTTP.

------------------------------------------------------------------------

# 16. Testing Strategy

Testing is a major part of the project.

Normal API tests verify:

``` text
Request → Response
```

But this project also needs **correctness tests under concurrency**, now
across both invariants, plus authorization tests.

## Room test

``` text
Room 101
```

Send:

``` text
User A → 10:00–12:00
User B → 11:00–13:00
```

Expected:

``` text
A → SUCCESS
B → CONFLICT
```

## GPU capacity test

``` text
Capacity = 8
```

Concurrent requests:

``` text
3 GPUs
4 GPUs
2 GPUs
```

Expected:

``` text
3 → SUCCESS
4 → SUCCESS
2 → FAILURE          -- 409 CAPACITY_EXHAUSTED
```

> **CHANGED — fire this as FACULTY, not STUDENT.** A student's GPU quota
> is 2, so a 3-unit or 4-unit request is rejected with `QUOTA_EXCEEDED`
> before capacity is ever consulted, and the test would pass for entirely
> the wrong reason. Faculty hold 10. Keeping the two invariants separable
> in the harness is the point: this test must fail only on capacity.

## GPU quota test (per-role limit)

``` text
Student GPU quota = 2
Two clusters X and Y, each with free capacity
```

Fire concurrently, as the same student:

``` text
Req 1 → 2 GPUs on cluster X
Req 2 → 2 GPUs on cluster Y
```

Expected:

``` text
Exactly one succeeds
Student's held GPU units never exceeds 2
```

This is the test that fails on the naive version (both succeed → held =
4) and passes once the user-row lock is added. It is the primary
demonstration of the quota feature.

The fix is **not** "add a lock" — the resource lock was already there and
already correct. It was the **wrong lock for that invariant.**

## Exactly-once test (idempotency)

``` text
The same request, sent twice, with the same Idempotency-Key
```

Expected:

``` text
Without a key → 2 reservations          ❌
With a key    → 1 reservation, and the second call returns the
                 ORIGINAL response body and status code
```

And with the same key but a *different* body:

``` text
422 IDEMPOTENCY_KEY_REUSED
```

## Build the broken version first

Each of these has a "before" number, and it must be **measured, not
reconstructed.** Write `reserve_gpu()` without the user lock, run the
quota test, record `held = 4`. Then add the lock and re-run. Two numbers
side by side is the single most credible artifact the project produces;
a "broken build" recreated afterwards to make a table look good is
obvious and worthless.

## Authorization test (RBAC)

``` text
A STUDENT token calls POST /api/v1/rooms
```

Expected:

``` text
403 Forbidden, and no room is created
```

## Course test

``` text
Capacity = 50
```

Send 500 concurrent registration requests.

Expected:

``` text
Successful = 50
Rejected = 450
Over-allocation = 0
```

The exact result may vary if requests are intentionally randomized, but
every invariant must always hold.

------------------------------------------------------------------------

# 17. Load Testing

> **CHANGED — Locust is cut.** It is item 1 on the pre-agreed cut order
> in `EXECUTION_PLAN.md`, dropped on Deadline 1. The asyncio + httpx harness
> already proves correctness under concurrency, which is the claim the
> project makes. Throughput numbers we never optimise against invite the
> question "so what did you do with that?" with no answer.
>
> This section is retained as the design for load testing, and belongs in
> the README's **"Designed, not implemented"** section. A documented
> deferral reads as judgment; a missing feature reads as failure.

Locust would be used to simulate concurrent users.

Example scenarios:

``` text
Scenario 1:
100 users → same course

Scenario 2:
1,000 users → same course

Scenario 3:
1,000 users → same room

Scenario 4:
1,000 users → same GPU cluster

Scenario 5:
1 student → 1,000 concurrent GPU requests (quota stress)
```

Metrics:

``` text
Requests/second
p50 latency
p95 latency
p99 latency
Failure rate
Successful allocations
Allocation conflicts
Quota rejections
```

The most important measurement is not just latency.

It is:

> **Did the system remain correct under load — for both capacity and
> quota?**

------------------------------------------------------------------------

# 18. Project Milestones

## Phase 1 — Foundation

-   FastAPI setup
-   PostgreSQL setup
-   SQLAlchemy
-   Alembic
-   Docker
-   Authentication (JWT, password hashing)
-   **Roles + `require_role` authorization dependency (RBAC)**

## Phase 2 — Room Reservations

-   Room CRUD (admin-only mutation)
-   Availability
-   Reservation
-   Cancellation
-   Conflict prevention
-   Tests

## Phase 3 — GPU Allocation

-   GPU cluster management
-   Capacity-based reservations (hold-until-release, no intervals)
-   Transactions
-   Row-level locking
-   **Per-role GPU quota (RoleQuota table, user-row lock, quota check)**
-   **Exactly-once (IdempotencyKey, UNIQUE(key, user_id), replay path)**
-   Concurrent allocation tests (capacity, quota, **and** exactly-once)

## Phase 4 — Course Registration

-   Course creation
-   Registration
-   Capacity enforcement
-   Duplicate registration prevention
-   Schedule conflict checking
-   Per-role course-load quota

## Phase 5 — Testing and Benchmarking

-   Concurrent tests
-   ~~Locust load tests~~ (cut — Section 17)
-   Broken-vs-fixed measurements, recorded as they happen
-   Correctness verification (capacity, quota, exactly-once, authorization)

## Optional Phase 6

If time remains:

-   Waitlists
-   ~~Reservation expiration~~ — **do not build this.** It would
    reintroduce time-bounded GPU holds through the back door, and the
    scalar `allocated` counter cannot represent them (Section 8).
    Expiring a hold is a schema change, not a feature.
-   Denormalized quota counter (only if benchmarks justify it)
-   Redis
-   Celery
-   Notifications
-   Prometheus/Grafana

Optional features should not delay completion of the core system.

------------------------------------------------------------------------

# 19. 15-Day Development Plan

> **SUPERSEDED by `EXECUTION_PLAN.md`.** This was written for one person
> over 15 days. The project is being built by two people across 10
> **Deadlines**, which changes the ordering (rooms and courses run in
> parallel with the GPU core rather than after it) and adds an ownership
> split. Retained for the phase breakdown; use `EXECUTION_PLAN.md` for
> what has to be true at each Deadline.
>
> **The "Days" below are deliberately still called days.** They belong to
> this dead 15-day schedule and map onto nothing current — renaming them
> "Deadlines" would imply fifteen live milestones competing with the ten
> real ones. Everywhere else in the repo, a numbered stage is a Deadline;
> here, and only here, it is a leftover.

### Days 1–2

Architecture, database schema, FastAPI setup, PostgreSQL, SQLAlchemy,
authentication, and the RBAC dependency (`require_role`).

### Days 3–5

Room reservation module and concurrency-safe interval booking.
Admin-only resource mutation enforced.

### Days 6–8

GPU allocation and transaction/locking logic, including the per-role GPU
quota with the two-lock (user-then-resource) design.

### Days 9–11

Course registration, capacity enforcement, and course-load quota.

### Days 12–13

Concurrency testing (capacity, quota, exactly-once, RBAC). ~~Locust load
testing~~ — cut; see Section 17.

### Day 14

Docker, documentation, API cleanup, error handling.

### Day 15

Benchmarking, demo preparation, bug fixing, and final polishing.

------------------------------------------------------------------------

# 20. Design Principles

The project follows several important backend engineering principles.

### Database as the source of truth

Resource availability and quota usage are not trusted from the client.

``` text
Client
  ↓
FastAPI
  ↓
PostgreSQL
  ↓
Authoritative state
```

### Atomic allocation

Allocation, quota accounting, and reservation creation happen inside a
single transaction.

### Defense in depth

Important invariants are enforced at both:

``` text
Application layer  (authorization: who may attempt)
+
Database layer     (capacity + quota invariants under lock)
```

### Separation of authorization and entitlement

Authorization (role gate) is a boundary concern enforced by a
dependency. Entitlement (quota) is a correctness invariant enforced
transactionally. They are deliberately implemented in different layers.

### Modular monolith

The system is separated into domain modules without introducing
unnecessary microservices.

### Correctness before performance

The system must first guarantee:

``` text
No overbooking, no quota violation
```

before optimizing throughput.

------------------------------------------------------------------------

# 21. Future Architecture

If the MVP becomes successful, the system can evolve into:

``` text
                         API Gateway
                              │
              ┌───────────────┼───────────────┐
              │               │               │
         Room Service    Course Service   GPU Service
              │               │               │
              └───────────────┼───────────────┘
                              │
                         Event Queue
                              │
                    ┌─────────┴─────────┐
                    │                   │
               Notifications       Analytics
```

Possible additions include:

-   Redis caching
-   Celery workers
-   Kafka/event streaming
-   Notification service
-   Resource utilization analytics
-   Admin dashboard
-   Prometheus/Grafana
-   More sophisticated scheduling
-   Centralized policy/quota service

These are future extensions, not requirements for the initial 15-day
implementation.

------------------------------------------------------------------------

# 22. Key Correctness Invariants

The project should explicitly verify the following invariants.

### Room

``` text
For every room:
No two active reservations overlap.
```

### GPU

``` text
For every cluster:
gpu_clusters.allocated <= gpu_clusters.gpu_count
```

> **CHANGED:** this originally read "for every time interval." That is
> the invariant a scalar counter provably cannot enforce, and stating it
> that way is what made the original design incoherent — see Section 8.
> GPU holds are not time-bounded, so there is no interval to quantify
> over.

### Course

``` text
For every course offering:
course_offerings.enrolled_count <= course_offerings.capacity

and, reconciling derived state against the source of truth:
enrolled_count = COUNT(enrollments WHERE status = 'ACTIVE')
```

The second line is a real check to run after the 500-concurrent
benchmark, not a restatement of the first — `enrolled_count` is derived
state and can disagree with `enrollments` if any code path updates one
without the other:

``` sql
SELECT o.id, o.enrolled_count, COUNT(e.id) FILTER (WHERE e.status = 'ACTIVE')
FROM course_offerings o LEFT JOIN enrollments e ON e.course_offering_id = o.id
GROUP BY o.id
HAVING o.enrolled_count <> COUNT(e.id) FILTER (WHERE e.status = 'ACTIVE');
```

### Student

``` text
A student cannot have duplicate enrollment.
```

### Schedule

``` text
A student cannot register for two overlapping courses.
```

### Quota (per-role entitlement)

``` text
For every user and resource type:
active_units_held(user, resource_type)
        <= role_quota(user.role, resource_type)
```

### Exactly-once

``` text
For every (idempotency key, user):
at most one allocation is created, however many times it is retried.
```

### Authorization

``` text
An action may only be performed by a role permitted to perform it;
all others are rejected with 403 before any state change.
```

### Waitlist

``` text
Promotion follows waitlist priority (and respects the quota invariant).
FIFO order is ORDER BY created_at, id -- the id tiebreak is load-bearing.
```

`func.now()` returns the *transaction's* start timestamp, not the
statement's, so several entries written in one transaction share a
`created_at` and `ORDER BY created_at` alone leaves their order
undefined. This never happens on the real path — each join is its own
request and its own transaction — but it happens in a seed script that
inserts several entries at once, which would make the waitlist benchmark
flap for a reason that has nothing to do with locking.

These invariants form the core correctness specification of the system.

------------------------------------------------------------------------

# 23. Why This Project?

Traditional student booking applications primarily demonstrate:

``` text
CRUD
Authentication
Database operations
```

This project focuses on a harder problem:

``` text
             Many concurrent users
                     │
                     ▼
             Limited resources
             + per-role entitlements
                     │
                     ▼
          Concurrent allocation
         under three invariants
                     │
                     ▼
          Correctness guarantees
```

The project therefore provides practical experience with:

-   Backend API design
-   Relational database design
-   Transactions
-   Concurrency
-   Locking (multiple lock targets + ordering)
-   Database constraints
-   Authentication and role-based authorization
-   Per-user entitlement enforcement
-   Automated testing
-   Load testing
-   Performance analysis

------------------------------------------------------------------------

# 24. Placement Project Description

### Short version

> **Campus Resource Allocation System** — A concurrency-safe backend for
> managing room reservations, GPU capacity allocation, and course
> registration using FastAPI and PostgreSQL. It enforces three classes of
> invariant under concurrent load: resource-capacity limits (keyed on the
> resource), per-role user quotas (keyed on the user), and exactly-once
> request handling (keyed on the request), using database transactions,
> row-level locking with a fixed global lock ordering, and exclusion and
> unique constraints. Access is governed by JWT-based role-based
> authorization (Student / Faculty / Admin), and correctness is verified
> with a concurrent integration harness reporting measured
> broken-vs-fixed results.

### One-line description

> A concurrency-safe campus resource allocation backend that prevents
> both overbooking of rooms, GPUs, and course seats and violation of
> per-role usage quotas under concurrent requests, with role-based
> access control.

------------------------------------------------------------------------

# 25. Success Criteria

The project is considered successful if it can demonstrate:

``` text
✓ Users can authenticate
✓ Role-based authorization gates every protected endpoint
✓ Only admins can create or modify resources
✓ Rooms can be reserved
✓ Overlapping room bookings are rejected
✓ GPU capacity cannot be exceeded
✓ Per-role quotas cannot be exceeded, even under concurrency
✓ Course capacity cannot be exceeded
✓ Duplicate registrations are rejected
✓ Course schedule conflicts are detected
✓ A retried request allocates exactly once, and replays the original
  response
✓ Concurrent requests do not violate any invariant
✓ Automated tests verify correctness (capacity, quota, exactly-once, RBAC)
✓ Each of those has a broken-vs-fixed table with real measured numbers
✓ Entire backend can run using Docker Compose on a clean machine
```

> **CHANGED:** "Locust measures system performance" is removed — Locust
> is cut (Section 17). The two lines added in its place are the ones the
> project is actually judged on.

The primary success criterion is:

> **No allocation or entitlement invariant is violated, even under
> concurrent requests.**
