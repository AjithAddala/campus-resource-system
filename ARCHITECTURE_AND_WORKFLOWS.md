# Campus Resource Allocation System — Architecture & User Workflows

The system enforces **three classes of correctness guarantee**, each
keyed differently and therefore each requiring its own serialization
point:

``` text
1. Capacity     — a resource is never allocated beyond its total    (keyed on RESOURCE)
2. Quota        — a user never holds more than their role permits   (keyed on USER)
3. Exactly-once — a retried request never allocates twice           (keyed on REQUEST)
```

------------------------------------------------------------------------

# Part I — Architecture

## 1. Layered View

``` text
┌─────────────────────────────────────────────────────────┐
│  CLIENT                                                  │
│  Swagger UI / HTTP / asyncio + httpx harness             │
│  Sends: Authorization: Bearer <JWT>                      │
│         Idempotency-Key: <uuid>   (mutations only)       │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│  API LAYER — FastAPI routers                             │
│                                                          │
│  Dependency chain, evaluated in order:                   │
│    get_current_user()   → authentication  (401)          │
│    require_role(...)    → authorization   (403)          │
│    Pydantic schema      → validation      (422)          │
│                                                          │
│  Nothing here touches business state.                    │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│  SERVICE LAYER — domain logic, transaction boundary      │
│                                                          │
│  Owns the single allocation transaction:                 │
│    idempotency → quota → capacity → write → commit       │
│                                                          │
│  All three guarantees are enforced HERE, inside one tx.  │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│  PERSISTENCE — SQLAlchemy 2.0                            │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│  PostgreSQL — source of truth                            │
│                                                          │
│  Row locks (FOR UPDATE)   Unique constraints             │
│  Exclusion constraints    Check constraints              │
│  ACID transactions        Indexes                        │
└─────────────────────────────────────────────────────────┘
```

**Layer rule:** authorization is a *boundary* concern (API layer);
capacity, quota, and exactly-once are *correctness invariants* (service
+ database layer). A 403 can be decided from a token alone; an invariant
can only be decided under a lock.

## 2. Module Map

``` text
app/
├── main.py
├── core/
│   ├── security.py       JWT encode/decode, Argon2 hashing
│   ├── dependencies.py   get_current_user, require_role
│   └── config.py
├── auth/                 register, login
├── users/                GET /me  (Deadline 3); GET /me/quota at Deadline 6
├── quotas/               RoleQuota policy + enforcement helper
├── idempotency/          key storage + replay helper
├── rooms/                interval reservations
├── gpus/                 capacity + quota flagship
├── courses/              enrollment, waitlist
├── reservations/         shared cancel/list
├── models/               SQLAlchemy ORM
└── database/             session, base
```

`quotas/` and `idempotency/` are **helpers called from inside other
modules' transactions**, not standalone services. They never open their
own transaction — this is what makes the guarantees atomic.

## 3. Data Model

``` text
User
  id, name, email, password_hash, created_at
  role            ENUM(STUDENT, FACULTY, ADMIN)

-- All created_at are timestamptz. UNIQUE on users.email, courses.code.
-- Indexed for the hot paths: gpu_reservations(user_id, status) for the
-- quota SUM, enrollments(course_offering_id, status) for the roster,
-- waitlist_entries(course_offering_id, created_at) for promotion.

RoleQuota                      -- admin-editable policy, NOT per-user
  role            ENUM(STUDENT, FACULTY, ADMIN)
  resource_type   ENUM(GPU, ROOM, COURSE)
  max_units       INT NULL     -- NULL = unlimited
  UNIQUE (role, resource_type)

IdempotencyKey                 -- exactly-once
  key             TEXT
  user_id         FK → User
  endpoint        TEXT
  request_hash    TEXT         -- detects key reuse w/ different body
  response_body   JSONB
  status_code     INT
  created_at
  UNIQUE (key, user_id)

Resource
  id, name
  resource_type   ENUM(GPU, ROOM, COURSE)    -- polymorphic discriminator
  status          ENUM(AVAILABLE, BLOCKED)   -- admin-controlled

Room            id → Resource.id, building, capacity
                CHECK (capacity > 0)
GPUCluster      id → Resource.id, gpu_count, allocated
                CHECK (allocated >= 0 AND allocated <= gpu_count)

-- Joined-table inheritance: rooms.id and gpu_clusters.id ARE resources.id,
-- not a separate resource_id column.
-- The original design also had Resource.location, Room.room_number, and
-- GPUCluster.gpu_type. RESOLVED: dropped from the design, not added to the
-- models. They are labels, carry no invariant, and nothing in the system
-- reads them. INIT_PLAN.md §11 has been corrected to match.

Course          id, code, name
CourseOffering  id, course_id → Course, instructor_id → User,
                semester, year, start_time, end_time, days,
                capacity, enrolled_count

Reservation     id, resource_id → Resource, user_id → User,
                start_time, end_time, status, created_at
GPUReservation  id, gpu_cluster_id → GPUCluster, user_id → User,
                gpu_count, status, created_at
                -- NO start_time/end_time. Hold-until-release; see
                -- DECISIONS.md. Do not re-add them.
Enrollment      id, student_id → User, course_offering_id → CourseOffering,
                status, created_at
                UNIQUE (student_id, course_offering_id) -- unconditional, so a
                -- dropped student STILL OWNS A ROW. Re-registration and
                -- waitlist promotion must therefore be UPDATEs, not INSERTs.
WaitlistEntry   id, student_id → User, course_offering_id → CourseOffering,
                created_at                 -- FIFO order; no stored position
                UNIQUE (student_id, course_offering_id)
```

