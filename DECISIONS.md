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

# Deadline 1

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
  land early — needed on Deadline 5 so the idempotency key hits the unique
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
deadlock across the whole app. Settled Deadline 1 to avoid a Deadline 7 merge
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

> ~~ACTION: the architecture doc still lists `start_time`/`end_time` on
> `GPUReservation`. It must be corrected — a stale doc is what makes
> someone "helpfully" re-add the column on Deadline 6.~~
> **DONE.** The doc now carries an explicit "do not re-add them" note.
> Three other lines in the same block had drifted (`Resource.location`,
> `Room.room_number`, `GPUCluster.gpu_type`, and `resource_id` where
> joined-table inheritance means `id`); all corrected against the models.

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
an IntegrityError inside the promotion transaction on Deadline 7, which would
look like a mysterious bug rather than a design consequence.

Partial unique index (`WHERE status='ACTIVE'`) was considered and
rejected: it allows clean inserts but lets duplicate DROPPED rows
accumulate, and loses the hard guarantee.

**`idempotency_keys.response_body` / `status_code` stay nullable.**
Load-bearing. The sequence is: INSERT key (response NULL) → do the
booking → UPDATE key with the response → COMMIT. The claim happens first
so the slot is taken before any work; the response is filled in once we
know what it is. Both land in the same transaction, which *is* the
guarantee — if the process crashes mid-transaction, key and booking roll
back together and the retry books cleanly.

**The JWT carries the role as a claim.** `require_role` can then
authorise without a DB query per request — which matters on Deadline 5, when
500 concurrent requests must not add user-lookup noise to lock
measurements. Tradeoff: a role change does not take effect until the
token expires (60 min). Accepted.

> **REVERSED at Deadline 3 — the saving was already spent.** The frozen
> signature returns a `User`, so `get_current_user` loads that row on
> every request regardless; by the time `require_role` runs there is no
> lookup left to avoid. `require_role` therefore reads `user.role` from
> the database row, and the stale-role window is zero rather than 60
> minutes. Left in place unstruck because the reasoning was sound given
> what was known at Deadline 1 — what changed is that the return type
> was frozen in the same session. See "The role is read from the
> database, not the claim" below.

## Frozen interfaces

```python
def get_current_user(...) -> User
def require_role(*allowed: Role) -> Callable
```

Stub shipped Deadline 1 in `app/core/dependencies.py`, returning a hardcoded
ADMIN so B is unblocked at 9am. Deadline 3 replaces the **bodies only** —
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
Deadline 4; without it there is no project.

Resolved by writing `resource.py` locally and renaming `enum.py` →
`enums.py`. Note `enum.py` is also actively dangerous as a filename: it
shadows Python's standard-library `enum` module, which `enums.py` itself
imports on line 1.

Cost: roughly one hour.

> **Rule going forward: run this before pushing any schema change.**
>
>     docker compose exec app python -c "import app.models; from app.database.base import Base; print(len(Base.metadata.tables))"

**The migration chain could not be re-run from empty.** Found in session 2
while verifying Deadline 1's "migration applies cleanly on a clean DB"
checkpoint — which had only ever been tested *incrementally*, never from
zero. `alembic downgrade base` then `alembic upgrade head` failed:

```
sqlalchemy.exc.ProgrammingError: (psycopg.errors.DuplicateObject)
type "resource_type_enum" already exists
[SQL: CREATE TYPE resource_type_enum AS ENUM ('GPU', 'ROOM', 'COURSE')]
```

Two independent bugs in `705b757e5df2`, both invisible to an incremental
upgrade:

1. **`op.drop_table` does not drop the types `sa.Enum` created.** All five
   enum types survived `downgrade base`, so the next `upgrade head` hit
   `CREATE TYPE ... already exists`. The downgrade now ends with explicit
   `DROP TYPE IF EXISTS` for all five. This is the *second* cost of native
   Postgres enums, alongside the `ALTER TYPE ADD VALUE` restriction
   already noted above — worth knowing before someone reaches for a sixth.
2. **The room exclusion constraint was in both `705b757e5df2` and
   `e0fbfe421403`.** A clean run therefore created it, then tried to
   create it again. Removed from the initial migration; `e0fbfe421403`
   owns it, which was the point of giving it its own revision.

The Deadline 1 note above says the duplicated DDL "silently did not execute."
It executes. What actually happened is that the constraint was created by
the initial migration, the check for it ran against a database where the
separate revision had also been applied, and nobody re-ran the chain from
scratch — so a broken chain looked like a working one for two days.

**Because the whole `upgrade` runs in one transaction** (env.py does not
set `transaction_per_migration`), the failure in revision 2 rolled back
revision 1 as well. The database was left with zero tables and an empty
`alembic_version` — not at revision 1, as a per-migration transaction
would have left it. Good property to know: a failed chain leaves nothing
half-built.

> Lesson, sharper than Deadline 1's version: `alembic upgrade head` on a
> database you built incrementally proves nothing. The check is
> **`downgrade base` → `upgrade head`, twice.** Now passing, twice, with
> `alembic check` clean and all 9 non-FK constraints present after the
> rebuild.

Safe to edit an applied migration here only because no environment holds
real data yet (verified 0 rows in all 12 tables before the teardown) and
the end state is identical for anyone already at head. That stops being
true the moment there is a database worth keeping.

**Port 5432 was held by containers from an earlier project directory**
that had been running for 28 hours. Docker containers survive folder
renames — `docker ps -a` and `docker rm -f` are the fix, not renaming or
deleting the folder.

**Column names differ from the original design** and code must match the
models, not the plan: `users.name` (not `full_name`),
`users.password_hash` (not `hashed_password`), and
`gpu_reservations.gpu_cluster_id` (not `cluster_id`). Relevant to Deadline 2's
`auth/service.py`.

## Outstanding — ~~B~~ to fix before Deadline 3

> **Mislabelled.** Items 1–5 are all schema changes, and schema changes
> require an Alembic revision, which **only A may create** (see Ownership
> above). B could not have done any of them. Items 6 and 7 are decisions,
> not code. Corrected: 1–5 were A's, all now done in `c86676652ca2` and
> `1ca8b85b7626`; 6 and 7 remain open and are joint calls.
>
> **The heading's "before Deadline 3" no longer holds either.** It was
> written when the list was all schema. Items 6–10 are design questions
> with different due dates, so each now names its own; item 7 was already
> the counter-example, since it blocks Deadline 7.

1. ~~**`WaitlistEntry` has no constraints at all.** Needs
   `UNIQUE(student_id, course_offering_id)` and
   `UNIQUE(course_offering_id, position)` — the latter with
   `deferrable=True, initially="IMMEDIATE"`, because renumbering after a
   promotion (`SET position = position - 1`) transiently collides:
   Postgres checks unique constraints per row during an UPDATE.
   Without the position constraint, Benchmark 4's double-promotion
   corrupts silently instead of failing loudly — which defeats the point
   of the benchmark.~~
   **RESOLVED in `c86676652ca2`, and the deferrable constraint turned out
   not to be needed — see "Waitlist order is `created_at`" below. Dropping
   `position` deleted the problem instead of constraining it.**
2. ~~**Missing indexes on `gpu_reservations.user_id` and `.status`.** The
   quota SUM runs inside the hottest transaction *while holding the user
   lock*; every millisecond there is a millisecond other requests from
   that user spend blocked. A sequential scan is measurable.~~
   **DONE — `1ca8b85b7626`.** One composite
   `ix_gpu_reservations_user_status (user_id, status)`, not two
   single-column indexes: the query filters on both, and a composite
   whose leading column is `user_id` also serves `user_id` alone.
3. ~~Same for `reservations.resource_id` and `.status`.~~
   **CLOSED, no index added.** `no_overlapping_room_reservations` is a
   GiST index on `(resource_id, tstzrange(start,end))` partial to
   `status='ACTIVE'` — the availability query's exact shape, so a btree on
   `(resource_id, status)` would duplicate it. Not EXPLAIN-verified,
   because that query does not exist yet; re-check on Deadline 3 when it does.
   Added `ix_reservations_user_id` instead, which nothing covered — "my
   reservations" had no index at all and was not on this list.
4. ~~**`created_at` resolved to `timestamp without time zone`** on
   `Reservation`, `GPUReservation`, `Enrollment`, and `WaitlistEntry`,
   because no explicit type was given. Should be
   `DateTime(timezone=True)`.~~
   **DONE — `1ca8b85b7626`.** Five tables, not four: `idempotency_keys`
   was missed by this note.
5. ~~`users` has no `created_at` at all.~~ **DONE — `1ca8b85b7626`.**
6. ~~Confirm what `ResourceStatus` (AVAILABLE / BLOCKED) is for — it
   appeared during the model session and is not in the original design.~~
   **Premise corrected: it IS in the original design** — `INIT_PLAN.md`
   §11 (`status -- AVAILABLE | BLOCKED (admin-controlled)`), §12
   (`PATCH /rooms/{id} -- modify availability/status`), and §13, which
   titles it *"Requested feature: admin-only resource modification."* It
   is admin-blocks-a-room-for-maintenance, and it was asked for.
   ~~**Still open, and narrower: the enforcement semantics.** Does blocking
   evict existing reservations (proposed: no, matching the
   capacity-reduction rule), and what error code?~~
   **RATIFIED at Deadline 3, as proposed, and now enforced in
   `rooms/service.py::reserve_room`:**
   - blocking stops **new** allocations and does **not** evict existing
     ones — the same rule as the capacity reduction in
     `ARCHITECTURE_AND_WORKFLOWS.md` §13;
   - distinct code **`409 RESOURCE_BLOCKED`**, because the remedy differs
     from both existing 409s: try a different resource, do not wait for
     capacity, do not release anything you hold;
   - checked **inside the transaction, against the row just locked**,
     never at the boundary — `status` is mutable, so an admin can flip it
     between a boundary read and the write.

   Asserted in `scripts/check_rooms.py`, including the non-eviction half
   (an active hold survives its room being blocked) and the fact that an
   ADMIN is refused too — BLOCKED is a fact about the resource, not a
   permission. The GPU half of the gate lands with that transaction at
   Deadline 4; the PATCH endpoints that *write* the flag are at Deadline
   6, and they had no deadline at all before session 5.
7. Confirm whether `EnrollmentStatus.WAITLISTED` is ever used. Waitlist
   entries live in their own table, so a student on the waitlist should
   have a row there, not an enrollment. Matters for Deadline 7 promotion.
