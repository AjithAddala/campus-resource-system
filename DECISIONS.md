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

> ~~ACTION: the architecture doc still lists `start_time`/`end_time` on
> `GPUReservation`. It must be corrected — a stale doc is what makes
> someone "helpfully" re-add the column on Day 6.~~
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

**The migration chain could not be re-run from empty.** Found on Day 2
while verifying Day 1's "migration applies cleanly on a clean DB"
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

The Day 1 note above says the duplicated DDL "silently did not execute."
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

> Lesson, sharper than Day 1's version: `alembic upgrade head` on a
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
`gpu_reservations.gpu_cluster_id` (not `cluster_id`). Relevant to Day 2's
`auth/service.py`.

## Outstanding — ~~B~~ to fix before Day 3

> **Mislabelled.** Items 1–5 are all schema changes, and schema changes
> require an Alembic revision, which **only A may create** (see Ownership
> above). B could not have done any of them. Items 6 and 7 are decisions,
> not code. Corrected: 1–5 were A's, all now done in `c86676652ca2` and
> `1ca8b85b7626`; 6 and 7 remain open and are joint calls.

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
   because that query does not exist yet; re-check on Day 3 when it does.
   Added `ix_reservations_user_id` instead, which nothing covered — "my
   reservations" had no index at all and was not on this list.
4. ~~**`created_at` resolved to `timestamp without time zone`** on
   `Reservation`, `GPUReservation`, `Enrollment`, and `WaitlistEntry`,
   because no explicit type was given. Should be
   `DateTime(timezone=True)`.~~
   **DONE — `1ca8b85b7626`.** Five tables, not four: `idempotency_keys`
   was missed by this note.
5. ~~`users` has no `created_at` at all.~~ **DONE — `1ca8b85b7626`.**
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
`GPUCluster.allocated`, so the Day 4 GPU transaction and the Day 6 course
transaction now have *the same shape*: `SELECT ... FOR UPDATE` the row
holding the counter, compare against `capacity`, increment, insert.

Without it, the capacity gate would have to be
`SELECT COUNT(*) FROM enrollments WHERE course_offering_id = ? AND status
= 'ACTIVE'`, which counts rows in a table other concurrent registrations
are inserting into. There is no row to lock, so `FOR UPDATE` has nothing
to take, and two registrations can both read 49 against a capacity of 50.
Locking the offering row is what makes Benchmark 1 winnable.

`enrolled_count` is therefore derived state and can disagree with
`enrollments` if any code path updates one without the other. The rule:
**every write to `enrollments` happens in the same transaction as the
matching `enrolled_count` update.** Day 6 should end with a reconciliation
query proving the two agree after Benchmark 1:

```sql
SELECT o.id, o.enrolled_count, COUNT(e.id) FILTER (WHERE e.status = 'ACTIVE')
FROM course_offerings o LEFT JOIN enrollments e ON e.course_offering_id = o.id
GROUP BY o.id HAVING o.enrolled_count <> COUNT(e.id) FILTER (WHERE e.status = 'ACTIVE');
```

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

**Verified** (per the Day 1 rule — query the database, do not trust
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

Seeds and read endpoints (Day 2, B) must set `capacity` and
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
2. Day 2's seed script either commits each waitlist insert separately, or
   sets `created_at` explicitly per row.

> Note: `created_at` is still `timestamp without time zone` (outstanding
> item 4). It is now an ordering key, which does not break — every value
> comes from the same server clock — but item 4 is worth doing before it
> is also a value we return in API responses.

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