Note: `RoleQuota` has **no foreign key to User**. It is policy keyed on
`(role, resource_type)`, resolved via the user's role at request time.

### Database-level constraints (defense in depth)

``` sql
-- capacity can never go negative or exceed total
ALTER TABLE gpu_clusters
  ADD CONSTRAINT gpu_capacity_sane
  CHECK (allocated >= 0 AND allocated <= gpu_count);

-- same invariant for course seats
ALTER TABLE course_offerings
  ADD CONSTRAINT offering_enrollment_sane
  CHECK (enrolled_count >= 0 AND enrolled_count <= capacity);

-- no duplicate enrollment, ever
ALTER TABLE enrollments
  ADD CONSTRAINT enrollment_unique
  UNIQUE (student_id, course_offering_id);

-- no overlapping room reservations, enforced by Postgres itself
ALTER TABLE reservations
  ADD CONSTRAINT room_no_overlap
  EXCLUDE USING gist (
    resource_id WITH =,
    tstzrange(start_time, end_time, '[)') WITH &&
  ) WHERE (status = 'ACTIVE');
```

These make the invariants true even if application code has a bug. The
service layer produces clean HTTP errors; the constraints are the
backstop.

## 4. The Three Serialization Points

``` text
Guarantee       Keyed on    Mechanism                     Failure if absent
──────────────────────────────────────────────────────────────────────────────
Exactly-once    REQUEST     UNIQUE(key, user_id) insert   retry double-books
Quota           USER        SELECT users FOR UPDATE       user exceeds cap
Capacity        RESOURCE    SELECT cluster FOR UPDATE     overbooking
```

Each row is a different bug that the other two do **not** prevent.

## 5. Lock Ordering (deadlock avoidance)

A single GPU allocation acquires two row locks. Order is fixed globally:

``` text
1. IdempotencyKey insert   (unique constraint = serialization point)
2. User row                FOR UPDATE     ← quota gate
3. Resource row            FOR UPDATE     ← capacity gate
4. Write reservation + update counters
5. COMMIT  (key + allocation commit together, atomically)
```

Because **every** allocation path acquires locks in this order, no cyclic
wait can form, so deadlock is structurally impossible. Violating this
order anywhere in the codebase reintroduces deadlock risk.

## 6. The Canonical Allocation Transaction

``` sql
BEGIN;                                    -- wraps ALL of the below

-- (1) EXACTLY-ONCE: fails on retry, and serializes simultaneous retries
INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash)
VALUES (:key, :uid, 'gpu.reserve', :hash);
-- unique violation  → replay stored response, return early

-- (2) QUOTA GATE: lock the USER, then read their held units
SELECT id FROM users WHERE id = :uid FOR UPDATE;

SELECT COALESCE(SUM(gpu_count), 0) INTO held
FROM gpu_reservations
WHERE user_id = :uid AND status = 'ACTIVE';

-- if held + :requested > role_quota  → ROLLBACK, 409 QUOTA_EXCEEDED

-- (3) CAPACITY GATE: lock the RESOURCE
SELECT * FROM gpu_clusters WHERE id = :cid FOR UPDATE;

-- if allocated + :requested > gpu_count → ROLLBACK, 409 CAPACITY_EXHAUSTED

-- (4) WRITE
UPDATE gpu_clusters SET allocated = allocated + :requested WHERE id = :cid;
INSERT INTO gpu_reservations (...);
UPDATE idempotency_keys SET response_body = :resp, status_code = 201
WHERE key = :key AND user_id = :uid;

COMMIT;
```

