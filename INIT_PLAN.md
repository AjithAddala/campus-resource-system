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

## 1. Project Overview

The Campus Resource Allocation System provides a unified backend for
three campus resource-management problems, governed by two independent
classes of allocation rule:

-   **Capacity rules** — a resource cannot be allocated beyond its total
    capacity.
-   **Entitlement rules** — a user cannot hold more than their role is
    permitted (per-role quotas).

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

The central problem is **concurrent resource allocation under two
simultaneous invariants**: a capacity invariant keyed on the *resource*,
and an entitlement (quota) invariant keyed on the *user*.

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
-   **SQLAlchemy 2.0**
-   **Alembic**

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
-   Argon2 or bcrypt for password hashing
-   Role claim embedded in the access token
-   Role-based dependencies for endpoint authorization

## Testing

-   Pytest
-   HTTPX
-   Concurrent integration tests

## Load Testing

-   Locust

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
enrolled_count <= course_capacity
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

## Two locking targets

This system enforces two invariants that live on **different keys**:

``` text
Capacity invariant   → keyed on the resource   → lock the resource row
Quota invariant      → keyed on the user       → lock the user quota row
```

A single allocation must satisfy both, so it acquires **two** locks. To
avoid deadlock, the lock acquisition order is fixed globally:

``` text
1. Lock the user quota row   (SELECT ... FOR UPDATE)
2. Lock the resource row     (SELECT ... FOR UPDATE)
3. Check capacity and quota
4. Insert reservation / update counters
5. COMMIT
```

Because every allocation path acquires locks in this same order, no two
transactions can hold one lock while waiting for the other in the
opposite order, so cyclic deadlocks cannot form.

## Row-Level Locking

For capacity-based resources, a transaction locks the relevant resource
row before checking and modifying allocation.

Conceptually:

``` sql
BEGIN;

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

INSERT INTO reservations (...);

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
room_id
user_id
start_time
end_time
status
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
Type: NVIDIA A6000
Total: 8
```

A reservation contains:

``` text
gpu_cluster_id
user_id
gpu_count
start_time
end_time
status
```

The system calculates the available capacity for the requested time
interval.

The two invariants that must hold simultaneously are:

``` text
Capacity:  sum(active allocations on cluster) <= total GPU capacity
Quota:     sum(active GPU units held by user) <= role quota for GPU
```

The allocation operation runs inside a single database transaction that
first locks the user quota row, then the cluster row, so neither
concurrent capacity nor concurrent quota violations are possible.

------------------------------------------------------------------------

# 9. Course Registration Strategy

Courses have:

``` text
course_id
course_code
name
capacity
start_time
end_time
days
```

Student registration creates an enrollment.

The critical invariant is:

``` text
number_of_active_enrollments <= course_capacity
```

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

------------------------------------------------------------------------

# 11. Database Model

A simplified schema is:

``` text
User
----
id
name
email
password_hash
role                -- ENUM: STUDENT | FACULTY | ADMIN

Resource
--------
id
type
name
location
status              -- e.g. AVAILABLE | BLOCKED (admin-controlled)

Room
----
id
resource_id
building
room_number

GPUCluster
----------
id
resource_id
gpu_type
gpu_count
allocated           -- running count of active GPU units

Course
------
id
code
name
capacity

CourseOffering
--------------
id
course_id
semester
year
start_time
end_time
days

Reservation
-----------
id
resource_id
user_id
start_time
end_time
status
created_at

GPUReservation
--------------
id
gpu_cluster_id
user_id
gpu_count
start_time
end_time
status

Enrollment
----------
id
student_id
course_offering_id
status
created_at

WaitlistEntry
-------------
id
student_id
course_offering_id
position
created_at

RoleQuota                       -- NEW: per-role entitlement policy
---------
id
role                -- ENUM: STUDENT | FACULTY | ADMIN
resource_type       -- ENUM: GPU | ROOM | COURSE
max_units           -- e.g. 2 for (STUDENT, GPU); NULL = unlimited
UNIQUE (role, resource_type)
```

Notes:

-   `role` is a PostgreSQL `ENUM` (or a `VARCHAR` with a `CHECK`
    constraint) so invalid roles cannot be stored.
-   `RoleQuota` is a small, admin-editable policy table. A `NULL`
    `max_units` means "no limit" (used for `ADMIN`).
-   The MVP computes a user's currently held units by aggregating active
    reservations under a user-row lock — there is deliberately **no
    denormalized per-user counter**, which avoids counter drift. A
    denormalized `quota_account(user_id, resource_type, units_held)` row
    with a `CHECK` constraint is a documented later optimization, to be
    introduced only if load testing shows contention on the user row
    (see Section 14).

The exact schema may be simplified or adjusted during implementation.

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

## Courses

