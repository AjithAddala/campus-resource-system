# Decisions

Format: what we tried, what we measured, what we chose — written as it
happens. This is the interview cheat-sheet; "we used pessimistic locking"
is a claim anyone can make, but *"we considered X, hit Y, so we chose Z"*
is impossible to reconstruct two months later.

Repo: https://github.com/AjithAddala/campus-resource-system
A = Ajith (correctness core) · B = Varshith (resources & verification)

---

## The claim this project makes

There is not one thing to guard. There are **three**, each keyed on
something different, and guarding one does nothing for the others.

| Rule | Is about | Serialize on |
|---|---|---|
| Cluster holds N GPUs | the **resource** | the cluster row |
| Student may hold 2 GPUs | the **user** | the user row |
| A retry must not book twice | the **request** | `UNIQUE(key, user_id)` |

One student, quota 2, fires two concurrent 2-GPU requests at *different*
clusters. Different rows, so the cluster locks never contend. Both
succeed. Student holds 4.

Nothing was overbooked. Both cluster locks worked perfectly. The rule
that broke — *a student may hold 2* — is a fact about the **student**,
and nothing locked the student.

> The lock was not missing. It was the **wrong lock for that invariant**.

**Lock ordering.** A GPU booking takes two locks, so deadlock is
possible. Fixed global rule: **user row first, resource row second.** If
A holds the user lock, B is stuck at step 1 and therefore cannot be
holding the resource lock — the cycle cannot form. This applies to
cancellation and course registration too. No exceptions, anywhere.

---

# Day 1

## Stack

**Sync SQLAlchemy 2.0 + psycopg, not async.** `SELECT FOR UPDATE`
semantics are identical either way. Concurrency lives in the *test
harness* (asyncio + httpx, client-side), which is unaffected by a sync
server. An async server would buy throughput we are not measuring and
double the debugging surface on the one thing we are.

**Three session settings in `app/database/session.py`** — these are the
difference between our skeleton and a tutorial's:

- `autocommit=False` — we control transaction boundaries. A lock is held
  until COMMIT; anything that commits on our behalf releases a lock we
  thought we still held.
- `autoflush=False` — SQLAlchemy will not silently emit INSERTs at
  surprising points. We are reasoning about statement order; statements
  must not move. `db.flush()` is called explicitly when a write must
  land early — needed on Day 5 so the idempotency key hits the unique
  index at a known point.
- `expire_on_commit=False` — objects stay readable after commit, so
  building a response does not trigger an extra SELECT.

**`get_db()` deliberately does not commit.** Many tutorials put
`db.commit()` in the `finally`. That would hide the transaction boundary
inside a dependency, far from the locks it governs, and would commit even
on paths where we meant to roll back. Commit belongs in the service
layer, on the line after the last write.

**Locust is out of scope.** The asyncio harness already proves
correctness, which is the claim. Throughput numbers we never optimise
against invite "so what did you do with that?"

## Ownership

**Alembic is owned exclusively by A.** Two people generating revisions
creates divergent `down_revision` chains — a half-day to unpick and we
have no buffer. B never runs `alembic revision`; schema changes go
through A.

**`reservations/service.py::cancel()` is owned by A**, even though
`reservations/` is otherwise B's. It is a locking transaction (user lock,
then cluster lock, then decrement `allocated`) and must obey the same
lock order as allocation. Reversing it in this one file would reintroduce
deadlock across the whole app. Settled Day 1 to avoid a Day 7 merge
conflict.

## Schema decisions (ratified before freeze)

**GPU reservations are hold-until-release.** `GPUReservation` has no
`start_time`/`end_time`.

The original spec had both timestamps *and* a scalar `allocated`
counter. Those do not compose. If reservations are time-bounded, capacity
is a question about intervals — "at 3pm, how many are allocated?" — and
a single integer cannot answer it. Two non-overlapping 8-GPU bookings
(10–12 and 14–16) should both succeed; the counter says `8 + 8 > 8` and
wrongly rejects the second. Worse in the other direction: nothing
decrements when an interval ends, so quota is held forever.

The spec even stated the invariant as *"for every time interval,
allocated ≤ total"*. A scalar cannot enforce that.