**The load-bearing detail:** the idempotency key and the allocation
commit in the *same* transaction. Split them and you move the bug rather
than fix it — commit the allocation but lose the key and a retry
double-allocates; store the key but roll back the allocation and a retry
returns a fake success.

**Measured at Deadline 5, and the numbers correct the claim above.** Both
broken builds were built and run against `scripts/check_idempotency.py`
before the correct one was kept:

``` text
build                          201    409    500   rows/trial  seq. failures
------------------------------------------------------------------------
correct (savepoint, one tx)    120      0      0       1            0
check-then-insert, no savepoint  34      0     86       1            1
key commits separately           22     98      0       1            2
                              (120 requests: 8 simultaneous retries x 15 trials)
```

**No build over-allocated.** `UNIQUE(key, user_id)` holds the row count
to exactly one in all three — the database was never going to let a
double-booking through. What the correct implementation buys is not
uniqueness, it is that the *retry gets the original response*: without
the SAVEPOINT the unique violation aborts the transaction and 86 of 120
retries become a `500`; with a split commit the key is visible with a
NULL response and 98 of 120 become a spurious `409`. Stating the claim as
"idempotency prevents double-booking" would be overclaiming against our
own measurements.

**Why aggregation, not a counter:** held units are recomputed by `SUM`
under the user lock, so they cannot drift out of sync with reality. A
denormalized counter is a later optimization, introduced only if load
testing proves the user-row lock is a bottleneck.

## 7. Error Semantics

``` text
401  missing / invalid / expired JWT
403  authenticated but role not permitted
404  resource does not exist
409  CAPACITY_EXHAUSTED     capacity exhausted
409  QUOTA_EXCEEDED         role quota exceeded
409  INTERVAL_CONFLICT      room slot overlaps an active reservation
409  RESOURCE_BLOCKED       admin has blocked this resource
409  ALREADY_ENROLLED       you already hold this seat
409  SCHEDULE_CONFLICT      clashes with an enrollment you hold
409  NOT_ENROLLED           drop called with no enrollment
409  ALREADY_WAITLISTED     you are already queued for this offering
409  NOT_WAITLISTED         leave called with no waitlist entry
409  OFFERING_NOT_FULL      seats remain -- register, do not queue
409  QUOTA_NOT_CONFIGURED   no policy row for (role, resource) -- fails closed
409  IDEMPOTENCY_IN_PROGRESS  claim committed, response not yet recorded
409  CAPACITY_BELOW_ALLOCATED admin shrink below units currently held
409  COURSE_CODE_TAKEN      that course code is already in the catalogue
409  INSTRUCTOR_NOT_FACULTY an offering must be taught by a FACULTY account
422  malformed request body
422  IDEMPOTENCY_KEY_REUSED same key, different request
201  idempotent replay      (the ORIGINAL response, the ORIGINAL status)
```

Capacity and quota rejection are both `409` but carry distinct
machine-readable codes, because the caller's remedy differs: wait / try
another cluster, vs. release something you already hold.

**The replay line was contradictory until Deadline 5 and is now
settled.** This table said `200`; §14 said `200/201`. A replay returns
**the status that was stored**, which for a successful GPU reservation is
`201` — a replay is supposed to be indistinguishable from the original
call, and a client that branches on `201` would take a different path on
the retry, which is the exact bug idempotency exists to remove.

`IDEMPOTENCY_IN_PROGRESS` is new at Deadline 5 and should be unreachable:
a concurrent retry either blocks on the unique index (the first
transaction is still open) or reads a fully populated row (it committed).
It exists so that a future caller who forgets `record_response` gets a
clear `409` instead of a replay of `null` surfacing as a `500` three
layers away.

### The envelope carrying those codes

Settled at Deadline 2, when duplicate registration became the first
endpoint to return a coded error. Every coded failure is built by
`app/core/errors.py::coded_error()` and arrives as:

``` json
{"detail": {"code": "QUOTA_EXCEEDED", "message": "human-readable prose"}}
```

`code` is the contract a client may branch on; `message` may be reworded
at any time. Uncoded errors — a plain 404, the 401 from a bad token, the
403 from `require_role` — keep FastAPI's default string `detail`, because
there is nothing for a caller to distinguish: there is one remedy for a
401 (get a token) and one for a 403 (stop).

Two details of the 401, settled at Deadline 3: every 401 is
**byte-identical** whatever went wrong — expired, tampered, malformed
subject, or a signed token naming a deleted user — because which one it
was is not the caller's business and distinguishing them hands an
attacker a probe. And it carries `WWW-Authenticate: Bearer`, which is
what makes it a spec-conforming 401 rather than a 401-shaped 403. Registration adds one code to the Deadline 1
table:

``` text
409  EMAIL_ALREADY_REGISTERED    that email already has an account
```

Deadline 4 adds four more. Three are the course path's refusals
(`ALREADY_ENROLLED`, `SCHEDULE_CONFLICT`, `NOT_ENROLLED`), and the fourth
is `QUOTA_NOT_CONFIGURED`, which **fails closed**: a missing `RoleQuota`
row is not the same thing as `max_units = NULL`. NULL is a policy that
says unlimited; no row is no policy at all, and treating it as unlimited
would let one un-seeded row silently disable the quota system while every
test still passed.

Deadline 3 adds two more, both on `POST /rooms/{id}/reservations` — which
is precisely why they are coded. Two 409s on one endpoint, distinguishable
only by the remedy:

``` text
409  INTERVAL_CONFLICT    that slot is taken       -> pick another slot
409  RESOURCE_BLOCKED     the room is blocked      -> pick another room
```

`RESOURCE_BLOCKED` is outstanding item 6, ratified at Deadline 3:
blocking stops **new** allocations and does **not** evict existing ones,
matching the capacity-reduction rule in §13. The gate is checked inside
the transaction against the row it just locked, never at the boundary,
because `status` is mutable — an admin can flip it between a boundary read
and the write. The database enforces none of this: the GiST constraint is
partial on the *reservation's* status, not the *resource's*, so the
service layer is the whole guarantee.

401 and 403 stay **uncoded**, with FastAPI's plain string `detail`: there
is one remedy for a 401 (get a token) and one for a 403 (stop), so a code
would carry nothing the status line does not.

**The catalogue endpoints add the last two**, after the plan closed —
`POST /courses` and `POST /offerings` had belonged to no deadline and did
not exist. Both follow the rule this section already states:

``` text
409  COURSE_CODE_TAKEN       that code is in the catalogue -> pick another
409  INSTRUCTOR_NOT_FACULTY  the id resolves, the role is wrong
```

`COURSE_CODE_TAKEN` is a **caught** `IntegrityError` on
`ix_courses_code`, never a pre-read — the same reasoning that made
`EMAIL_ALREADY_REGISTERED` a caught error at Deadline 2. Two admins
posting one code can both pass a "does this code exist?" check and only
one can insert, so a pre-check converts a clean `409` into a `500`
precisely when it is contended. Asserted rather than argued:
`check_catalog.py` fires eight barrier-released creates at one code and
requires exactly one `201`, seven `409`, and zero `5xx`.

The **404/409 split on `POST /offerings` is the same distinction** drawn
above. An unknown `course_id` or `instructor_id` is a plain, uncoded
`404`: the id names nothing and there is one remedy. A user who exists
and is a STUDENT is a coded `409`: the request is well-formed, the id
resolves, and the refusal is policy — which is exactly the case a code
exists to make branchable. Note also that both ids are resolved
explicitly before the insert rather than left to the foreign keys, since
one `ForeignKeyViolation` cannot say *which* of the two was wrong.

------------------------------------------------------------------------

# Part II — User Workflows

## 8. Role Capability Matrix

``` text
Action                              STUDENT   FACULTY   ADMIN
──────────────────────────────────────────────────────────────
Register / login                       ✓         ✓        ✓
View resources & availability          ✓         ✓        ✓
View own quota & usage                 ✓         ✓        ✓
Reserve a room                         ✓         ✓        ✓
Reserve GPU capacity                   ✓         ✓        ✓
Cancel own reservation                 ✓         ✓        ✓
Register for a course                  ✓         ✗        ✗
Join / leave a course waitlist         ✓         ✗        ✗
View a course waitlist                 ✓         ✓        ✓
Create a course                        ✗         ✓        ✓
Create a resource                      ✗         ✗        ✓
Modify resource availability/status    ✗         ✗        ✓
Configure per-role quotas              ✗         ✗        ✓
Cancel ANY user's reservation          ✗         ✗        ✓
```