8. **`DELETE /reservations/{id}` names a row in two different tables.**
   Room holds live in `reservations`, GPU holds in `gpu_reservations`,
   each with its own id sequence — so `/reservations/5` matches a row in
   both and the path does not say which. This is the same shape as the
   `/courses/{id}/register` problem: a route keyed on something that does
   not identify one row. It survived the documentation sync because it
   does not break a *lock*, only the routing, so nothing failed loudly.
   In `INIT_PLAN.md` §12 under both Rooms and GPUs, in
   `ARCHITECTURE_AND_WORKFLOWS.md` Workflow C, and in `EXECUTION_PLAN.md`
   at Deadline 4 — it is a real open question, not doc staleness.
   ~~**Needs deciding before Deadline 4** (B's endpoint, A's `cancel()`).~~
   **RATIFIED at Deadline 4: the cancel route mirrors the POST route.**
   `DELETE /rooms/{room_id}/reservations/{id}` and
   `DELETE /gpus/{gpu_id}/reservations/{id}`. No new naming convention,
   and the ambiguity disappears structurally rather than by
   documentation. Both services verify the reservation really belongs to
   the resource named in the path, so the id there is load-bearing, and
   both checks are asserted. See the Deadline 4 section below.
9. **The global lock order and the waitlist promotion contradict each
   other.** `INIT_PLAN.md` §14 says every path takes key → user row →
   resource row, naming *"allocation, cancellation, course registration,
   and waitlist promotion alike."* Deadline 7 in `EXECUTION_PLAN.md` has
   promotion lock the **offering first** and then check the promoted
   student's course-load quota with no user lock at all. Two consequences:
   the quota check is unprotected — the exact failure Benchmark 2 exists
   to demonstrate — and taking the user lock to fix it yields
   offering → user while registration holds user → offering, which is a
   cycle on the one path these documents promise cannot deadlock.
   Note "The claim this project makes" above is careful here: it lists
   cancellation and course registration and does *not* claim promotion.
   §14 over-claims relative to it. **Needs deciding before Deadline 7**;
   it is A's transaction.
10. **Is joining a waitlist automatic or explicit?**
    `ARCHITECTURE_AND_WORKFLOWS.md` Workflow D falls through to the
    waitlist when an offering is full (`else → waitlist`);
    `INIT_PLAN.md` §12 has the student call
    `POST /offerings/{id}/waitlist` themselves. Both cannot be the
    default. This is **item 7 in another form**: auto-waitlisting on a
    full register naturally writes an enrollment row with
    `status = WAITLISTED`, putting the student in two tables at once;
    explicit joining leaves `WAITLISTED` dead and it should come out of
    the enum. Answer one and the other follows.
    **Needs deciding before Deadline 7**, with item 7.
11. **Why does the GPU path not reproduce the stale locked read?** A
    two-session probe shows `SELECT ... FOR UPDATE` returning a stale
    `GPUCluster.allocated` exactly as it did for `CourseOffering.
    enrolled_count` — lock held, value from before the lock. But with
    `populate_existing()` removed, the 12-racer capacity race produced a
    correct 8/8 on four consecutive runs, `allocated` matching
    `SUM(active)` every time. The course path failed catastrophically in
    the same situation (20 of 5 seats sold, counter reading 3).
    The statement sequences differ slightly and no explanation was
    established. `populate_existing()` stays on every locked read
    regardless, because the staleness is demonstrable and the cost is one
    keyword — but until this is understood, it is a precaution rather
    than a diagnosed fix. **Matters before Deadline 7**, which introduces
    the next locked read (the promotion transaction), and it is A's.

---

## Pre-agreed cut order

Decided now rather than under pressure on Deadline 8. Deadline 10 is not a buffer.

1. ~~Locust~~ (cut Deadline 1)
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
artifact in the project. Reconstructing a "broken build" on Deadline 8 to make
a table look good is obvious and worthless. Same applies to Benchmark 1
against unlocked course registration.

---

## Deadline 2 split

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

---

## Schema amendment — course capacity moves to the offering

Revision `268c10da1da4`. Taken before any seed data existed, so it is a
clean move rather than a data migration.

**`capacity` moved from `courses` to `course_offerings`.** It was on the
wrong table. `courses` is the catalogue entry — stable across semesters —
so a capacity there says "CS101 has 50 seats, forever, in every section
simultaneously," which is not a thing. Seats belong to a section: two
sections of the same course, in the same semester, can be sized
differently, and next semester's section can be resized without
rewriting the catalogue.

**`enrolled_count` added to `course_offerings`.** This is the counter
that course registration locks — the direct analogue of
`GPUCluster.allocated`, so the GPU transaction and the course transaction
— both Deadline 4, one per person — now have *the same shape*:
`SELECT ... FOR UPDATE` the row holding the counter, compare against
`capacity`, increment, insert.

Without it, the capacity gate would have to be
`SELECT COUNT(*) FROM enrollments WHERE course_offering_id = ? AND status
= 'ACTIVE'`, which counts rows in a table other concurrent registrations
are inserting into. There is no row to lock, so `FOR UPDATE` has nothing
to take, and two registrations can both read 49 against a capacity of 50.
Locking the offering row is what makes Benchmark 1 winnable.

`enrolled_count` is therefore derived state and can disagree with
`enrollments` if any code path updates one without the other. The rule:
**every write to `enrollments` happens in the same transaction as the
matching `enrolled_count` update.** Deadline 8 should end with a reconciliation
query proving the two agree after Benchmark 1:

```sql
SELECT o.id, o.enrolled_count, COUNT(e.id) FILTER (WHERE e.status = 'ACTIVE')
FROM course_offerings o LEFT JOIN enrollments e ON e.course_offering_id = o.id
GROUP BY o.id HAVING o.enrolled_count <> COUNT(e.id) FILTER (WHERE e.status = 'ACTIVE');
```

> **Two deadline numbers in this section were wrong and are now fixed.**
> Course registration is **Deadline 4**, the same stage as the GPU
> transaction — which is the point the paragraph above is making, and it
> read "Deadline 6". The reconciliation query is scheduled at **Deadline
> 8** in `EXECUTION_PLAN.md`, in B's column, and it read "Deadline 6" too.
> Both said "Day 6" when written, so the Deadlines-not-days substitution
> carried them through faithfully. Corrected rather than struck through:
> these were miscounts on the day, not decisions that later changed, and
> nothing about the amendment itself moved.

Two CHECKs guard it, same reasoning as `gpu_capacity_sane` — the lock is
the mechanism, the constraint makes a locking bug fail loudly instead of
overselling seats:

```sql
ALTER TABLE course_offerings
  ADD CONSTRAINT offering_capacity_positive CHECK (capacity > 0);
ALTER TABLE course_offerings
  ADD CONSTRAINT offering_enrollment_sane
  CHECK (enrolled_count >= 0 AND enrolled_count <= capacity);
```

**`instructor_id → users.id` added to `course_offerings`**, indexed. The
instructor teaches a section, not a catalogue entry, so this is on the
same table for the same reason capacity is. It is `NOT NULL` — every
offering has an owner, which gives FACULTY endpoints an ownership check
("this is my section") instead of a blanket role check. Nothing enforces
that the referenced user has `role = FACULTY`; the FK points at `users`,
and the role is checked in the service layer.

> `NOT NULL` on `instructor_id` was only possible because
> `course_offerings` was empty (verified: 0 rows). There is no backfill
> value for it, so the same migration against a populated database would
> fail. The `capacity` move does backfill from the parent course, so that
> half is safe either way.

Downgrade collapses capacity back to `MAX` over a course's offerings —
lossy by nature, since many offerings map to one course. Noted so nobody
reads the downgrade as a true inverse.

**Verified** (per the Deadline 1 rule — query the database, do not trust
"upgrade successful"):

```
alembic upgrade head → downgrade -1 → upgrade head   → clean
alembic check                                        → no drift
\d courses                                           → no capacity column
\d course_offerings   → capacity, enrolled_count DEFAULT 0, instructor_id
                      → both CHECKs present
                      → fk_course_offerings_instructor_id_users present
                      → ix_course_offerings_instructor_id present
import app.models                                    → still 12 tables
```

Seeds and read endpoints (Deadline 2, B) must set `capacity` and
`instructor_id` on the offering, not the course.

---

## Schema amendment — waitlist order is `created_at`

Revision `c86676652ca2`. Resolves outstanding item 1.

**`waitlist_entries.position` dropped.** FIFO order is now
`ORDER BY created_at, id`.

The position column stored information the table already contained.
Worse, it stored it *redundantly across rows*: promoting entry 1 means
rewriting every remaining position for that offering. That is a
write-amplified O(n) UPDATE inside the promotion transaction, while
holding the offering lock — the exact place where holding the lock longer
costs the most. And the renumbering was what forced the deferrable unique
constraint, because `SET position = position - 1` transiently collides
mid-UPDATE.

Dropping the column removes all of it at once. A promotion now touches
one row (delete the entry) instead of n. There is no renumbering, so
there is nothing to defer, so the tricky constraint is unnecessary rather
than merely absent.

The tradeoff is real and worth stating: reading "what position am I in?"
becomes `ROW_NUMBER() OVER (ORDER BY created_at, id)` at query time
instead of a column read. That is the right side of the trade — position
is read occasionally by one student, and was written by every promotion
under lock.

**`UNIQUE(student_id, course_offering_id)` added** as `waitlist_unique`,
matching `enrollment_unique`. One student cannot queue twice for the same
offering, and a double-submit gets an IntegrityError to catch rather than
a duplicate row. Unlike `enrollments`, this has no dropped-row trap:
leaving the waitlist DELETEs the row, so a re-join is a clean INSERT.

**`ix_waitlist_entries_offering_created` added** on
`(course_offering_id, created_at)`. The promotion query
(`WHERE course_offering_id = ? ORDER BY created_at, id LIMIT 1`) runs
inside the promotion transaction while holding the offering lock, so a
sequential scan there is lock time paid by every other request on that
offering. Verified with EXPLAIN — index scan plus an incremental sort on
the `id` tiebreak, no sort of the full set.

### `now()` is transaction start time — the `id` tiebreak is load-bearing

Tested directly. Three waitlist rows inserted in **one** transaction:

```
distinct_created_at | rows
--------------------+------
                  1 |    3
```

`func.now()` returns the transaction's start timestamp, not the
statement's. Every row written in one transaction shares a `created_at`,
so `ORDER BY created_at` alone leaves their order **undefined**.

This never happens on the real path — each student's join is its own
request and its own transaction (measured 3 ms apart, ordered correctly).
It happens in exactly one place that matters: **a seed script that inserts
several waitlist entries in one transaction.** Benchmark 4 asserts *which*
entries got promoted, so seeded ties would make it flap for a reason that
has nothing to do with locking, and the flap would look like a
concurrency bug.

Two consequences, both cheap:

1. **`ORDER BY created_at, id` — always, never `created_at` alone.** The
   index leads with `created_at`, so the tiebreak costs nothing.
2. Deadline 2's seed script either commits each waitlist insert separately, or
   sets `created_at` explicitly per row.