Two ways out: make capacity interval-aware (SUM over overlapping
reservations, no counter), or drop the intervals. We chose the second —
simpler, matches how quota is already defined ("concurrently held
units"), and rooms still carry the interval story.

**Rooms stay interval-based.** That is where the GiST exclusion
constraint earns its place, and it is the one part of the system where
Postgres does the concurrency work for us — no lock, no read-then-write,
no race. Good contrast to the GPU path in the README: some invariants the
database can express, some it cannot, and quota is one it cannot.

> ACTION: the architecture doc still lists `start_time`/`end_time` on
> `GPUReservation`. It must be corrected — a stale doc is what makes
> someone "helpfully" re-add the column on Day 6.

**Native Postgres enum types**, via SQLAlchemy `Enum(...)`. Our five
vocabularies (Role, ResourceType, ResourceStatus, ReservationStatus,
EnrollmentStatus) are stable; we are not adding a role mid-project.
Accepted cost: adding a value later needs a migration, and
`ALTER TYPE ... ADD VALUE` has restrictions inside a transaction block,
which Alembic wraps migrations in.

`ReservationStatus` has exactly two values (ACTIVE / CANCELLED) because
every quota calculation is `SUM(...) WHERE status = 'ACTIVE'`. A third
state would force a decision about whether it counts and an update to
every quota query. There is no EXPIRED because nothing expires on a
timer — that follows directly from hold-until-release.

**Course schedule times are `"HH:MM"` strings plus a days string
("MWF").** Separate from room reservations, which use real `tstzrange`
timestamps. Must be zero-padded (`"09:00"`, not `"9:00"`) or
lexicographic comparison breaks. Schedule-overlap is #4 on the cut list
anyway, so we spent no more time here.

**Enrollment uniqueness is plain `UNIQUE(student_id, course_offering_id)`.**

Consequence, and it is a real trap: a student who dropped still owns a
row, so **re-registration and waitlist promotion must be UPDATEs, not
INSERTs.** A promoted student who previously dropped would otherwise hit
an IntegrityError inside the promotion transaction on Day 7, which would
look like a mysterious bug rather than a design consequence.

Partial unique index (`WHERE status='ACTIVE'`) was considered and
rejected: it allows clean inserts but lets duplicate DROPPED rows
accumulate, and loses the hard guarantee.

**`idempotency_keys.response_body` / `response_status` stay nullable.**
Load-bearing. The sequence is: INSERT key (response NULL) → do the
booking → UPDATE key with the response → COMMIT. The claim happens first
so the slot is taken before any work; the response is filled in once we
know what it is. Both land in the same transaction, which *is* the
guarantee — if the process crashes mid-transaction, key and booking roll
back together and the retry books cleanly.

**The JWT carries the role as a claim.** `require_role` can then
authorise without a DB query per request — which matters on Day 5, when
500 concurrent requests must not add user-lookup noise to lock
measurements. Tradeoff: a role change does not take effect until the
token expires (60 min). Accepted.

## Frozen interfaces

```python
def get_current_user(...) -> User
def require_role(*allowed: Role) -> Callable
```

Stub shipped Day 1 in `app/core/dependencies.py`, returning a hardcoded
ADMIN so B is unblocked at 9am. Day 3 replaces the **bodies only** —
same import path, no refactor on B's side.

| Code | Meaning |
|---|---|
| 401 | Missing or invalid token |
| 403 | Valid token, wrong role |
| 409 `CAPACITY_EXHAUSTED` | The resource is full |
| 409 `QUOTA_EXCEEDED` | Caller is at their personal limit |
| 422 `IDEMPOTENCY_KEY_REUSED` | Same key, different payload |

The two 409s must stay distinguishable — the remedies differ. Capacity
means wait or try another cluster; quota means release something held.

## Migration

Two revisions applied:

- `705b757e5df2` — initial schema, 12 tables (autogenerated)
- `e0fbfe421403` — GiST exclusion constraint (hand-written)

`alembic/env.py` wired with `import app.models` (registers every model in
`Base.metadata`), `target_metadata = Base.metadata`, and
`config.set_main_option("sqlalchemy.url", get_settings().database_url)`
so credentials stay in one place.

**The exclusion constraint needed its own revision.** The first attempt
added it to the generated file, but it silently did not execute — the
migration reported success and the constraint was absent. Caught only by
querying `pg_constraint` directly.

> Lesson: verify DDL by querying the database, never by assuming a
> successful `alembic upgrade` did what you wrote.

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE reservations
ADD CONSTRAINT no_overlapping_room_reservations
EXCLUDE USING gist (
    resource_id WITH =,
    tstzrange(start_time, end_time, '[)') WITH &&
) WHERE (status = 'ACTIVE');
```

- `btree_gist` — GiST handles ranges natively but not plain equality on
  an integer. The extension adds it, which is what allows combining
  `resource_id WITH =` and a range operator in one constraint.
- `EXCLUDE USING gist` — "no two rows may satisfy all these conditions
  at once." A generalisation of UNIQUE.
- `'[)'` — **half-open: inclusive start, exclusive end.** With `'[]'`,
  back-to-back bookings would be impossible.
- `WHERE (status = 'ACTIVE')` — partial constraint, so cancelled
  reservations do not block rebooking a slot you released.

**Overlap test, run manually and passed:**

| Interval | Expected | Result |
|---|---|---|
| `[10:00, 12:00)` | insert | ✅ |
| `[11:00, 13:00)` | reject (overlap) | ✅ rejected |
| `[12:00, 14:00)` | insert (adjacent) | ✅ |

The third case is the whole reason the range is `'[)'`.

## Verification

```
docker compose up            → both containers healthy (A and B)
GET /health                  → {"status":"ok"}
GET /health/db               → {"database":"ok","result":1}
/docs                        → renders
hot reload                   → fires on save (OneDrive not a problem)
import app.models            → 12 tables in Base.metadata
\dt                          → 12 tables + alembic_version
pg_constraint query          → no_overlapping_room_reservations present
                             → gpu_capacity_sane present
```

## Incidents

**`app/models/enum.py` was committed empty (0 bytes) and `resource.py`
was never committed at all.** Every model imports from `enums` (plural),
so the package could not import — meaning the models had never been run
before being pushed. `resource.py` holds `GPUCluster`, the row locked on
Day 4; without it there is no project.

Resolved by writing `resource.py` locally and renaming `enum.py` →
`enums.py`. Note `enum.py` is also actively dangerous as a filename: it
shadows Python's standard-library `enum` module, which `enums.py` itself
imports on line 1.

Cost: roughly one hour.

> **Rule going forward: run this before pushing any schema change.**
>
>     docker compose exec app python -c "import app.models; from app.database.base import Base; print(len(Base.metadata.tables))"

**Port 5432 was held by containers from an earlier project directory**
that had been running for 28 hours. Docker containers survive folder
renames — `docker ps -a` and `docker rm -f` are the fix, not renaming or
deleting the folder.

**Column names differ from the original design** and code must match the
models, not the plan: `users.name` (not `full_name`),
`users.password_hash` (not `hashed_password`), and
`gpu_reservations.gpu_cluster_id` (not `cluster_id`). Relevant to Day 2's
`auth/service.py`.

## Outstanding — B to fix before Day 3

1. **`WaitlistEntry` has no constraints at all.** Needs
   `UNIQUE(student_id, course_offering_id)` and
   `UNIQUE(course_offering_id, position)` — the latter with
   `deferrable=True, initially="IMMEDIATE"`, because renumbering after a
   promotion (`SET position = position - 1`) transiently collides:
   Postgres checks unique constraints per row during an UPDATE.
   Without the position constraint, Benchmark 4's double-promotion
   corrupts silently instead of failing loudly — which defeats the point
   of the benchmark.
2. **Missing indexes on `gpu_reservations.user_id` and `.status`.** The
   quota SUM runs inside the hottest transaction *while holding the user
   lock*; every millisecond there is a millisecond other requests from
   that user spend blocked. A sequential scan is measurable.
3. Same for `reservations.resource_id` and `.status`.
4. **`created_at` resolved to `timestamp without time zone`** on
   `Reservation`, `GPUReservation`, `Enrollment`, and `WaitlistEntry`,
   because no explicit type was given. Should be
   `DateTime(timezone=True)`.
5. `users` has no `created_at` at all.
6. Confirm what `ResourceStatus` (AVAILABLE / BLOCKED) is for — it
   appeared during the model session and is not in the original design.
7. Confirm whether `EnrollmentStatus.WAITLISTED` is ever used. Waitlist
   entries live in their own table, so a student on the waitlist should
   have a row there, not an enrollment. Matters for Day 7 promotion.

---

## Pre-agreed cut order

Decided now rather than under pressure on Day 8. Day 10 is not a buffer.

1. ~~Locust~~ (cut Day 1)
2. Room quota (mechanism identical to GPU; document it)
3. Course-load quota (same)
4. Schedule-overlap check (orthogonal to the concurrency story)
5. Waitlist (largest single item — **cut whole, not half**)

**Never cut:** the GPU transaction, RBAC, or the first three benchmarks.
Those are the project.

Anything cut gets a README section titled **"Designed, not implemented"**
with the mechanism described. A documented deferral reads as judgment; a
missing feature reads as failure.

## Build the broken version first

`reserve_gpu()` gets written **without** the user lock first,
deliberately. B runs Benchmark 2 against it and records the corruption
(`held = 4`). Then the lock goes in and it is re-run.

Two numbers side by side — broken and fixed — is the single most credible
artifact in the project. Reconstructing a "broken build" on Day 8 to make
a table look good is obvious and worthless. Same applies to Benchmark 1
against unlocked course registration.

---

## Day 2 split

- **A:** Argon2 hashing, JWT encode/decode with role claim,
  `POST /auth/register`, `POST /auth/login`.
  Order: `core/security.py` → `auth/schemas.py` → `auth/service.py` →
  `auth/router.py`. `security.py` first because it imports only `config`
  and is the only file fully testable without a database.
- **B:** seed script (3 users one per role, 2 GPU clusters, 2 rooms,
  1 course + offering) and read endpoints (`GET /gpus`, `/rooms`,
  `/courses`, `/{id}/availability`), coded against A's stub.

Duplicate-email registration must return 409, not 500. Our code checks
whether the email exists, but two simultaneous registrations could both
check, both see "no", and both insert — the `UNIQUE` constraint is what
makes it impossible. Catch the IntegrityError. Same pattern all week:
**our code handles the normal case, the database makes the race
impossible.**