### Default quotas (seeded, admin-editable)

``` text
Resource   STUDENT   FACULTY   ADMIN
─────────────────────────────────────────
GPU           2         10     unlimited
ROOM          2          5     unlimited
COURSE        6          —     unlimited
```

## 9. Workflow A — Onboarding (all roles)

``` text
POST /api/v1/auth/register     JSON: name, email, password, role
                               password → Argon2id hash
                               role stored as ENUM
                               duplicate email → 409, from the UNIQUE
                                 constraint, not a pre-flight check
        │
        ▼
POST /api/v1/auth/login        FORM-ENCODED, not JSON: username, password
                               → JWT { sub: "user_id", role: STUDENT, iat, exp }
        │
        ▼
All subsequent calls:          Authorization: Bearer <JWT>
```

Two interface facts that are easy to get wrong and expensive to discover
late:

- **Login takes a form body, and the email travels in a field named
  `username`.** It is the OAuth2 password flow, which is what gives
  `/docs` a working *Authorize* button. Registration is JSON; only this
  one route is form-encoded. See `DECISIONS.md`.
- **`sub` is a string**, `"1"` and not `1`. PyJWT ≥ 2.10 enforces that on
  *decode*, so an int subject issues a valid-looking token and then 401s
  every later request — a bug in the issuer that only ever shows up in
  the consumer.

The role travels in the token — but **authorization reads it from the
database row, not from the claim** (changed at Deadline 3; see
`DECISIONS.md`). The lookup the claim was meant to save had already been
spent: `get_current_user` returns a `User`, so the row is loaded on every
authenticated request regardless, and by the time `require_role` runs
there is nothing left to avoid. Given both copies, the fresher one wins,
so a role change takes effect on the caller's next request rather than at
token expiry.

The database is therefore the source of truth for *state* **and** for
entitlement; the token is the source of *identity*, and its role claim is
the demonstrable half of the auth story rather than the enforced one.

## 10. Workflow B — Student reserves a GPU (flagship path)

This path exercises all three guarantees at once.

``` text
GET /api/v1/me/quota
    → { GPU: { limit: 2, held: 0, available: 2 } }

GET /api/v1/gpus/1/availability
    → { total: 8, allocated: 6, free: 2 }

POST /api/v1/gpus/1/reservations
Headers: Authorization: Bearer <JWT>
         Idempotency-Key: 7f3a-...
Body:    { gpu_count: 2 }        -- no times: hold-until-release
```

Server-side sequence:

``` text
  get_current_user      valid JWT?                → else 401
  require_role(...)     STUDENT permitted?        → else 403
  Pydantic              gpu_count >= 1?           → else 422
        │
        ▼  BEGIN TRANSACTION
  ① insert idempotency key      duplicate? → replay stored response
  ② LOCK user row               ─┐
     held = SUM(active) = 0      │ quota gate
     0 + 2 <= 2  ✓               ─┘
  ③ LOCK cluster row            ─┐
     allocated 6 + 2 <= 8  ✓     │ capacity gate
                                 ─┘
  ④ allocated → 8
     INSERT gpu_reservation
     store response in key row
     COMMIT
        │
        ▼
  201 Created  { reservation_id, gpu_count: 2, status: ACTIVE }
```

### Same student retries the identical call (network timeout)

``` text
  ① insert idempotency key → UNIQUE violation
        → read stored response, return 201 with SAME reservation_id
        → held stays 2, allocated stays 8       ✓ exactly-once
```

### Same student, a genuinely different request

``` text
POST /api/v1/gpus/2/reservations  { gpu_count: 2 }   (different cluster!)
  ② LOCK user row → held = 2 → 2 + 2 = 4 > 2
        → 409 QUOTA_EXCEEDED                          ✓ quota holds
```

This second rejection is invisible to cluster-level locking — different
cluster, no contention. Only the user-row lock catches it.

## 11. Workflow C — Release and re-acquire