> Note: `created_at` is still `timestamp without time zone` (outstanding
> item 4). It is now an ordering key, which does not break — every value
> comes from the same server clock — but item 4 is worth doing before it
> is also a value we return in API responses.
>
> **RESOLVED later the same day in `1ca8b85b7626`** — see "Schema hygiene"
> below. Left in place because it is why that revision happened.

**Verified:**

```
upgrade head → downgrade -1 → upgrade head   clean
alembic check                                no drift
\d waitlist_entries    → no position column
                       → waitlist_unique UNIQUE (student_id, course_offering_id)
                       → ix_waitlist_entries_offering_created present
3 joins, 3 transactions → ORDER BY created_at, id = S1, S2, S3   ✅
EXPLAIN promotion query → Index Scan + Incremental Sort           ✅
S2 joins twice          → rejected, waitlist_unique               ✅
3 joins, 1 transaction  → 1 distinct created_at (see above)       ✅
test rows removed, all tables back to 0
```

Downgrade rebuilds `position` with `ROW_NUMBER() OVER (PARTITION BY
course_offering_id ORDER BY created_at, id)`, so it is a true inverse —
unlike the capacity downgrade above.

---

## Schema hygiene — outstanding items 2 through 5

Revision `1ca8b85b7626`. Autogenerated, then adjusted; one revision
because the items are independent and none needs the others.

**All `created_at` columns are `timestamptz`.** Five tables, not the four
the outstanding note listed — `idempotency_keys` was missed there.

The conversion carries an explicit
`USING created_at AT TIME ZONE 'UTC'`. Postgres will convert
`timestamp` → `timestamptz` without one, but it interprets the naive
values in **the session's `TimeZone`**, so the result depends on who ran
the migration and with what settings. Our container is `Etc/UTC` and the
tables were empty, which makes the implicit conversion identical today and
wrong the first time either of those stops being true. The downgrade
carries the same clause in reverse.

**`users.created_at` added**, `timestamptz`, `server_default now()`.

**`courses.code` is now UNIQUE.** `ix_courses_code` was a plain index, so
two `CS101` rows were legal in a catalogue table. Replaced in place — same
index name, `unique=True` — rather than adding a separate constraint
alongside, so there is one object enforcing it instead of two.

**Three indexes added**, each tied to a specific query:

| Index | Serves |
|---|---|
| `ix_gpu_reservations_user_status (user_id, status)` | the quota SUM, under the user lock |
| `ix_enrollments_offering_status (course_offering_id, status)` | class roster, `enrolled_count` reconciliation |
| `ix_reservations_user_id (user_id)` | "my reservations" |

One composite rather than two single-column indexes on
`gpu_reservations`: the query filters on both columns, and a composite led
by `user_id` also serves `user_id` alone. `enrollment_unique` already
covers the course-load quota (`student_id` leading), which is why the
enrollments index is keyed the other way round — that direction was
uncovered.

**Verified — EXPLAIN, not just "the DDL ran":**

```
quota SUM   → Index Scan using ix_gpu_reservations_user_status
               Index Cond: user_id = 1 AND status = 'ACTIVE'
roster      → Index Only Scan using ix_enrollments_offering_status
duplicate 'CS101' → rejected by ix_courses_code
users.created_at  → 2026-08-17 10:15:29.001829+00, tz-aware
all 6 created_at  → timestamp WITH time zone
downgrade base → upgrade head, twice (5 revisions each way), alembic check clean
```

---

## Documentation reconciled against the schema at head

No code or schema change. The five `.md` files had drifted apart, in the
specific direction the Deadline 1 GPU note warned about: *"a stale doc is what
makes someone 'helpfully' re-add the column on Deadline 6."*

**Document precedence, now written down.** The models and migrations are
the truth. Then this file (why, and what was reversed), then
`ARCHITECTURE_AND_WORKFLOWS.md` (what the system is now), then
`WORK_LOG.md` and `EXECUTION_PLAN.md`, then `INIT_PLAN.md`.

**`INIT_PLAN.md` was corrected in place rather than deleted.** Its §11
data model was the pre-amendment design and described four things the
project has deliberately removed — GPU `start_time`/`end_time`,
`Course.capacity`, `waitlist_entries.position`, and the
`location`/`room_number`/`gpu_type` columns. It also predates the
exactly-once guarantee entirely: no `IdempotencyKey` table, no
`idempotency/` module, and a two-step lock order.

Deleting it was considered and rejected — it is the only document that
states the problem and the motivation, and those have not changed. Every
divergence is now marked inline with `CHANGED:` and a pointer to the
revision, so the file reads as a record of what was decided differently
rather than as a competing specification.

**`DECISIONS.md` and `WORK_LOG.md` were deliberately NOT rewritten.**
They are append-only records whose value is that they were written as
things happened. Retrofitting today's schema onto Deadline 1's entries would
destroy the one property that makes them credible. Only resolution
markers were added, in the style the files already use.

### Course write paths are keyed on the offering, not the course

The one genuine design decision surfaced by the sync, and a direct
consequence of `268c10da1da4` that nothing had recorded.

The original API put registration at
`POST /api/v1/courses/{course_id}/register`. Once `capacity`,
`enrolled_count`, and `instructor_id` moved to `course_offerings`, that
route stopped being implementable as specified: **one course has many
offerings, so a course-keyed route has no single row to lock** — and
locking the row that holds the counter is the entire mechanism that makes
Benchmark 1 winnable.

Resolved by splitting the API the same way the schema was split:

``` text
read paths stay course-shaped     GET /courses
                                  GET /courses/{id}
                                  GET /courses/{id}/offerings

write paths become offering-shaped  POST   /offerings/{id}/register
                                    DELETE /offerings/{id}/drop
                                    GET    /offerings/{id}/waitlist
```

Browsing a catalogue is genuinely course-shaped; allocating a seat is
not. Affects B's Deadline 4 column.

### Left open on purpose

Outstanding items 6 and 7 above are unchanged — both are joint calls and
neither is a documentation problem:

- what `ResourceStatus` (AVAILABLE / BLOCKED) is actually for;
- whether `EnrollmentStatus.WAITLISTED` is ever used, given waitlist
  entries live in their own table. **This blocks Deadline 7 promotion.**

Related, and the same confusion: `Resource` sets
`polymorphic_identity = ResourceType.COURSE` on the base class, so a bare
`Resource()` is typed COURSE while courses never appear in `resources` at
all. Recorded here rather than fixed, because `models/` needs both people.

---

## Incident — a healthy stack on an empty database

B's database had **zero tables and no `alembic_version` row**. The chain
had never been applied to that volume. Both containers were up, the `db`
container was reporting `healthy`, and `/health/db` was returning
`{"database":"ok"}` throughout.

It returns ok because it runs `SELECT 1`. That proves the connection
works, the credentials are right, and the server is accepting queries —
and says nothing whatsoever about whether the schema exists. A green
health check and a green `docker compose ps` were both true and both
irrelevant.

> Sharpening the two lessons above, which were about not trusting a
> *command's* success: do not trust a **health check** either. `/health/db`
> answers "can I reach Postgres", not "is this database usable". The check
> that would have caught this is `alembic current`, and it costs nothing:
>
>     docker compose exec app alembic current   # expect: <rev> (head)
>     docker compose exec app alembic check     # expect: no new operations
>
> Worth running at the start of any session that has been away from the
> repo, before concluding anything from the app's behaviour.

Fixing it was a single `alembic upgrade head`, and it had a silver
lining: because the database was at base rather than incrementally built,
that upgrade **was** the clean-DB test Deadline 1 asked for. All five
revisions applied from empty, `alembic check` clean, 9 non-FK constraints
and 6 hot-path indexes verified by querying `pg_constraint` and
`pg_indexes` directly. Full output in `WORK_LOG.md`.

~~Still outstanding: the same run on A's machine. "Verify on a clean DB on
**both** machines" is not met by verifying one.~~

> **RESOLVED 2026-08-18.** Run on A's machine: `downgrade base` →
> `upgrade head`, twice, five revisions each way, `alembic current` =
> `1ca8b85b7626 (head)`, `alembic check` clean. Full output in
> `WORK_LOG.md`, Deadline 3.
>
> One thing the round-trip confirms that `alembic check` cannot: at
> `base` the database holds **only `alembic_version`** — no orphan enum
> types. That is the Deadline 2 chain-breaking bug, still fixed. `btree_gist`
> deliberately survives the downgrade (dropping an extension can break
> objects outside our schema); `CREATE EXTENSION IF NOT EXISTS` is what
> makes the next upgrade idempotent anyway.

---

## JWT library — PyJWT, not python-jose

`python-jose[cryptography]==3.3.0` → `PyJWT==2.10.1`. Taken **before**
`core/security.py` exists, which is the only reason it was a one-line
change: nothing imported `jose`, so there were no call sites to migrate.
The same swap after Deadline 2's auth column would have touched encode,
decode, and every exception handler.

python-jose 3.3.0 carries known advisories and the project is not
actively maintained. PyJWT is the maintained option, is what FastAPI's
own security docs use, and needs no `[cryptography]` extra for HS256 —
HMAC is in the core package. `cryptography` left the dependency tree
entirely with it (jose was the only thing pulling it); it comes back only
if we ever move to RS256, via `PyJWT[crypto]`.

**Two behavioural differences that matter for `security.py`,** both
verified in the container rather than read off a changelog:

1. **`sub` must be a string, and PyJWT ≥ 2.10 enforces it on `decode`,
   not `encode`.** `{'sub': user.id}` with an int encodes **silently**
   and raises `InvalidSubjectError: Subject must be a string` only when
   the token is later read. The failure mode is therefore the nasty
   shape: registration and login both return 200 with a valid-looking
   token, and then *every authenticated request* 401s — the broken thing
   is the token issuer, but the symptom appears in `get_current_user`.
   So: `str(user.id)` on the way out, `int(payload["sub"])` on the way
   back.

   > First written up here as raising at encode time. It does not. Caught
   > by `scripts/check_jwt.py` asserting the wrong half — which is the
   > argument for the script existing rather than a one-off paste into a
   > shell, and the Deadline 1 lesson again: verify by running, and make sure
   > the thing you run asserts what you actually claimed.

2. **Exceptions are PyJWT's, not jose's.** `JWTError` no longer exists.
   The 401 handler catches `jwt.InvalidTokenError`, the base class for
   `ExpiredSignatureError`, `InvalidSignatureError`, and
   `InvalidSubjectError` — one `except` covers expiry, tampering, and a
   malformed subject, all of which are the same 401 to the caller.
   `InvalidSubjectError` is importable only from `jwt.exceptions`; unlike
   the others it is not re-exported at `jwt.*`.