``` text
POST   /api/v1/courses                        [ADMIN | FACULTY]
GET    /api/v1/courses                         [any authenticated]
GET    /api/v1/courses/{course_id}             [any authenticated]

POST   /api/v1/courses/{course_id}/register    [STUDENT]
DELETE /api/v1/courses/{course_id}/drop        [STUDENT]

GET    /api/v1/courses/{course_id}/waitlist    [any authenticated]
```

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
Reserve room                          ✓         ✓        ✓
Reserve GPU capacity                  ✓         ✓        ✓
Register for a course                 ✓         —        —
Create a course                       —         ✓        ✓
Create / modify / block a resource    —         —        ✓
Modify resource availability          —         —        ✓
Configure per-role quotas             —         —        ✓
```

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

-   Passwords are never stored directly; they are hashed with Argon2 (or
    bcrypt).
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

"Active" means reservations that are currently allocated (and, for
time-bounded resources, whose interval has not ended). Releasing or
cancelling a reservation frees the quota immediately.

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

-- Serialize all of this user's quota-bearing allocations
SELECT id FROM users
WHERE id = :user_id
FOR UPDATE;

-- Now the held-count read is protected
SELECT COALESCE(SUM(gpu_count), 0)
FROM gpu_reservations
WHERE user_id = :user_id
  AND status = 'ACTIVE';        -- = current held units

-- if held + requested > role_quota → reject (409 Conflict)

-- Capacity gate on the resource row (see Section 6)
SELECT * FROM gpu_clusters WHERE id = :cluster_id FOR UPDATE;
-- ... capacity check + update + insert ...

COMMIT;
```

Now T1 and T2 contend on the same user row. T2 blocks until T1 commits,
re-reads held units as 2, computes `2 + 2 = 4 > 2`, and is rejected.

## Lock ordering (deadlock avoidance)

Because a single allocation holds both the user lock and the resource
lock, the acquisition order is fixed globally: **user quota row first,
then resource row.** Every code path follows this order, so no cyclic
wait can form. (See Section 6.)

## Scope for the MVP

-   **GPU quota is enforced rigorously** — it is the flagship because
    GPU units are finite and integer-valued, making the race clean to
    demonstrate and benchmark.
-   **Room and course quotas use the identical mechanism**
    (`RoleQuota` row + user-row lock + aggregate check). Rooms cap
    concurrent active reservations; courses cap active enrollments.
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
│   ├── quotas/                -- NEW: per-role limit policy + checks
│   │   ├── router.py          -- admin quota configuration
│   │   ├── service.py         -- quota lookup + enforcement helper
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
│   ├── models/
│   └── core/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── concurrency/           -- capacity, quota, and RBAC tests
│
├── alembic/
│
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

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
2 → FAILURE
```

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

Locust will be used to simulate concurrent users.

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
-   Capacity-based reservations
-   Transactions
-   Row-level locking
-   **Per-role GPU quota (RoleQuota table, user-row lock, quota check)**
-   Concurrent allocation tests (capacity **and** quota)

## Phase 4 — Course Registration

-   Course creation
-   Registration
-   Capacity enforcement
-   Duplicate registration prevention
-   Schedule conflict checking
-   Per-role course-load quota

## Phase 5 — Testing and Benchmarking

-   Concurrent tests
-   Locust load tests
-   Performance measurements
-   Correctness verification (capacity, quota, authorization)

## Optional Phase 6

If time remains:

-   Waitlists
-   Reservation expiration
-   Denormalized quota counter (only if benchmarks justify it)
-   Redis
-   Celery
-   Notifications
-   Prometheus/Grafana

Optional features should not delay completion of the core system.

------------------------------------------------------------------------

# 19. 15-Day Development Plan

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

Concurrency testing (capacity, quota, RBAC) and Locust load testing.

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
For every time interval:
allocated_gpu_capacity <= total_gpu_capacity
```

### Course

``` text
For every course offering:
active_enrollments <= course_capacity
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

### Authorization

``` text
An action may only be performed by a role permitted to perform it;
all others are rejected with 403 before any state change.
```

### Waitlist

``` text
Promotion follows waitlist priority (and respects the quota invariant).
```

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
          under two invariants
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
> registration using FastAPI and PostgreSQL. It enforces two classes of
> invariant under concurrent load: resource-capacity limits (keyed on
> the resource) and per-role user quotas (keyed on the user), using
> database transactions, row-level locking with a fixed lock ordering,
> and constraints. Access is governed by JWT-based role-based
> authorization (Student / Faculty / Admin), and correctness is verified
> with concurrent integration tests and Locust load tests.

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
✓ Concurrent requests do not violate any invariant
✓ Automated tests verify correctness (capacity, quota, RBAC)
✓ Locust measures system performance
✓ Entire backend can run using Docker Compose
```

The primary success criterion is:

> **No allocation or entitlement invariant is violated, even under
> concurrent requests.**