``` text
DELETE /api/v1/reservations/{id}
   ownership check: caller is owner OR ADMIN   → else 403
   BEGIN
     LOCK user row
     LOCK cluster row
     reservation.status → CANCELLED
     cluster.allocated  → allocated - gpu_count
   COMMIT

   → held recomputed as 0; student may allocate again
```

The path is `DELETE /gpus/{gpu_id}/reservations/{id}` — it mirrors the
POST, because `DELETE /reservations/{id}` named a row in two tables with
separate id sequences (outstanding item 8, ratified at Deadline 4). Note
the user lock is taken on the reservation's **owner**, not the caller: an
ADMIN cancelling someone else's hold must serialize against that user's
allocations.

Cancellation is **naturally idempotent**: it flips a status flag, and
flipping an already-`CANCELLED` row is a no-op. This is a direct benefit
of aggregation over a denormalized counter — with a counter, a repeated
cancel would double-decrement and require its own guard.

## 12. Workflow D — Student registers for a course

``` text
POST /api/v1/offerings/{id}/register      [STUDENT only → else 403]

  BEGIN
    LOCK user row
      active enrollments = 5, quota 6      ✓ course-load quota
      no schedule overlap with existing    ✓ else 409
    LOCK course_offering row
      enrolled_count 49 < capacity 50      ✓ else 409 CAPACITY_EXHAUSTED
    UPSERT enrollment                      ← UNIQUE(student, offering)
                                             blocks duplicates; a student
                                             who dropped still owns a row,
                                             so this is an UPDATE not an
                                             INSERT (see §3)
    enrolled_count → 50                    ← same transaction, always
  COMMIT
  → 201
```

**The route is keyed on the offering, not the course.** Capacity,
`enrolled_count`, and the locked row all live on `course_offerings`, and
one course has many offerings — so `/courses/{id}/register` would have no
single row to lock, which is the entire mechanism. Read paths stay
course-shaped (`GET /courses`, `GET /courses/{id}/offerings`); write paths
are offering-shaped.

**Measured at Deadline 5** — 500 registrations submitted simultaneously
at a 50-seat offering, 40 in flight, 3 trials per build:

``` text
build                      201      409 CAPACITY   enrolled_count   active rows   oversold
--------------------------------------------------------------------------------------------
no offering lock       377 - 500     0 - 123          24 - 50        377 - 500      3/3
+ SELECT ... FOR UPDATE       50           450              50             50      0/3
```

Two trials of the unlocked build seated **all 500 students in a 50-seat
section**. The locked build sold exactly 50 every time, with the counter
and the row count agreeing.

**"500 concurrent" is 500 submitted, 40 in flight, and the distinction is
not pedantry.** A connection is checked out during dependency resolution
(`get_current_user` reads the user row) and held by the Session until the
request ends, so in-flight requests *are* connections — and Postgres
allows 100. Firing 500 unbounded returns 440 of them as `500`, with the
whole pool sitting `idle in transaction`. Serving 500 truly at once would
need a Postgres sized for 500 backends. The contention that matters is at
the seat gate, and 40-way is enough to oversell the unlocked build
tenfold.

Also measured at Deadline 4 in miniature — 20 concurrent registrations for 5 seats gave exactly 5, with
`enrolled_count` and the active-row count agreeing; Deadline 5's harness
scales it.

**The route also locks the student's user row, before the offering row.**
The plan had this transaction offering-lock-only, with the course-load
quota arriving at Deadline 6. But the schedule-overlap check reads the
student's *other* enrollments, and two concurrent registrations for two
clashing offerings share no offering row — so nothing would serialize
them and both would pass. A schedule clash is a fact about the **student**,
exactly like a quota, so it is guarded by the user lock. Measured: 0 of 15
concurrent clashing-registration trials produced a double-booking.

Course registration gets **deduplication for free** from the unique
constraint, but not full idempotency — a retry returns a duplicate error
rather than the original success. That is an accepted, documented
trade-off; only GPU allocation carries idempotency keys.

> **CORRECTED at Deadline 7, when the waitlist endpoints were written.**
> This block used to read `enrolled_count < capacity ✓ else → waitlist`,
> which made `POST /register` auto-enrol *or* auto-queue depending on a
> number the caller could not see. Outstanding item 10 settled it the
> other way: **joining is explicit**, and a full offering returns
> `409 CAPACITY_EXHAUSTED` and writes nothing. See Workflow F.
>
> The argument that decided it is the one Deadline 5 already made about
> the replay status: a `201` from this route would have meant either "you
> have a seat" or "you are queued", and a client branching on the status
> code would take the wrong branch. `409 CAPACITY_EXHAUSTED` keeps
> meaning exactly one thing. `scripts/check_waitlist.py` had been
> asserting the explicit behaviour since before the endpoints existed.