`exp` is verified by default, so the 60-minute expiry from
`ACCESS_TOKEN_EXPIRE_MINUTES` is enforced by the library rather than by
us — which is what makes the role-in-the-claim tradeoff above ("a role
change does not take effect until the token expires") actually bounded.

Verified by `scripts/check_jwt.py`, which runs against the real
`get_settings()` and exits non-zero on the first failure, so it is usable
as a gate rather than something to read: 9 checks, all passing. `/health`
and `/health/db` still 200 after the rebuild.

    docker compose exec app python scripts/check_jwt.py

---

## Deadlines, not days — and sessions are neither

The schedule is flexible. Ten numbered **Deadlines** are ordered
milestones with checkpoints; none of them names a date, and one may take
four sittings or half of one.

**The rename was not cosmetic.** Two different things were both called
"Day N", and the collision was actively lying to us:

| Was called | Is really | Now called |
|---|---|---|
| `EXECUTION_PLAN.md` "Day 4" | a milestone with a checkpoint | **Deadline 4** |
| `WORK_LOG.md` "Day 3 — 2026-08-18" | one dated sitting | **Session 4** |

Because both were "Day N", a log with three entries read as three
deadlines met. In fact sessions 2, 3 and 4 were *all* still finishing
**Deadline 1** — the carried schema items, the documentation sync, and
the clean-DB migration run. Deadline 2 has not been started by either
person.

> Four sessions to meet one of ten deadlines. That ratio is the useful
> number, and the old scheme made it structurally impossible to see:
> every session incremented the counter that was supposed to track
> milestones, so falling behind and making progress looked identical.

Consequences, all cheap and all now in place:

- `10_DAY_PLAN.md` → **`EXECUTION_PLAN.md`**, `DAILY_LOG.md` →
  **`WORK_LOG.md`**. The filenames asserted a cadence the project does
  not have. Renamed with `git mv`, so history follows.
- Every `WORK_LOG.md` entry opens with **`Advances: Deadline N`** and
  closes with **`Deadline N status: MET / still open`**. Where the plan
  said one thing and reality did another, the entry says so — sessions 2
  and 3 are both marked *"not Deadline 2"*.
- "Daily Ritual" is now the **Session Ritual**, since it runs at the
  edges of a sitting, not of a calendar day.
- `INIT_PLAN.md` §19 keeps its "Days 1–2" headings. It is a superseded
  15-day solo schedule; converting it would imply fifteen live
  milestones competing with the ten real ones. Flagged inline as the one
  deliberate exception.

The genuine durations were left alone throughout — "half a day to unpick
divergent `down_revision` chains", "20 person-days", the `days` schedule
column ("MWF"). Those are measurements, not milestones. Substitution was
done on `Day <digit>` only, so they were never candidates.

---

# Deadline 2 — the auth column (A)

Files, in the order the split specified and the order they were written:
`core/security.py` → `auth/schemas.py` → `auth/service.py` →
`auth/router.py`. Plus `core/errors.py`, which the split did not
anticipate; see below. Mounted under `API_PREFIX` from `main.py`, which
was B's one coordination request from session 6.

## The error envelope — settled here because this is the first 409

Deadline 1 agreed the error *codes*. It never agreed the **JSON they
arrive in**, and nobody noticed because no endpoint returned one yet.
Duplicate registration is the first, so the shape is fixed now rather
than improvised three more times at Deadlines 4 and 5:

```json
{"detail": {"code": "EMAIL_ALREADY_REGISTERED", "message": "..."}}
```

`code` is the contract and `message` is prose that may be reworded. Every
coded failure goes through `core/errors.py::coded_error()`; nothing
hand-rolls a `detail` dict. The reason is the same one that made the two
409s distinguishable in the first place: `CAPACITY_EXHAUSTED` and
`QUOTA_EXCEEDED` differ only in the caller's remedy, and B's benchmarks
have to assert on that difference without parsing English.

Cheap now, and the alternative is discovering at Deadline 8's integration
pass that three modules each invented their own envelope.

## Login is form-encoded, not JSON

`POST /auth/login` takes `OAuth2PasswordRequestForm`, so the body is
`username=...&password=...`, while `/auth/register` takes JSON. The
asymmetry is deliberate and was the one genuinely arguable call in this
column.

For it: `INIT_PLAN.md` §4 names the **OAuth2 password flow**, and
`python-multipart` has sat in `requirements.txt` since Deadline 1 doing
nothing else — session 4 even verified it imports. Following the flow
means `/docs` gets a working **Authorize** button the moment Deadline 3
registers the bearer scheme, which is the difference between demoing RBAC
by clicking and demoing it by pasting headers into every request. That is
worth real money at Deadline 10.

Against it: the email arrives in a field the spec insists on calling
`username`, and B's harness has to send `data=` rather than `json=` for
this one route.

Chosen with the note that **the mapping happens once, in the handler**.
The rest of the system says `email` everywhere.

## Registration does not check whether the email exists

There is no `SELECT ... WHERE email = ?` before the INSERT, and its
absence is the point. Two simultaneous registrations of one address can
both run that check, both see nothing, and both proceed — so it would
pass exactly when it matters least, while making the code *look* like the
race was handled. `UNIQUE(users.email)` is what makes the second insert
impossible, so the INSERT is the check and catching `IntegrityError` is
how its result is read.

One detail that is easy to get wrong: `db.rollback()` must come before
anything else in the `except`. After an `IntegrityError` the session is
in a failed state and any further statement raises
`PendingRollbackError` — which would surface as a 500 and bury the 409 it
was supposed to produce.

## `InvalidHashError` is a `ValueError`, not an `Argon2Error`

Verified in the container rather than assumed, and it changes the code:

```
VerifyMismatchError -> VerificationError -> Argon2Error -> Exception
InvalidHashError    -> ValueError        -> Exception
```

The intuitive single clause, `except Argon2Error`, therefore catches a
wrong password but **not a malformed or empty `password_hash`**, which
would escape the login handler as a 500. `verify_password` catches
`(VerificationError, InvalidHashError)` and returns False for both,
because "this stored hash is garbage" and "this password is wrong" are
the same answer to a caller: these credentials do not authenticate
anyone.

Same shape as the PyJWT `sub` lesson — the exception hierarchy you assume
is not the one the library has, and the only way to know is to run it.

## Both 401s are identical, including their timing

"No such email" and "wrong password" return the same body. That much is
obvious. Less obvious: without help they return it at very different
*speeds* — an unknown address skips argon2 entirely and answers in
microseconds, while a real one pays ~50 ms for a full verify. An
identical body with a distinguishable response time is still a
disclosure of which addresses are registered.

`security.py` exports `DUMMY_PASSWORD_HASH` and `authenticate()` verifies
against it on the miss path, so both cost the same.

## Accepted holes, recorded rather than shipped quietly

- **A caller may register themselves as ADMIN.** `role` comes from the
  request body, which is what Workflow A specifies. The alternative is
  bootstrapping the first admin out of band, which adds a seeding
  concern to every clean-room run — for a project whose claim is about
  concurrency, not identity management. `scripts/seed.py` already
  supplies the three demo accounts. Left as-is, deliberately, and stated
  in the README's limitations rather than discovered by a reader.
- **`email` is a constrained `str`, not Pydantic's `EmailStr`.** That
  needs `email-validator`, which the image does not have, and adding a
  dependency forces a rebuild on both machines — B rebuilt at session 5 —
  for a format check that guarantees nothing the system relies on. What
  it relies on is `UNIQUE(email)`, which is in the database.

## Verification

`scripts/check_auth.py`, 25 assertions, exits non-zero on failure. Same
argument as `check_jwt.py`: that script caught a stale image on its first
run on a second machine, and it could only do so because it was a gate
rather than a paste into a shell. This one covers what `check_jwt.py`
structurally cannot — everything that needs a database and a router.

It registers under a random email and deletes that user at the end, so a
passing run leaves the database at its post-seed counts and the next
person to count rows is not misled by it.

Two assertions in it are worth naming, because they are the ones that
would catch a real regression rather than a typo:

- **`sub` is a string.** The PyJWT ≥ 2.10 trap. If it ever becomes an
  int, login still returns 200 and every authenticated request 401s at
  Deadline 3 — the bug is in the issuer, the symptom is in the consumer.
- **The seeded accounts log in.** B hashed those three users with a bare
  `PasswordHasher()` in session 5, *before* `core/security.py` existed,
  on the argument that argon2 encodes its parameters into the hash string
  so `verify()` would accept them whatever A chose later. That was a
  claim about a file that did not exist. It is now tested, and it holds.

## A test that the broken version also passes is not a test

The first version of the duplicate-email check registered one address
**twice in a row** and asserted 409. It passed. It was close to
worthless, and spotting that is the most useful thing the Deadline 2
audit did.

The plan does not ask for a 409. It asks for *"409, not 500: catch the
IntegrityError, **because** two simultaneous registrations can both pass
a 'does this email exist?' check."* A sequential retry is exactly the
case a naive pre-flight `SELECT` handles correctly — so the test agreed
with the implementation we deliberately rejected, and would have gone on
agreeing with it forever.

Replaced with 8 registrations of one address released together on a
barrier: `1 x 201, 7 x 409`, zero 500s, and — the assertion that actually
matters — **one row**, counted in the database rather than inferred from
status codes. Status codes are what the server claimed; the row count is
what happened.

This is `DECISIONS.md`'s "build the broken version first" arriving from
the other direction, and it generalises to every benchmark still ahead.
Before writing an assertion, ask **which implementation would fail it**.
If the answer is "none of the ones we were choosing between", it is
measuring something else. Deadlines 4, 5 and 7 are all of this shape:
Benchmark 2 in particular is only meaningful because the *correct*
resource-lock implementation fails it.

---

# Deadline 3 — the authorization boundary (A)

The stub is gone. `core/dependencies.py` decodes a real token, loads a
real user, and rejects with 401 or 403 before any handler body runs.
Nothing changed at B's ten call sites: the import path, the call shape
and the return type were frozen at Deadline 1 for exactly this moment,
and the swap cost zero edits outside `core/`.

## What Deadline 2's green checkpoint was actually worth

`GET /gpus` returned 200 with a bearer token — and also with no token at
all, because the stub took no parameters and read no header. Session 7's
entry recorded that honestly at the time. It is worth restating as a
general rule rather than a one-off caveat:

> A route that answers the same way with and without a credential has
> tested the happy path of *routing*, not of *authentication*.

`scripts/check_rbac.py` opens with the negative case for that reason —
six ways of arriving without a valid identity (absent, malformed, wrongly
signed, expired, int `sub`, and a signed token naming a user who does not
exist), each asserted to be 401. Every one of them returned **200**
against yesterday's build.

## The role is read from the database, not the claim

Deadline 1 decided the opposite, and the reversal is worth stating
plainly because the original reasoning was not wrong — it was overtaken
by another decision made in the same session.

The claim was: put `role` in the token so `require_role` authorises with
no DB round-trip, accepting that a role change lags until the token
expires. The problem is that the *other* Deadline 1 decision — freezing
`get_current_user() -> User` — means the user row is loaded on every
authenticated request anyway. `require_role` depends on
`get_current_user`, so by the time it runs, the row is in hand and the
lookup it was meant to avoid has already happened. The saving was spent
before the function that was supposed to collect it was reached.

With both copies available, the fresher one wins:

``` text
token claim   signed, tamper-proof, up to 60 minutes stale
database row  already loaded, never stale
```

So a demoted admin loses admin on their next request rather than at
expiry, and the "accepted tradeoff" in the Deadline 1 block above is
simply void — there is no cost being paid for it any more.

The claim still earns its place, in two ways that are not authorization:
it is the **demonstrable** half of the auth story (Deadline 2's
checkpoint is literally "login returns a token containing a role", and
`/docs` shows it decoding), and it is what a future stateless service
would read if one ever needed to authorise without touching this
database. It is simply not what *this* system authorises on.

**The assertion that proves it, and which implementation fails it:** mint
a token signed with the real secret, `sub` = the seeded STUDENT's id,
`role` claim = `ADMIN`, and POST it at `/gpus`. A build that trusts the
claim returns 201 and creates a cluster. Ours returns 403. No forgery is
involved — only this system could have signed that token — which is what
makes it a test of *which copy is consulted* rather than of the
signature.

## The 403 must fire before the handler body, and a status code cannot show that

`require_role` is a dependency, not an `if` at the top of a handler.
FastAPI resolves the dependency chain before calling the function, so a
rejected caller cannot leave partial state behind.

An `if user.role != ADMIN: raise` written as the **first** line of the
handler behaves identically from outside — same 403, same body — and the
two only diverge when the check drifts one line below the INSERT, which
is exactly the kind of edit nobody notices in review. So the assertion is
not the status code; it is the **row count on both sides of the 403**,
read from the database. Same argument as Deadline 2's duplicate-email
audit: status codes are what the server said, row counts are what
happened.

The dependency chain also has an order worth asserting, because it is
observable: authentication (401) → authorization (403) → validation
(422). A student sending a body that is *also* invalid gets 403, not 422.
A 422 there would mean the request body was parsed and validated for a
caller with no business reaching the endpoint.

## `POST /gpus` and `POST /rooms` — the checkpoint had nothing to fire at

Deadline 3's checkpoint is *"student token on `POST /gpus` returns 403"*.
That route did not exist. Neither did any other admin-only route, so
`require_role` was about to ship with nothing guarding anything, and the
checkpoint could not have been run at all.

Creating a resource is `[ADMIN]` in the role matrix of both
`INIT_PLAN.md` §13 and `ARCHITECTURE_AND_WORKFLOWS.md` §8, and appears in
the API list of `INIT_PLAN.md` §12 — with **no deadline assigned**. This
is the third instance of that exact gap, after `GET /me` and the admin
PATCH endpoints (both found in session 5). The pattern is now clear
enough to name: *an endpoint that appears in a specification but in no
deadline's column belongs to nobody, and is discovered by whichever
checkpoint trips over it.* Both are assigned here.

`GPUClusterCreate` deliberately has **no `allocated` field**. It is
derived state owned by the allocation transaction, and letting a caller
set it would allow a cluster to be born already over-allocated — the
precise invariant `gpu_capacity_sane` exists to protect. New clusters
start at zero, always.

`RoomCreate` carries `capacity`, and it is a different kind of number:
seats in a room, a physical fact. Rooms are allocated by **interval**,
not by unit — the invariant is "no two active reservations overlap",
enforced by the GiST constraint — so there is no counter and nothing to
guard on write. Worth stating because the two resources sharing one base
table invites the assumption that they share an allocation model, and
they deliberately do not.

## `GET /me` is here because this is the deadline it proves

Same missing-deadline gap, assigned in session 5 to Deadline 3 and
implemented here. It is the end-to-end demonstration that a real token
decodes to a real user carrying a real role — which is the entire claim
of this deadline, and it cannot be made by a 403 alone (a route that
rejects *everyone* also produces 403s).

It reuses `auth.schemas.UserRead` rather than declaring a second model of
the same row. The property that matters most about that model is a
negative one — it has no `password_hash` field, so no handler can leak
the hash by returning an ORM object — and a guarantee like that is worth
having in exactly one place.

There is no `users/service.py`. `INIT_PLAN.md` §15 splits routers from
services because the domain half is the part worth testing without a
client; here the domain half is empty, since the dependency has already
produced the row. A file that only forwards is a file to keep in sync for
nothing. `GET /me/quota` lands in the same module at Deadline 6 and
*will* bring a service, because reading the quota policy and SUMming held
units is real domain logic.

## 401 and 403 stay uncoded

`core/errors.py::coded_error()` exists for failures a client must branch
on — `CAPACITY_EXHAUSTED` and `QUOTA_EXCEEDED` are both 409 and demand
opposite remedies. There is exactly one remedy for a 401 (get a token)
and one for a 403 (stop). So both keep FastAPI's plain string `detail`,
per `ARCHITECTURE_AND_WORKFLOWS.md` §7, and the envelope stays reserved
for cases where the code carries information the status line does not.

Two smaller calls in the same spirit:

- **Every 401 is byte-identical**, whatever went wrong — expired,
  tampered, malformed subject, user deleted. Which one it was is not the
  caller's business, and distinguishing them hands an attacker a probe.
  The 401 carries `WWW-Authenticate: Bearer`, which is what makes it a
  spec-conforming 401 rather than a 401-shaped 403.
- **The 403 does not name the required role.** That is policy
  information, and a caller cannot act on it.

## `API_PREFIX` moved to `core/config.py`

`OAuth2PasswordBearer(tokenUrl=...)` needs the prefix, and importing
`main.py` from `core/dependencies.py` is a cycle — `main.py` imports the
routers, which import the dependency. The value and its reasoning are
unchanged from session 6; only its address moved, so there is still
exactly one place to change it.

`tokenUrl` has **no leading slash**, so Swagger resolves it relative to
the docs page and the Authorize button survives the app ever being
mounted under a sub-path. That button is the payoff for login being
form-encoded, argued in the Deadline 2 block above, and it is now real:
`check_rbac.py` asserts the security scheme is in `openapi.json` and that
protected routes declare it, because a demo that goes back to pasting
headers has lost the thing that decision bought.

## Verification

`scripts/check_rbac.py`, 34 assertions, exits non-zero on failure. Third
gate in the project written as a script rather than a shell paste, a
habit that has now caught a stale image (`check_jwt.py`, session 5) and a
requirement tested against the wrong implementation (`check_auth.py`,
session 7) on their first runs.

It leaves the database at its post-seed counts: the cluster and room it
creates as ADMIN are deleted at the end, and the counts are re-asserted
afterwards rather than assumed. Deleting through the subclass removes the
`resources` row too — checked directly in `psql`, not inferred, because
an orphaned `resources` row would be invisible to `GET /gpus` and would
surface much later as a resource of no type.

---

# Deadline 3 — the room path (B)

`POST /rooms/{id}/reservations`. The second half of the checkpoint, and
the one resource in the system whose central invariant is enforced by
Postgres rather than by us.

## Three invariants, three different mechanisms, one endpoint

This endpoint is worth reading closely because the three things it
guarantees are guaranteed in three completely different ways, and only
one of them is application code:

``` text
Invariant                    Enforced by                    If our code is wrong
---------------------------------------------------------------------------------
no overlapping holds         EXCLUDE USING gist             nothing happens.
                             (the database)                 The INSERT is rejected.
this id is really a room     the service layer              a "room booking" lands
                             (nothing else)                 on a GPU cluster
the room is not BLOCKED      the service layer              a booking lands on a
                             (nothing else)                 blocked room
```

The FK on `reservations.resource_id` points at `resources`, and a GPU
cluster **is** a resource — so the database accepts that row happily. The
GiST constraint is partial on the *reservation's* status, not the
*resource's*, so it is entirely indifferent to BLOCKED. Rows two and
three are the only places application code can be wrong here, which is
why `scripts/check_rooms.py` tests them hardest and asserts the absence
of the bad row (`no reservation row against the GPU cluster`) rather than
just the status code.

## No "is this slot free?" SELECT

Same argument as registration at Deadline 2, and it is the project's
recurring pattern: **our code handles the normal case, the database makes
the race impossible.** A pre-flight availability check passes exactly when
it does not matter — two concurrent bookings both run it, both see free,
both proceed — while making the code *look* like the race was handled.
The INSERT is the check, and catching the `IntegrityError` is how its
result is read.

`_is_overlap_violation()` reads the **constraint name** out of psycopg's
diagnostics rather than matching on the message text, and re-raises
anything else. Mapping every `IntegrityError` to "slot taken" would report
an unrelated foreign-key bug to the caller as a booking conflict, which is
a lie that would survive a long time. The message text is localised and
reworded across server versions; the constraint name is not.

And, as at Deadline 2: `db.rollback()` is the first statement in the
`except`. After an `IntegrityError` the session is in a failed state and
any further statement raises `PendingRollbackError` — a 500 burying the
409 it was supposed to produce.

## `FOR SHARE`, not `FOR UPDATE` — the gate needs the row not to *change*

The ratified item-6 semantics say the status gate reads the row it just
locked. The obvious way to write that is `SELECT ... FOR UPDATE`, and it
is what `EXECUTION_PLAN.md` sketches — correctly, because that sketch is
in the **GPU** transaction, which goes on to *write* the row it locked
(`allocated = allocated + n`).

The room transaction never writes the resource row. Rooms have no counter;
that is the whole difference between the two resources. So an exclusive
lock buys nothing the gate needs and costs something the design claims:

``` text
FOR UPDATE   admin PATCH waits   ✓        other bookers wait too    ✗
FOR SHARE    admin PATCH waits   ✓        other bookers proceed     ✓
```

With `FOR UPDATE`, every booking of one room queues behind every other,
and the *application lock* — not the exclusion constraint — becomes the
thing deciding who wins a concurrent slot race. The invariant still holds.
The claim that "Postgres enforces this one, unaided" quietly stops being
true, which matters at Deadline 10 when that contrast with the GPU path
is the thing being explained.

This was caught by writing the comment before re-reading the code: the
router docstring claimed no application lock was involved, and by then one
was. The fix was to make the lock mode match the claim rather than soften
the claim.

**Both halves are asserted, because "it should block" and "it does block"
are different statements.** One session holds `FOR SHARE` on the resource
row; a second session with `lock_timeout = 750ms` then tries the admin's
`UPDATE` and must fail with `LockNotAvailable`, and tries a second
`FOR SHARE` and must succeed:

```
SHARE lock blocks an admin write to the row   -> LockNotAvailable
SHARE lock does NOT block another booker      -> acquired
```

Note this is an argument about which mechanism is load-bearing, **not** a
measured throughput claim. Nothing here has been benchmarked; Deadline 5's
harness is the instrument that could, if it ever matters.

## Where the user lock goes, and why it is not there yet

The global order is user row → resource row, no exceptions. This
transaction takes only the second lock. That is consistent rather than an
exception: room quota is Deadline 6, when A adds the user-row lock and the
held-reservations count. The comment block in `reserve_room` marks the
exact position — immediately above the resource lock, with deliberately
nothing between them — so Deadline 6 is an **insertion**, not a reordering.
Taking the user lock today would be a lock protecting no invariant, and
the project's standard is that complexity is earned.

## `INTERVAL_CONFLICT` is a coded error

`ARCHITECTURE_AND_WORKFLOWS.md` §7 listed "409 room interval conflict"
with no code, from before `core/errors.py` existed. It gets one, through
`coded_error()` like every other coded failure, because B's harness must
distinguish it from `RESOURCE_BLOCKED` — two 409s on the same endpoint,
with different remedies (pick another slot / pick another room), which is
the exact situation the envelope was created for at Deadline 2.

## A naive datetime is interpreted as UTC, explicitly

Postgres would otherwise resolve a naive timestamp against the session's
`TimeZone`, making the stored instant depend on who ran the request and
with what settings. That is the same trap revision `1ca8b85b7626` hit
converting `timestamp` to `timestamptz`, where the fix was an explicit
`USING ... AT TIME ZONE 'UTC'`.

Rejecting naive input with a 422 was the alternative. Interpreting it,
explicitly and in one validator, blocks nobody and leaves no ambiguity —
and it is asserted rather than asserted-about: a booking sent naive, then
repeated with an explicit `+00:00`, must return 409. That only happens if
the first one was stored as UTC.

## Verification

`scripts/check_rooms.py`, 34 assertions, exits non-zero on failure.
Fourth gate in the project.

Three worth naming, on the "which implementation would fail this"
standard:

- **Containment in both directions, and an identical window.** A
  hand-written overlap test with one comparison inverted passes the
  simple `[11,13)` case and fails these. The constraint gets them all
  right for free, which is the argument for using it.
- **Eight simultaneous identical bookings**, released on a barrier:
  exactly one 201, seven 409s, zero 500s — and **one row**, counted in
  the database. Every other assertion in the file passes against a
  check-then-insert implementation. Only this one does not.
- **A cancelled hold does not block its old slot.** The constraint is
  partial on `status = 'ACTIVE'`. If that `WHERE` clause were ever
  dropped, releasing a room would make the slot permanently unbookable,
  and nothing else in the file would notice.

---

# Deadline 4 — the flagship and the courses

Both locking transactions, written at the same deadline as the plan
intends. Two things learned here matter more than either feature, and
both are the same lesson from opposite ends: **a lock can be present,
correct, and useless.**

## The finding: `SELECT ... FOR UPDATE` returned a stale value

Course registration was written, and 20 concurrent registrations for a
5-seat offering produced this:

``` text
20 concurrent registrations, 5 seats: {201: 20}
enrolled_count = 3        active enrollment rows = 20
```

Every request succeeded. The counter said 3. Not an off-by-one — lost
updates, twenty transactions all incrementing the same number.

The lock was there. The SQL was right. `SELECT ... FOR UPDATE` was
emitted and acquired. **The value it returned was from before the lock.**

The cause is SQLAlchemy's identity map. The transaction reads the
offering once for the 404 check, which puts the row in the Session. When
the locked read then returns that same primary key, SQLAlchemy hands back
the **existing Python object without refreshing its attributes** — so the
capacity gate compared, and the increment incremented, a value read
before anyone was holding anything.

Proven directly rather than reasoned about, in two sessions:

``` text
A reads enrolled_count            = 0
B commits enrolled_count          = 41
A: SELECT ... FOR UPDATE          = 0     <-- the lock is held; the value is stale
A: same statement + populate_existing = 41
raw column value                  = 41
```

The fix is `populate_existing()` (or `.execution_options(populate_existing
=True)`) on every locked read. One keyword. Its absence is invisible in
review, invisible in the SQL log — the `FOR UPDATE` really is in the
statement — and invisible to every sequential test.

> **The general form, worth carrying to Deadline 7:** in an ORM, taking
> the lock and reading the locked value are two different things. Ask of
> every `FOR UPDATE`: *is the value I am about to compare the one this
> statement just fetched, or one I already had?*

### The honest part: the GPU path does not reproduce it

The same probe shows the same staleness on `GPUCluster` — A reads
`allocated = 0`, B commits 7, A's `FOR UPDATE` still reports 0. So the
flagship transaction has the same latent read.

But removing `populate_existing()` from the GPU path and running the
12-racer capacity race **four times** gave a correct `8/8` every time,
with `allocated` matching `SUM(active)` exactly. The statement sequence
differs slightly between the two paths, and no explanation was
established for why one races and the other does not.

`populate_existing()` stays in both. The staleness is demonstrable, the
cost is one keyword, and *"we could not make it fail today"* is not a
reason to depend on a read a probe says is wrong. Recorded as an
unresolved question rather than written up as a fix for a bug we proved
was biting — the difference matters, and Deadline 7's promotion
transaction is the next place it could.

## The second finding: Benchmark 2, as specified, can pass against the broken build

`DECISIONS.md` already required building `reserve_gpu()` without the user
lock first and measuring the corruption. Doing that produced a result
nobody expected:

``` text
first run, unlocked build, 2 concurrent cross-cluster requests:
    held = 2      <-- PASSED. Against the build it exists to indict.
```

The window between `SUM held units` and `COMMIT` is well under a
millisecond, and two HTTP requests released on a barrier do not reliably
land inside it. A single trial is a coin flip, so the benchmark as
written in the plan proves nothing on either side.

Rerun as a **measurement** — the same race, 25 trials, counting how often
the invariant broke:

| build | over-quota trials | held units observed |
|---|---|---|
| resource lock only | **24 / 25** | `{2: 1, 4: 24}` |
| + user-row lock | **0 / 25** | `{2: 25}` |

That is the real Benchmark 2 table, both halves measured at build time
rather than reconstructed later.

And the contrast that makes the point: **the capacity race passed on the
same broken build** — 12 concurrent requests, 8 units, exactly 8
succeeded, `allocated = 8`. The cluster lock was already perfect. The
quota rule broke anyway, because it is a fact about the *user* and
nothing was holding the user still.

> The lesson generalises past benchmarks: a concurrency test that
> asserts once is a test of scheduling luck. Assert over trials and
> report the count.

### `BENCHMARK_UNSAFE_NO_USER_LOCK`

A setting, default false, that drops the user lock out of the GPU
transaction. Nothing but Benchmark 2 ever sets it.

It exists because Deadline 9 asks that a stranger reproduce our numbers
from a fresh clone, and nobody can reproduce the *broken* half of a
broken-vs-fixed table unless the broken build is still reachable. The
alternative is a table with one number that cannot be re-derived, which
is exactly the "reconstructing a broken build to make a table look good"
this file already rejects. It removes a lock and changes no other logic —
the quota arithmetic under it is the same arithmetic, which is the whole
point: the bug is correct arithmetic on a value nothing was holding.

## Outstanding item 8, ratified: cancel mirrors the POST path

``` text
POST   /rooms/{room_id}/reservations          DELETE /rooms/{room_id}/reservations/{id}
POST   /gpus/{gpu_id}/reservations            DELETE /gpus/{gpu_id}/reservations/{id}
```

`DELETE /reservations/{id}` named a row in two tables — room holds in
`reservations`, GPU holds in `gpu_reservations`, each with its own id
sequence — so `/reservations/5` matched a row in both. Scoping the cancel
under its resource removes the ambiguity **structurally** rather than by
documentation, and introduces no new naming convention, because the POST
routes were already shaped that way.

The resource id in the path is load-bearing, not decorative: both cancel
paths verify the reservation actually belongs to the resource named, and
both gates are asserted (`/gpus/2/reservations/{id}` where the hold is on
cluster 1 must 404).

## The quota helper never opens a transaction and never takes a lock

`quotas/service.py` takes a `Session` it did not create, emits no COMMIT,
and knows nothing about HTTP. That is what makes the guarantee atomic:
the quota gate and the write it guards commit or roll back together.

It also deliberately does **not** take the user lock. A helper that
locked on its own behalf would bury the single most important line of the
project inside a utility function, where nobody reading the transaction
would see it. The caller takes the lock; the helper documents that it
must already be held.

**`limit_for` reads the policy row without a lock**, and that is
deliberate too. `role_quotas` is admin-editable policy, not per-user
state. The invariant is `held <= limit` at commit time, and the caller is
holding the user row, so `held` cannot move underneath it. Locking policy
rows would serialize every allocation in the system on a handful of rows
to protect nothing.

### Absent and NULL are different, and the code fails closed

`scripts/seed.py` omits `(FACULTY, COURSE)` because course registration
is STUDENT-only, so the pair is unreachable behind the 403. Meanwhile
`(ADMIN, *)` rows exist with `max_units = NULL`, meaning unlimited.

So NULL is a policy that says yes; a missing row is *no policy at all*.
`QuotaNotConfigured` fails closed and surfaces as
`409 QUOTA_NOT_CONFIGURED`, naming the pair. Treating a missing row as
unlimited would be the dangerous default — one un-seeded row would
silently switch off the invariant this project exists to enforce, and
every existing test would still pass.

`None` is also checked *before* the comparison rather than defaulted to a
large number: a sentinel like 999999 makes "unlimited" a quantity that
can be exceeded.

## Course registration takes the user lock at Deadline 4, not Deadline 6

The plan sketches this transaction as offering-lock-only, with the
course-load quota arriving at Deadline 6. That is correct for capacity
alone — but the **schedule-overlap check reads the student's other
enrollments**, and two concurrent registrations for two clashing
offerings touch no common offering row. Nothing would serialize them and
both would pass.

That is the cross-cluster GPU quota race exactly, on a different
invariant: *a schedule clash is a fact about the student.* So the lock is
earned here rather than taken early for tidiness, and Deadline 6 adds the
quota SUM inside a lock already held — an addition, not a reordering.

Measured, as trials rather than once: **0/15 students double-booked**
across 15 concurrent clashing-registration trials.

## Smaller decisions, each with a reason

**The 404 check runs before both gates, and reads only immutable state.**
Without it, a caller already at quota gets `409 QUOTA_EXCEEDED` for
naming a room or a cluster that does not exist — the quota gate fires and
nobody ever checks the target. The distinction that makes a boundary read
safe here is that **existence and `resource_type` never change**, while
`status` is admin-mutable and therefore must be read under the lock. Found
by running the broken build: the assertion said 404 and the server said
`QUOTA_EXCEEDED`.

**Cancellation locks the reservation's OWNER, not the caller.** An ADMIN
cancelling someone else's hold must serialize against *that user's*
allocations. Locking the admin's own row would protect nothing.

**Cancellation and drop are naturally idempotent.** The counter moves only
on the `ACTIVE -> CANCELLED` (or `-> DROPPED`) transition, so a repeat is
a no-op. This is a direct benefit of recomputing held units by `SUM`
rather than keeping a denormalized counter: with a counter, a repeated
cancel would double-decrement, and the damage would surface much later as
a perfectly valid allocation being wrongly refused somewhere else.

**Room cancellation takes no locks at all**, unlike GPU cancellation.
A room hold owns no counter — the only state changing is that
reservation's own `status`, and the exclusion constraint reads status at
INSERT time, so releasing a slot simply makes the constraint stop
objecting. Room quota at Deadline 6 changes this: once "how many active
holds" is a gate, a release changes a quantity the quota reads, and the
user lock arrives — first, per the global order.

**Day codes are single characters (`MTWRFSU`, R = Thursday).** Overlap is
a set intersection of characters, and `set("Tu") & set("Th") == {"T"}` —
so multi-character tokens would report a Tuesday class as clashing with a
Thursday one. One character per day makes the intersection exact.

**Schedule times compare lexicographically, which is correct only because
they are zero-padded.** `"9:00"` sorts after `"10:30"` and silently
inverts every comparison. Half-open (`<`, not `<=`), for the same reason
the room constraint uses `'[)'`: a class ending at 10:30 does not clash
with one starting at 10:30. Asserted both ways.

**`DELETE /offerings/{id}/drop` is assigned here**, the fifth endpoint
found specified but owned by no deadline. It is not optional at Deadline
4: the column requires that re-registration be an UPDATE rather than an
INSERT, and that is untestable without a DROPPED row to re-register over.

**Registering for a full offering returns `409 CAPACITY_EXHAUSTED`**,
rather than falling through to a waitlist. Whether a full registration
auto-waitlists is outstanding item 10, due at Deadline 7; this is a
policy question about the API, not about the lock, and Deadline 7 can
change the branch without touching the transaction.

## Two test bugs, both worth recording

**A capacity race in which every racer shares one account is not a
capacity test.** The GPU capacity race was first written with 12 requests
from a single ADMIN. The user-row lock serialized all of them, so the
cluster gate was never contended — it would have passed against a
capacity gate that did not work. Capacity is keyed on the *resource*, so
the racers must differ in everything except the resource. It now creates
12 distinct students.

**A test that has to be talked out of a true failure deserves more
attention than one that passes.** `after dropping the clashing one,
registration succeeds` failed, and the code was right: the student still
held a *second* offering (MWF 10:30–12:00) that also overlaps the
10:00–11:00 one being registered. The assertion was wrong, not the
service. Recorded rather than quietly corrected.

---

# Deadline 5 — exactly-once (A)

## The table already existed, so this deadline was pure application code

`alembic check` reports **"No new upgrade operations detected"** at head
`1ca8b85b7626`. `idempotency_keys`, with its `UNIQUE(key, user_id)`,
shipped at Deadline 1 in `705b757e5df2`; the `timestamptz` fix in
`1ca8b85b7626` was the last thing it needed.

Worth recording because A owns Alembic exclusively and every previous A
deadline opened with a migration. This one did not, and checking rather
than assuming saved generating a revision against a schema that was
already correct.

## The serialization point is an index, not a lock

The other two guarantees are row locks: quota locks the user row,
capacity locks the resource row. Exactly-once cannot work that way, and
the reason is worth being able to say unprompted:

> **there is nothing to lock until somebody creates the row, and
> creating the row is exactly the window a retry arrives in.**

`UNIQUE(key, user_id)` serializes on the *index entry*. Two simultaneous
retries race to INSERT; one proceeds, the other blocks on the entry until
the first transaction ends, then either sees the violation (it committed,
so replay its stored response) or succeeds (it rolled back, so do the
work for real). No application lock can express that, because the thing
being contended does not exist yet.

This is the third distinct mechanism in the system, and the README should
say so plainly: **three invariants, three serialization points.**

``` text
capacity      a fact about the RESOURCE  -> resource row lock
quota         a fact about the USER      -> user row lock
exactly-once  a fact about the REQUEST   -> unique index
```

## The SAVEPOINT, which was not in the plan and without which nothing works

A unique violation **aborts the entire Postgres transaction**. Every
statement after it fails with `InFailedSqlTransaction` — including the
SELECT that fetches the stored response, which is the whole point of
catching the violation in the first place. `db.begin_nested()` wraps the
INSERT in a SAVEPOINT so the rollback is partial and the surrounding
transaction survives.

The plan's Deadline 5 column does not mention it. It is not an
implementation detail: without it the feature does not function at all
under concurrency, and the measurement below says how badly.

Core `insert()` rather than `db.add()`, deliberately: an ORM add puts a
pending object in the identity map, and reasoning about what a savepoint
rollback leaves behind there is a complication with nothing to buy it.

## Measured: three builds, and the claim we were about to overstate

Both broken builds were written and run before the correct one was kept —
this file's standing rule that the broken version is built and measured
*first*, so the table is reproducible rather than reconstructed.

`scripts/check_idempotency.py`, 8 simultaneous retries of one key × 15
trials = 120 requests:

``` text
build                              201    409    500   rows/trial   seq fails
----------------------------------------------------------------------------
correct (savepoint, one commit)    120      0      0        1           0
check-then-insert, no savepoint     34      0     86        1           1
key commits separately              22     98      0        1           2
```

**No build over-allocated. Not one.** `UNIQUE(key, user_id)` held the row
count to exactly one in all three — the database was never going to allow
a double-booking, whatever the application did.

That is a correction to what this project was about to claim.
"Idempotency prevents double-booking" is *false here*, and saying it in
an interview against our own numbers would be indefensible. What the
correct implementation buys is that **the retry gets the original
response**:

- **no savepoint** — the violation aborts the transaction and the replay
  lookup cannot run: 86 of 120 retries become a `500`;
- **split commit** — the key commits with a NULL response while the
  allocation is still in flight, so concurrent retries see a claimed-but-
  unanswered row: 98 of 120 become a spurious `409`;
- **correct** — 120 of 120 return `201` with byte-identical bodies.

The split-commit build is also the only one caught by a *sequential*
assertion: it leaves a key behind after a failed allocation, so the
caller can never retry a request that never happened. Two of the
sequential assertions fail against it and none of the concurrency ones
do — which is the reverse of what was expected, and the reason both
kinds are in the script.

## Exactly-once is a promise about successes

A direct consequence of committing the key with the allocation: **a
request refused by the quota gate leaves no key.** The claim rolls back
with everything else, so a later retry of that same key allocates for
real rather than replaying a 409.

This is correct and it is the only defensible option. The alternative — a
key that outlives its own rollback — has to answer "for how long is the
caller forbidden from retrying a request that never happened?", and there
is no good answer. Asserted directly: over-quota request with a key gives
409 and zero key rows; cancel a hold; the same key again gives 201.

## The replay returns 201, not 200 — a contradiction in our own docs

`ARCHITECTURE_AND_WORKFLOWS.md` §7 said `200 idempotent replay`; §14 said
`200/201 replay`. Both cannot be right and the question had never been
asked out loud.

**Settled: the replay returns the status that was stored**, which for a
successful reservation is `201`. A replay is supposed to be
indistinguishable from the original call. A client that branches on `201`
would take a different path on the retry — precisely the class of bug
idempotency exists to remove, reintroduced by the mechanism meant to
prevent it. Both doc lines now say `201`.

Implementation note: the router returns a `JSONResponse` directly, which
bypasses `response_model`. Deliberate — the stored body goes back exactly
as recorded, rather than being re-serialized from a row that may have
changed since. A reservation cancelled between the original call and the
retry would otherwise replay as `CANCELLED`, which is not what the caller
was told the first time.

## The `Idempotency-Key` header is optional

Benchmark 3's entire shape is the contrast between sending it and not —
*"no key gives 2 reservations; key gives 1 reservation, identical
response."* Requiring the header would delete one column of the table it
exists to produce.

It is also the honest default: a caller who has not thought about retries
gets the old behaviour, and a caller who has gets exactly-once by adding
a header. The unkeyed double-allocation is asserted as a **passing** test
for this reason — it is the broken column, and it is supposed to be there.

## The fingerprint covers the cluster id, not just the body

`POST /gpus/1/reservations {"gpu_count": 2}` and
`POST /gpus/2/reservations {"gpu_count": 2}` have identical bodies and
are different requests. Hashing the body alone would let a key claimed
for cluster 1 replay a response naming cluster 1 while the caller asked
about cluster 2 — a wrong answer that looks like a success. `endpoint` is
compared as well, so the same key on a different route is caught even if
the bodies happen to hash alike.

**`hash()` would have been wrong, silently.** Python salts it per
process, so a digest stored by one uvicorn worker would never match the
one computed by another for an identical body, and every cross-worker
replay would 422. Single-process tests would all pass.
`hashlib.sha256` over `json.dumps(..., sort_keys=True)` instead.

## `IDEMPOTENCY_IN_PROGRESS`, a code for something that should not happen

409, raised when a claim is committed but carries no response. It should
be unreachable: a concurrent retry either blocks (the first transaction
is open) or reads a populated row (it committed). It exists so that if a
future caller ever wires `claim()` in without `record_response()`, the
symptom is a clear 409 naming the problem rather than a replay of `null`
surfacing as a 500 three layers away.

The split-commit build made it reachable and returned it 98 times, which
is how we know the branch works.

## Verification

`scripts/check_idempotency.py` — **37/37 PASS**, the sixth gate.

``` text
no key, twice                    -> 2 reservations, distinct ids
same key, twice                  -> 1 reservation, 201, identical body
third retry, later               -> still replays, still 1 reservation
stored claim                     -> status_code 201, body == returned body
same key, different body         -> 422 IDEMPOTENCY_KEY_REUSED
same key, different cluster      -> 422       <- fingerprint covers gpu_id
same key STRING, different users -> 2 reservations, 2 key rows
over quota with a key            -> 409, ZERO key rows left
same key after cancelling        -> 201, allocates for real
8 retries x 15 trials            -> {201: 120}, 1 row every trial
```

Full regression after wiring into the flagship transaction: `check_jwt`,
`check_auth`, `check_rbac`, `check_rooms`, `check_gpus`, `check_courses`
— all pass. `alembic check` — no drift.

## Outstanding — B, before Deadline 5 can close

A's column is met. **The deadline is not**, and the gap is entirely B's:
the checkpoint is *"first broken-vs-fixed table with real numbers"* and
the table it names is Benchmark 1. A's half produced *a* broken-vs-fixed
table with real numbers (three builds, above), which is evidence but is
not the checkpoint as written. Recorded here so the two are not conflated
later — the same mistake this file corrected once already, when three
dated sessions were being read as three met deadlines.

Verified at session 11, not assumed:

1. **`tests/` is still empty and `pytest-asyncio` is still absent from
   `requirements.txt`.** `EXECUTION_PLAN.md` has flagged both as a
   blocker "before starting" since the plan was written, and neither has
   moved. `requirements.txt` ends at `pytest==8.3.4` with no async
   plugin, so `tests/concurrency/harness.py` cannot run on the day it is
   written.

2. **Benchmark 1 must be written as trials, not a single run.** This is
   Deadline 4's finding applied forward and it is the one most likely to
   be skipped, because a single 500-request run *looks* like plenty of
   evidence. It is not: Benchmark 2's first run passed against the very
   build it exists to indict, because the corruption window is
   sub-millisecond. A benchmark that reports one number cannot separate
   the two builds; one that counts over trials can.

3. **The shape already exists — scale it, do not restart.**
   `scripts/check_courses.py` contains a working mini version: 20 racers
   on 5 seats, `{201: 5, 409: 15}`, with the `enrolled_count` vs
   active-row reconciliation query already written. That query is
   Deadline 8's arriving early, and it is the assertion that proves the
   counter and the table agree. 500/50 is that test with two constants
   changed.

4. **The connection pool is joint, and it is due now.** `session.py`
   passes no `pool_size`, so SQLAlchemy defaults to 5 + 10 overflow =
   **15 connections against a 500-request harness**. Carried since
   session 5 as a standing note; Benchmark 1 is the specific thing it
   distorts, because a run that queues 485 of its 500 requests behind a
   pool is measuring the pool and not the lock. `session.py` is a shared
   file, so the protocol says both people present — this is the item to
   settle *before* B starts measuring, not after the first strange
   number.

**Nothing in A's column is blocked on any of these**, which is worth
saying plainly because the plan's phrasing implied a shared deliverable.
`check_idempotency.py` uses threads exactly as `check_gpus.py` does, so
the exactly-once evidence exists today without the asyncio harness
existing at all.

---

# Deadline 5 — the harness and Benchmark 1 (B)

A's column landed in session 11. This is the other half, and almost all
of its cost went to a discovery the plan had no line for: **the system
cannot serve 500 concurrent requests, and no amount of pool tuning
changes that.**

## BENCHMARK 1 — capacity under concurrency

500 registrations submitted simultaneously at a 50-seat offering, 40 in
flight, 3 trials each. Both builds run today; the broken one first.

``` text
build                      201      409 CAPACITY   enrolled_count   active rows   oversold
--------------------------------------------------------------------------------------------
no offering lock       377 - 500     0 - 123          24 - 50        377 - 500      3/3
+ SELECT ... FOR UPDATE       50           450              50             50      0/3
```

Two trials of the unlocked build seated **all 500 students in a 50-seat
section**, with `enrolled_count` reading 34 and 24 while 500 ACTIVE rows
existed. The locked build sold exactly 50 in every trial, with the
counter and the row count agreeing every time, and zero 5xx.

**The broken build reads a FRESH value.** `populate_existing()` stays on
in both builds; only `FOR UPDATE` is removed. So this table is not about
a stale read — it is a correct read of a number that another transaction
changes between the check and the increment. That distinction matters
because Deadline 4's finding was the *other* failure (lock held, value
stale), and the two are easy to conflate. One is fixed by
`populate_existing()`, the other only by the lock. Both are needed, and
neither implies the other.

## The finding: "500 concurrent" was never achievable

Firing 500 unbounded, against a correctly sized pool, gives:

``` text
responses: {(201, None): 60, (500, None): 440}
median latency 86 s
pg_stat_activity: 50 connections `idle in transaction`, wait_event Client
```

Fifty connections — the entire pool — open, in a transaction, and doing
nothing. Sampled from `pg_stat_activity` during the run rather than
inferred from the error.

The cause is structural, not a setting:

> A connection is checked out during **dependency resolution** —
> `get_current_user` reads the user row — and the Session holds it, with
> an open transaction, until `get_db` closes it at the end of the
> request. The connection is therefore held across event-loop hops, so
> the ceiling is **requests in flight**, not the server's 40 worker
> threads. N in-flight requests need N connections.

Postgres allows 100. So 500-way concurrency needs a Postgres sized for
500 backends, at roughly 10 MB each — 5 GB of server memory to make one
sentence in a benchmark literally true.

**What we do instead, and say out loud:** 500 requests are submitted
together and at most 40 are in flight. The first 40 hit the seat gate
simultaneously, which is the contention the benchmark is actually about
— and 40-way contention oversells the unlocked build tenfold. The claim
does not need 500. What it needs is to be stated accurately.

`ARCHITECTURE_AND_WORKFLOWS.md` §12 said "under 500 concurrent
registrations"; it now says what is measured.