## 12b. Workflow F — Student queues, and is promoted

The fourth concurrency problem, and the only one whose two halves are
written by different people: B owns the endpoints below, A owns the
promotion transaction that consumes their rows.

``` text
POST /api/v1/offerings/{id}/waitlist       [STUDENT only → else 403]

  BEGIN
    LOCK user row            FOR UPDATE   ← a seat and a place in the
      already ACTIVE here?   → 409 ALREADY_ENROLLED    queue are mutually
      already queued here?   → 409 ALREADY_WAITLISTED  exclusive states
    LOCK offering row        FOR SHARE    ← read-only gate; see below
      enrolled_count < capacity → 409 OFFERING_NOT_FULL
    INSERT waitlist_entries
  COMMIT
  → 201  { id, student_id, course_offering_id, created_at, position }

DELETE /api/v1/offerings/{id}/waitlist     [STUDENT only]
  same two locks → DELETE the row → 200 with the position that was held
  → 409 NOT_WAITLISTED when there is nothing to leave

GET  /api/v1/offerings/{id}/waitlist       [any authenticated role]
  no lock. position = ROW_NUMBER() OVER (ORDER BY created_at, id)
```

**The offering lock is `FOR SHARE`, not `FOR UPDATE`.** Joining reads
`enrolled_count` to decide fullness and never writes it — the same
distinction that made the room gate a share lock at Deadline 3, while the
GPU and registration gates take `FOR UPDATE` because they write a
counter. A share lock still excludes the writers, which is the entire
requirement: `register` and `drop` both take `FOR UPDATE`, so a seat can
neither appear nor vanish between the fullness check and the commit.

**Why the user row is locked at all.** Without it a student's concurrent
`register` and waitlist-join touch no common row — one writes an
enrollment, the other a waitlist entry — and the student ends up holding
a seat *and* queueing for it. Same shape as the cross-cluster GPU quota
race: the invariant is a fact about the **user**, and the user row is the
only thing both paths share.

**Queueing costs no course-load quota.** `held_course_enrollments` counts
ACTIVE enrollments, and a queued student has no enrollment row at all, so
a student at their cap of 6 may still queue. The quota is enforced at
*promotion* time, where the transaction skips a candidate who would
breach it. A waitlist entry holds nothing, so charging quota for one
would refuse a student something they have not yet received.

**Leaving deletes the row; dropping a seat does not.** An enrollment must
keep a `DROPPED` row because `enrollment_unique` is unconditional and
re-registration is therefore an UPDATE. A waitlist entry carries no
history anyone reads, and its absence *is* the state — a `LEFT` status
would have to be excluded from every FIFO read, and one forgotten
exclusion would promote a student who had left.

### Position is computed, never stored

There is no `position` column; it was dropped in revision
`c86676652ca2`. Position is `ROW_NUMBER() OVER (ORDER BY created_at, id)`
evaluated at read time, which is what makes a promotion touch **exactly
one row** — the one it deletes. A stored position would have to be
renumbered for everyone behind the promoted student, and renumbering
transiently violates a unique constraint mid-UPDATE.

**The `id` tiebreak is the whole guarantee, not a formality.**
`func.now()` is transaction start time, so entries written inside one
transaction share a `created_at` to the microsecond and `created_at`
alone cannot express FIFO between them. `scripts/check_waitlist.py`
Part 1 proves this against the live database rather than asserting it.

### Promotion (A's column) — the FIFO promise is *oldest eligible*

``` text
DELETE /api/v1/offerings/{id}/drop        → frees a seat
  BEGIN                                     (already holding user + offering)
    for each entry, ORDER BY created_at, id:
        try LOCK user(candidate) FOR UPDATE SKIP LOCKED
        not acquired           → skip to the next entry
        course-load quota full → skip to the next entry
        schedule clash         → skip to the next entry
        otherwise              → promote exactly one, DELETE its row, stop
  COMMIT
```