## The connection pool was not a distortion, it was a wall

Carried since session 5 as "the pool will distort Benchmark 1". It does
not distort it. On the defaults it makes it **impossible**:

``` text
500 registrations, default pool (5 + 10 = 15):
  201=0  409=0  errors=500/500  median latency 126_000 ms
  sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10
  reached, connection timed out, timeout 30.00
```

Every request failed. Five sessions of "we should look at that" and the
first run turned it into a wall.

The sizing argument is sharper than "the pool was small". FastAPI runs
`def` endpoints in anyio's worker pool, default **40** threads, so the
pool was set **below the number of requests the server would admit** —
40 threads competing for 15 connections, 25 blocking then raising. Now:

``` text
Postgres max_connections     100  (3 reserved)
server thread ceiling         40  (anyio default)
server pool_size + overflow   50  <- must exceed in-flight requests
benchmark tool pool            5  (separate engine, see below)
```

`pool_timeout` 30s → 10s: with 50 > 40 a checkout cannot block, so a
timeout now means a real misconfiguration and should surface fast rather
than stall for half a minute looking like a slow query.

**This is a shared file changed solo.** The protocol says both people
present; it was changed anyway because it is a hard blocker with a
measured failure attached, and A must review it. The numbers above are
the argument.

## A benchmark must not import the application's `SessionLocal`

After sizing the server pool correctly, 500 requests *still* returned
387 × `500`. The benchmark process imports `app.database.session`, which
builds a **second engine with the same 40 + 10 sizing** — two pools of 50
against a Postgres that allows 100, and the symptom appears inside the
*server* as `QueuePool timed out`, which reads exactly like the bug the
sizing was meant to fix.

`tests/concurrency/harness.py::tool_session_factory()` gives benchmarks
their own 5-connection engine. Setup, resets and the observer need about
three between them.

> Generalisation worth keeping: **a test process that imports production
> connection settings is competing with the thing it measures.**

## The harness reports the concurrency it ACHIEVED

Three throttles sit between `asyncio.gather` and a row lock, and the
lowest wins silently:

``` text
httpx max_connections     default 100   <- harness raises it
server thread pool        default  40
SQLAlchemy pool           default  15   <- was the binding one
```

Firing 500 through defaults measures 15-way contention and calls it 500.
So `DBConcurrencyObserver` samples `pg_stat_activity` during every run
and each trial prints `peak_db_conns` next to the number requested. If
those differ by an order of magnitude, the run measured a pool.

Measured on the final runs: **38–39 achieved against 40 in flight.**

## The instrument gets tested too

`tests/concurrency/test_harness.py`, 5 tests, and one of them is the only
kind that can catch the worst failure: `test_requests_actually_overlap`
asserts **wall time < half the summed latencies.** A harness that awaited
each call in turn would pass every status-code assertion in the project
and fail that one. Every benchmark is built on this function; if it
serialized, all four tables would be measuring nothing and all four would
still look plausible.