**The seat moves; it is never released and re-taken.** The `-1` for the
drop and the `+1` for the promotion happen before a single commit, so
there is no instant at which the freed seat is visible to an ordinary
`register`. A queued student cannot lose their place to someone
refreshing the page.

**The schedule-clash skip is an addition to the ratified proposal**
(B, Deadline 7). Item 9 named only the quota. Without it, promotion can
seat a student in a class that clashes with one they already hold —
which `register` refuses outright — so the same illegal state would be
reachable through a different door. Both are facts about the student,
both guarded by the same user lock.

**Every skip is logged.** A passed-over student changes no row at all, so
without a log line they lose their turn with no record anywhere.

**`SKIP LOCKED` is why this cannot deadlock.** Promotion needs a second
user row it cannot name until it has read the waitlist, and reading the
waitlist needs the offering lock — so its order is offering → user while
registration's is user → offering. Rather than order the wait, the design
removes it: a transaction that never blocks on a user row cannot appear
in a cycle at all. Outstanding item 9.

The cost is stated rather than hidden: **the promise is *oldest
eligible*, not *oldest***. A queued student who is concurrently doing
something else is passed over, and nothing about their row changes when
that happens.

## 13. Workflow E — Admin manages resources and policy

``` text
POST  /api/v1/gpus                     [ADMIN]  create cluster
PATCH /api/v1/gpus/{id}                [ADMIN]  change capacity / status
PATCH /api/v1/rooms/{id}               [ADMIN]  block room for maintenance
PUT   /api/v1/admin/quotas/STUDENT/GPU [ADMIN]  { max_units: 3 }
```

Any non-admin token is rejected with `403` **before the handler runs** —
no partial state change is possible.

**Capacity-reduction rule:** existing reservations are *not* retroactively
evicted when an admin lowers a cluster's capacity. The new limit applies
only to future allocations; `allocated` drains as reservations are
released.

> **CORRECTED at Deadline 6, when the endpoint that does this was first
> written.** The original wording gave the example *"lowers a cluster from
> 8 to 4 while 6 are allocated"* — and that state cannot be stored.
> `gpu_capacity_sane` is `allocated >= 0 AND allocated <= gpu_count`, so
> Postgres rejects `gpu_count = 4` with `allocated = 6`, and a literal
> implementation would reach the caller as a 500 from a CHECK violation.
> This document described a database state the schema forbids, and had
> since Deadline 1; nothing read the two against each other until
> `PATCH /gpus/{id}` existed to try.
>
> **`PATCH /gpus/{id}` refuses the reduction with
> `409 CAPACITY_BELOW_ALLOCATED`**, decided against the row it holds
> `FOR UPDATE`. The CHECK stays, because it is what makes a locking bug in
> the flagship transaction fail loudly rather than quietly overselling a
> cluster. The rule's intent is unchanged — nothing is ever evicted — and
> an admin shrinks to `allocated` now and further as holds are released.
> Only the original example is unavailable. See DECISIONS.md, Deadline 6.

**Quota-change rule:** raising a quota takes effect immediately.
Lowering it below a user's current holding does not evict — the user
simply cannot acquire more until they drop below the new limit.

## 14. Failure-Path Summary

``` text
Scenario                          Result
────────────────────────────────────────────────────────────
No token                          401
Student calls POST /gpus          403, no state change
Cluster full                      409 CAPACITY_EXHAUSTED
Student already holds 2 GPUs      409 QUOTA_EXCEEDED
Student already holds 2 rooms     409 QUOTA_EXCEEDED     (Deadline 6)
Student at their course load      409 QUOTA_EXCEEDED     (Deadline 6)
Admin shrinks GPUs below held     409 CAPACITY_BELOW_ALLOCATED, nothing evicted
Admin lowers a role quota         existing holds survive; next request refused
Retried request (same key)        201 replay of the STORED response
Same key, different request       422 IDEMPOTENCY_KEY_REUSED
Retry of a request that FAILED    allocates for real -- the key rolled
                                  back with it, so there is nothing to
                                  replay. Exactly-once covers SUCCESSES.
Overlapping room booking          409 INTERVAL_CONFLICT (exclusion constraint)
Booking a BLOCKED room            409 RESOURCE_BLOCKED, existing holds kept
Booking a GPU cluster via /rooms  404 (service-layer resource_type check;
                                       the FK alone would accept it)
Duplicate course registration     409 (unique constraint)
Two concurrent identical retries  one commits, one blocks then replays
```