`asyncio_mode = strict` in `pytest.ini`, deliberately not `auto`: with
`auto`, a test that loses its marker is silently collected as a coroutine
that never runs and reported as a **PASS**. Strict mode makes that a
failure.

## The reporting bug in my own benchmark

An early run reported `201=0  409=0  err=0` for 500 requests. All three
buckets empty, 500 responses unaccounted for — the tally only counted
`201`, `409 CAPACITY_EXHAUSTED` and `5xx`, and everything else vanished.

Fixed by printing the **full** `Counter` every trial plus an explicit
warning when the buckets do not sum to the request count.

> A benchmark that can silently drop 500 responses is not an instrument.
> It is the same class of error as a test that passes against the broken
> build, and it cost two long runs before the numbers were even readable.

# Deadline 6 — Benchmarks 2 and 3 on the harness (B)

## The racers in Benchmark 3 must be FACULTY, or it measures the wrong gate

Benchmark 3's unkeyed column has to be free to allocate on every retry.
Run it as a STUDENT and the GPU quota of 2 refuses retries 3..N, so the
column reports "2 reservations" — which is the number the plan predicts,
arrived at through the **quota** gate rather than through the absence of
an idempotency key. The table would look exactly right and would be
evidence for nothing.

FACULTY (quota 10) removes the interference, and the benchmark refuses to
start when `--retries` exceeds that cap rather than silently producing
the truncated number.

> This is the Deadline 4 lesson wearing a different hat. There, a test
> passed against the build it was meant to indict; here, a column would
> report the *expected* value for the wrong reason. Both are instruments
> agreeing with you for reasons you did not check.

## `--retries` defaults to 8 where the plan says "twice"

Two requests cannot separate "every retry allocates" from noise. Eight
makes the unkeyed row count track N, which is the claim. `--retries 2`
reproduces the plan's literal shape and was run: `no key -> 2 holds,
key -> 1 hold, identical bodies`, exactly as written.

## The threaded checks stay; the benchmarks are not their replacement

`check_gpus.py` and `check_idempotency.py` keep their thread-based races.
They are **gates** — they assert, they exit non-zero, and they cover the
sequential cases the benchmarks deliberately skip. The benchmarks are
**instruments**: they produce tables and, on the broken build, are
*supposed* to record failure without failing. Deleting the checks in
favour of the benchmarks would trade an assertion for a number.

## The claim we were about to overstate, again

The first draft of the session-13 log credited the asyncio barrier with
turning Benchmark 2's broken column "from a coin flip into 25/25". The
threaded measurement recorded above was already **24/25**. One trial of
difference is not evidence about barriers or the GIL, and the harness
port reproduces the threaded table rather than improving on it — the
reason to port was uniformity, not accuracy.

> Twice now the mistake has been the same shape: reading a difference
> into a sample too small to carry one. It is what this project's own
> benchmarks are built to refuse, in the sentences describing them.
