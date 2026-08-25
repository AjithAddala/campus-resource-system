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
7. ~~Confirm whether `EnrollmentStatus.WAITLISTED` is ever used.~~
   **RATIFIED at Deadline 7: it is never written, and the enum value
   stays.** A proposed (session 14), B agreed (session 15), and it is
   enforced by construction — no code path writes it. A queued student
   has a row in `waitlist_entries` and nothing else. The value is left in
   the enum because removing it means recreating a Postgres type and
   rewriting the column: a migration and a `models/` change buying zero
   behavioural difference. Revisit only if Deadline 8's integration pass
   wants the enum clean, and then with both people present.
   *(Original text below.)*

   Confirm whether `EnrollmentStatus.WAITLISTED` is ever used. Waitlist
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
9. ~~**The global lock order and the waitlist promotion contradict each
   other.**~~ **RATIFIED at Deadline 7: promotion never waits.**
   A proposed `SKIP LOCKED` (session 14); B agreed (session 15) **with
   one condition**; the condition is met; the code is built and verified.
   A transaction that never blocks on a user row cannot appear in a wait
   cycle, so §14's "every path" claim needs no exception written into it.
   The cost is stated rather than hidden: the promise is *oldest
   ELIGIBLE*, not *oldest*.

   **B's condition, and why it mattered.** A's proposal offered as
   reassurance that `SKIP LOCKED` does not disturb Benchmark 4, because
   its three waitlisted students are idle. B identified that as the
   problem: if no candidate row is ever locked, **the skip clause never
   executes and the mechanism ships unmeasured** — the same shape as
   Benchmark 2 passing against the build it indicts. B required a third
   column holding a candidate's row on purpose.
   **Satisfied:** `benchmark_4_waitlist.py::column_three` (deterministic,
   one run — holding a lock is not a race) and two assertions in
   `check_waitlist.py`, both green: *a candidate whose user row is LOCKED
   is skipped, not waited for*, and *promotion COMPLETES while the row is
   still held*.
   *(Original text below.)*

   **The global lock order and the waitlist promotion contradict each
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
   → **A's proposal is written**, at the end of the Deadline 6 section:
   promotion takes candidate user rows with `SKIP LOCKED` and never
   waits, so it cannot join a cycle and the global order needs no
   exception.
   → **B has responded** (session 15, the section after A's): agreed,
   **on one condition** — Benchmark 4 as specified cannot see the skip
   clause at all, because its waitlisted students are idle and no
   candidate row is ever locked. It gains a third column that holds a
   candidate's row and asserts promotion *completes* while it is held.
   Ratifying still needs both people saying so.
10. ~~**Is joining a waitlist automatic or explicit?**~~
    **RATIFIED at Deadline 7: EXPLICIT** — `POST /offerings/{id}/waitlist`,
    never a fall-through from a full `register`. A proposed (session 14),
    B agreed (session 15), shipped in B's endpoints.
    The argument that settled it: auto-waitlisting would make one `201`
    from `register` mean either "you have a seat" or "you are queued",
    distinguishable only by reading the body — the same defect Deadline 5
    refused when it settled the idempotent-replay status. A client that
    branches on the status code must not be sent down the wrong branch.
    **B added the evidence A's section lacked:** auto-waitlisting would
    not land on untested ground, it would turn a currently-green
    assertion red — `full offering -> 409 CAPACITY_EXHAUSTED, not a
    silent waitlist`, and `the refused registration created NO waitlist
    row`.
    **Workflow D was corrected in the same deadline** — it used to read
    `enrolled_count < capacity ✓ else → waitlist`, and now records the
    explicit-join decision with the reasoning. No doc debt outstanding on
    this item.
    *(Original text below.)*

    **Is joining a waitlist automatic or explicit?**
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
    → **A's proposal is written**, at the end of the Deadline 6 section:
    joining is EXPLICIT, because auto-waitlisting makes one `201` mean
    two different things — the same defect the Deadline 5 replay-status
    decision refused. Item 7 follows: `WAITLISTED` is never written, and
    the enum value stays because removing it costs a migration and buys
    nothing.
    → **B has responded** (session 15): agreed on both, with the
    observation that `check_waitlist.py` Part 1 **already asserts the
    explicit behaviour today** — a full offering returns
    `409 CAPACITY_EXHAUSTED` and writes no waitlist row — so
    auto-waitlisting would turn a currently-green assertion red. B needs
    four interface answers item 10 does not contain before the endpoints
    can be written: `OFFERING_NOT_FULL`, `ALREADY_WAITLISTED`,
    `NOT_WAITLISTED`, and whether queueing counts against the
    course-load quota (B proposes it does not).
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
    → **B has sharpened it into a prediction** (session 15): nothing
    between steps (0) and (3) of `reserve_gpu` expires the cluster, so
    the identity-map explanation predicts twelve racers all writing `2`
    — `allocated = 2` against 12 committed reservations. A measured 8/8
    with the counter matching `SUM(active)`. Both cannot be right, so
    the next step is one instrumented re-run, not more argument.

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

---

# Deadline 6 — the quota rollout and the admin endpoints (A)

## One mechanism, three resources — and the third one measured

Deadline 4 proved the quota mechanism on GPUs. Deadline 6 applies it to
rooms and courses, and the interesting question was whether the argument
generalises or whether GPUs were a special case. It generalises, and the
room path now has its own number:

``` text
2 concurrent bookings, 2 DIFFERENT rooms, one student at 1 of 2, 20 trials

  resource lock only (no user lock)   19/20 over quota   held {2: 1, 3: 19}
  + user-row lock                      0/20 over quota   held {2: 20}
```

Compare Benchmark 2 at Deadline 4: **24/25 versus 0/25**. Same shape, same
cause, third invariant. The rooms case is arguably the cleaner
demonstration, because rooms have *no counter at all* — the overlap
invariant is enforced entirely by a GiST exclusion constraint, and that
constraint is perfectly correct throughout. It simply cannot see a fact
about the user. Two different rooms are two different resources; nothing
serializes them but the user row.

The unit differs and nothing else does. GPUs are held in units, so the
GPU quota SUMs `gpu_count`; a room hold and a course seat are
indivisible, so those COUNT rows.

## The course-load quota was an addition, not a reordering

`courses.service.register` has taken the user lock since Deadline 4, for
the schedule-overlap check — which is a fact about the student for
exactly the reason the quota is. So Deadline 6 added a gate *inside* a
lock that already existed. The Deadline 4 write-up predicted this in
those words, and it held.

**Gate order: `ALREADY_ENROLLED` → quota → `SCHEDULE_CONFLICT` →
`CAPACITY_EXHAUSTED`.** Each boundary is deliberate. Quota comes *after*
already-enrolled because a caller asking about a seat they hold should be
told that, not told they are at their limit — the count includes the seat
they are asking about. It comes *before* the other two because a student
at their limit cannot register for anything, so a clash or a full section
is a detail about a request that was never going to succeed. That also
matches the GPU path, where `check_gpus.py` already asserts quota fires
before capacity.

## `resources.status` is now written as well as read

Deadlines 3 and 4 built the gates that READ `status` under a row lock.
`PATCH /rooms/{id}` and `PATCH /gpus/{id}` are the only things that write
it. That order was the right one: a flag nothing enforces is worth less
than an enforced flag nothing can set, and the second is one endpoint
away from the first.

Neither PATCH needs a lock for the status write itself. `reserve_room`
holds `FOR SHARE` on that row across its gate, so a concurrent booking
either commits before the UPDATE can proceed or waits and then sees
BLOCKED — the serialization is already paid for by the reader.

## THE FINDING — §13's capacity-reduction rule contradicts the schema

`ARCHITECTURE_AND_WORKFLOWS.md` §13 says:

> *if an admin lowers a cluster from 8 to 4 while 6 are allocated,
> existing reservations are not retroactively evicted; the new limit
> applies only to future allocations and `allocated` drains naturally.*

**That state cannot be stored.** `gpu_capacity_sane` is
`allocated >= 0 AND allocated <= gpu_count`, so `gpu_count = 4` with
`allocated = 6` is rejected by Postgres. Implemented literally, §13 would
arrive at the caller as a 500 from a CHECK violation. The document
describes a database state the schema forbids, and it has said so since
Deadline 1 — nothing read it against the constraint until an endpoint
existed to try.

**Resolved by refusing the reduction, not by dropping the CHECK.**
`409 CAPACITY_BELOW_ALLOCATED`, decided against the row just locked
`FOR UPDATE` so an allocation cannot commit between the check and the
write.

The reasoning for keeping the constraint: it is what makes a locking bug
in the flagship transaction fail *loudly* instead of quietly overselling
a cluster. `check_gpus.py` leans on it — a clean capacity race is also
evidence that no request had to be saved by the CHECK. Trading that for
one sentence of documented convenience is a bad exchange.

And §13's *intent* survives intact: nothing is ever evicted. An admin
shrinks to `allocated` immediately and further as holds are released.
Only the literal example — 8 down to 4 with 6 held — is unavailable.
**§13 needs the correction**; it is made in that file at this deadline.

## The read endpoint must not fail closed the way the transaction does

`limit_for` raises `QuotaNotConfigured` on a missing policy row, which is
right inside an allocation: no policy means no permission, and one
un-seeded row must not silently switch the invariant off.

It is wrong in `GET /me/quota`. `(FACULTY, COURSE)` is deliberately
absent, so a faculty member asking "what are my limits?" would get a 409
for asking a reasonable question. `usage_snapshot` catches it and reports
`configured: false` per row instead.

Note the response carries two different meanings of `limit: null` —
unlimited, and unconfigured — which is the same conflation
`QuotaNotConfigured` exists to prevent inside the transaction. `unlimited`
is true only when a row exists *and* says NULL, so a client can tell them
apart without inferring.

## `GET /me/quota` takes no lock, and the test proves it

The plan says read-only, must not take the user lock. The reason is worth
keeping: a number shown to a caller is stale the moment it is rendered,
and that is fine — the allocation transaction re-reads everything under
the lock and is what has to be right. Locking the user row to render a
dashboard would put a GET in contention with the flagship write path.

Asserted rather than asserted-about: `check_quotas.py` holds the user row
`FOR UPDATE` in a separate session and calls the endpoint with a 5-second
timeout. If it took the lock it would block; it answers.

## Admin quota policy: PUT upserts, and absent ≠ NULL

`PUT /admin/quotas/{role}/{resource}` creates the row if it does not
exist. PUT is the right verb precisely *because* it creates: the URL
identifies the (role, resource) pair rather than a row id, so the same
call is correct either way and repeating it changes nothing.

It also gives an admin the only in-band way to fix a missing policy.
Before this endpoint, `(FACULTY, COURSE)` 409ing every faculty
registration with `QUOTA_NOT_CONFIGURED` meant reaching for psql.

`GET` on a missing pair returns **404, not a row with a null limit** —
absent and NULL are different, and an endpoint that flattened them would
teach an admin the wrong model of their own policy table. Three states,
three spellings, and `max_units = 0` is a fourth meaning again ("none at
all"), which is why the write schema is `ge=0` rather than `ge=1`.

Nothing here takes a lock, for the reason `limit_for` already records:
these are policy rows, not per-user state. Lowering a limit below what
users hold does not evict, and the *implementation* of that rule is that
nothing here looks at anyone's holdings.

## The regression this deadline nearly hid in B's gate

`check_rooms.py` failed eleven assertions the moment the room quota
landed — the seed student books about ten slots to test intervals, and
the cap is 2. Expected, and the plan's *"fix whatever B's benchmarks
break"* is exactly this.

**The second effect was not expected and matters more.** That script's
strongest assertion fires eight simultaneous identical bookings at one
room and requires exactly one row to land. All eight used the *same*
student token. Once `reserve_room` took the user-row lock, those eight
serialized on that user's row and reached the exclusion constraint one at
a time — the assertion would still have passed, while no longer measuring
the constraint it is named for.

This project has caught the identical bug once before, in its own words
at Deadline 4: *"a capacity race in which every racer shares one account
is not a capacity test."* It arrived here from the opposite direction —
not a test written wrong, but a correct test invalidated by a change
elsewhere. Fixed by giving the racers eight distinct accounts.

The quota itself is lifted for the duration of `check_rooms.py` and
restored in cleanup, rather than spreading its bookings across users:
several of its assertions are specifically about ONE caller booking
adjacent and overlapping slots, and rewriting those would quietly change
what they prove. Quota is tested in `check_quotas.py`, which is where it
belongs.

**A smaller lesson, learned by doing it wrong:** the restore first
captured the current limit and put it back. A run that crashed midway
left the quota unlimited, so the next run recorded `None` as "the seeded
value" and faithfully restored the corruption. It restores a named
constant now.

## `cancel_reservation` gained a lock it did not need before

The Deadline 3 docstring predicted this too. Cancelling a room hold still
touches no shared counter — but `held_room_reservations` COUNTs ACTIVE
rows for the owner, so flipping this row's status *is* a write to a
quantity the quota reads. Without the user lock, a cancel and a
concurrent booking by the same user interleave and the caller is refused
at quota by a hold that no longer exists.

The lock is on the reservation's **owner**, not the caller — an ADMIN
releasing someone else's hold must serialize against that user's
bookings. Same rule, same reason, as the GPU cancel path.

## Smaller decisions

**The admin router lives in `quotas/`, not a new `admin/` package.** The
module that owns an invariant should own the endpoint that configures it;
`role_quotas` is read by `limit_for` inside three transactions, and
splitting the policy writer elsewhere would put the read and the write of
one table in two packages. A owns both halves.

**`GET /me/quota` did not bring a `users/service.py`**, contradicting
that file's own Deadline 3 prediction. There is real domain logic, but it
belongs to `quotas/`, which already owns every other reader of that
policy. The router stays an HTTP wrapper — which is how the plan
described the endpoint in the first place.

**`PATCH /rooms/{id}` takes `status` only.** The plan names it *"block a
room for maintenance."* `capacity` and `building` are editable in
principle, in no requirement, and two deadlines from a freeze.

**`allocated` is not a field on `GPUClusterUpdate`**, for the same reason
it is absent from `GPUClusterCreate`: it is derived state owned by the
allocation transaction. An admin who could set it directly could make it
disagree with `SUM(active reservations)`, the invariant `check_gpus.py`
asserts after every phase. Asserted: PATCHing it changes nothing.

## Verification

`scripts/check_quotas.py` — **61/61 PASS**, the seventh gate. No
migration: `RoleQuota` shipped at Deadline 1 and `alembic check` reports
no drift.

``` text
room 3 of 2                      -> 409 QUOTA_EXCEEDED, nothing created
faculty, same slot               -> INTERVAL_CONFLICT, not QUOTA
cancel then rebook               -> 201            <- cancel frees quota
admin (NULL policy)              -> 3 bookings, unlimited
me/quota, three resources        -> limits 2/2/6, held reflects reality
me/quota while user row LOCKED   -> answers        <- takes no lock
(FACULTY, COURSE) via me/quota   -> configured:false, not unlimited
student GET/PUT /admin/quotas    -> 403 x2, policy unchanged
GET unseeded pair                -> 404, not a null limit
PUT creates / null / 0 / -1      -> 200, 200, 200, 422
lower quota under a holding      -> holds survive, new booking refused
course 2 of 1                    -> QUOTA_EXCEEDED, not SCHEDULE_CONFLICT
re-register same offering        -> ALREADY_ENROLLED, not QUOTA
drop then register the other     -> 201            <- DROPPED stops counting
block a room                     -> RESOURCE_BLOCKED, hold NOT evicted
shrink GPU below allocated       -> 409 CAPACITY_BELOW_ALLOCATED, no change
shrink TO allocated              -> 200            <- boundary allowed
PATCH allocated directly         -> ignored, still 2
ROOM QUOTA RACE, 20 trials       -> 0/20 over quota   (19/20 without the lock)
```

Full regression, run **twice** to prove each gate leaves the state the
next one assumes: `check_jwt`, `check_auth`, `check_rbac`, `check_rooms`,
`check_gpus`, `check_courses`, `check_idempotency`, `check_quotas` — all
pass, both times. B's three benchmarks re-run after the change:
Benchmark 1 `PASS — exactly 10 in every trial`, Benchmark 2 `PASS`,
Benchmark 3 `PASS`.

---

## Deadline 6 joint work, part 1: A's review of `session.py` — APPROVED

B changed a shared file solo across sessions 12 and 13, flagged it for A's
review three times, and attached a measured failure as the justification.
This is that review. **Verdict: approved, with two findings, neither
blocking.**

### What was checked against the running system, not the comment

Every number in B's justification block was re-derived rather than read:

``` text
claim in the comment              verified                     result
--------------------------------------------------------------------------
anyio thread ceiling = 40         current_default_thread_       40      OK
                                  limiter().total_tokens
max_connections = 100             SHOW max_connections          100     OK
superuser_reserved = 3            SHOW superuser_reserved_      3       OK
                                  connections
pool_size + overflow = 50         engine.pool.size() = 40,      50      OK
                                  _max_overflow = 10
50 > 40 (pool exceeds threads)    arithmetic                    holds   OK
```

**The core argument is correct and is the right diagnosis.** FastAPI runs
`def` endpoints in anyio's worker thread pool, so at most 40 requests are
ever inside a handler; a pool smaller than that ceiling guarantees
starvation regardless of load shape. Sizing the pool *above* the ceiling
is what makes checkout non-blocking, and dropping `pool_timeout` from 30s
to 10s follows from it: with 50 > 40 a timeout can no longer be
contention, so it should surface fast as the misconfiguration it now is.

**Approved because the finding was measured, not reasoned.** 500
concurrent registrations on the defaults returned `errors=500/500` with a
126-second median — the run measured the pool and never reached a lock.
A shared-file change made solo needs a stronger justification than
convenience, and "the benchmark produces no number at all" is one.

### Finding 1 — the connection budget in the comment is wrong (minor)

The budget table says `benchmark script's own pool  15`. It is not 15.
`tests/concurrency/harness.py::tool_session_factory` creates `pool_size=5,
max_overflow=2` — **7**. The number 15 is the old SQLAlchemy default,
which after this very change applies nowhere in the project.

Worst case is therefore `50 + 7 + 1 psql ≈ 58`, not the stated 65. The
conclusion is unaffected — 58 is further under 97 than 65 was — so this
is a documentation defect rather than a sizing defect. It matters only
because that table *is* the argument for a solo change to a shared file,
and a wrong number in the argument invites a reader to distrust the rest
of it.

### Finding 2 — the gates use the server's pool, which B's own rule forbids

`tool_session_factory` exists precisely because a benchmark process
importing the application's `SessionLocal` silently reserves a second
50-connection pool, and B measured the consequence: 387 of 500 requests
returned `500` while the harness competed for the same 100 connections.
The docstring is emphatic that benchmarks must not do this.

**Eight scripts do exactly that.** All seven `scripts/check_*.py` gates
and `scripts/seed.py` import `app.database.session.SessionLocal`, so each
run holds an engine sized for the server.

They get away with it today because their database use is essentially
sequential — measured at one to three connections in practice — while
their concurrency is HTTP-side through `httpx`. So this is latent, not
live, and it is not a reason to withhold approval.

It is worth fixing before Deadline 9's clean-room run for one reason:
**the failure would be silent and misattributed.** A check script that
ever grows a threaded database section reserves up to 50 connections, and
the symptom appears inside the *server* as a `QueuePool` timeout — which
reads exactly like the bug this change fixed, in a file nobody would
think to look at.

**Proposed fix, not applied here:** the gates take the same small pool
the benchmarks take. That touches two of B's scripts (`check_rooms`,
`check_courses`) and `seed.py`, so it is a joint change rather than
something to land inside a review. Recorded for the same session that
ratifies items 9, 10 and 7.

### What this review does NOT cover

`get_db()` and `SessionLocal` are unchanged by B and were not re-reviewed;
`expire_on_commit=False` and the deliberate absence of a commit in
`get_db` remain as settled at Deadline 1, and the GPU transaction's
correctness depends on both.

---

## Deadline 6 joint work, part 2: swap review — HELD; this was the preparation

> **The review took place** and closed Deadline 6. What follows is what
> was written *before* it — the walkthrough A owed B, and what A should
> be able to answer when B reciprocated. It is kept unedited rather than
> rewritten in hindsight, because the value of a prepared walkthrough is
> that it shows what A understood going in.
>
> **What is not here: B's answers.** The four questions at the end of
> part 3 were the point of holding the session, and their answers were
> not recorded. If they were given, they belong in this file — an answer
> nobody wrote down is an answer nobody has in two months, which is the
> entire reason this file exists.

### A walks B through the GPU transaction — the spine of the walkthrough

`gpus/service.py::reserve_gpu`. Three guarantees meet in one function and
each has a *different* serialization point. That is the whole talk:

``` text
exactly-once  a fact about the REQUEST   -> UNIQUE(key,user_id) index
quota         a fact about the USER      -> user row  FOR UPDATE
capacity      a fact about the RESOURCE  -> cluster row FOR UPDATE
```

Six points to make in order, each with the reason it is not the obvious
alternative:

1. **Step (0) reads existence without a lock, and that is safe** because
   `resource_type` is immutable while `status` is not. The mutable one is
   read under the lock at step (3); the immutable one is not. Without
   step (0) an over-quota caller naming a nonexistent cluster gets
   `QUOTA_EXCEEDED` instead of a 404 — found by running it.
2. **Step (1) is above both locks** so a replayed request never queues
   for them. The serialization point is an index, not a lock, because
   *there is nothing to lock until somebody creates the row, and creating
   it is exactly the window a retry arrives in.*
3. **The SAVEPOINT.** A unique violation aborts the whole transaction, so
   the read that fetches the stored response cannot run without it.
   Measured: 86 of 120 concurrent retries become 500s.
4. **Step (2) locks the USER, not the resource** — and this is the
   sentence B must be able to say unprompted: *the lock was never
   missing, it was the wrong lock for that invariant.* Two 2-unit
   requests at two different clusters contend on no common row; both
   cluster locks work perfectly and the student ends up holding 4.
5. **Step (3) is `FOR UPDATE` here and `FOR SHARE` in rooms**, because
   this path writes the row it locks and the room path does not. B should
   be asked to explain that difference in the other direction.
6. **`populate_existing()` is load-bearing**, and item 11 is still open:
   the probe says the locked read is stale on this path too, yet the race
   will not reproduce it. Present it as unresolved, because it is.

### What A must be able to answer when B reciprocates

B's half is the exclusion constraint and the harness. A should come out
of it able to say, without B present:

- why `EXCLUDE USING gist` with `'[)'` makes adjacent bookings succeed,
  and why the constraint is partial on `status = 'ACTIVE'`;
- why the room path takes no "is this slot free?" SELECT — the check
  passes exactly when it does not matter;
- why the harness reports **achieved** concurrency rather than requested,
  and what the 40-thread ceiling means for the phrase "500 concurrent"
  that appears in three documents;
- why Benchmark 1's broken build reads a *fresh* value and still
  oversells — it is not a stale read, so `populate_existing` does not fix
  it and only the lock does.

### Deadline 10's four questions, to be rehearsed here rather than there

Each writes their own answer, separately, then compares:

``` text
Why is the quota lock on the user, not the resource?
Why must the idempotency key commit in the same transaction?
Why does fixed lock ordering prevent deadlock -- and where does
  waitlist promotion sit relative to that claim? (see items 9/10/7)
What did you deliberately NOT build, and why?
```

The third has changed since it was written: promotion is now *proposed*
to sit outside the ordering argument entirely, by never waiting. Both
people should be able to explain why that is stronger than obeying the
order, not weaker.

---

## Deadline 6 joint work, part 3: A's study of B's modules

> Written by A, from B's code, **before** the swap review rather than
> after it. The point of the swap is that each person can explain the
> other's module unprompted; doing the reading first means the live
> session is B *correcting* A rather than B *teaching* A, which is a
> better use of an hour and a harder test of whether A actually
> understood it. **Errors below are A's and are exactly what B should be
> looking for.** Unresolved questions are listed at the end rather than
> guessed at.

### The exclusion constraint — `e0fbfe421403`

``` sql
EXCLUDE USING gist (
    resource_id                              WITH =,
    tstzrange(start_time, end_time, '[)')    WITH &&
) WHERE (status = 'ACTIVE');
```

Four things carry the whole room guarantee, and A should be able to name
each without the file open:

1. **`btree_gist` is not optional.** GiST handles ranges natively but not
   scalar equality; the extension is what allows `resource_id WITH =` and
   the range overlap to live in **one** index. Two separate indexes could
   not express "same room AND overlapping time" as a single constraint.
2. **`'[)'` — half-open.** `[10,12)` and `[12,14)` do not overlap, so
   back-to-back bookings both succeed. With `'[]'` a booking ending at
   12:00 would collide with one starting at 12:00. This is the same
   half-open convention `courses.service._times_overlap` uses with `<`
   rather than `<=`, and the two must agree or the system contradicts
   itself across modules.
3. **Partial on `status = 'ACTIVE'`.** A cancelled reservation must stop
   blocking its old slot. This is also why cancelling a room hold needs
   no lock — releasing a slot is just making the constraint stop
   objecting to it.
4. **It is an index, so it serves reads too.** This is why outstanding
   item 3 closed with *no* btree on `(resource_id, status)`: the GiST
   index already has that exact shape, and a second index would have been
   a duplicate nobody measured.

**Why there is no "is this slot free?" SELECT before the INSERT.** The
check passes exactly when it does not matter: two concurrent bookings can
both run it, both see free, and both proceed. The INSERT *is* the check.
This is the same argument as duplicate registration at Deadline 2, where
a "does this email exist?" query was rejected for the same reason and
`UNIQUE(email)` was made to do the work.

**`FOR SHARE`, not `FOR UPDATE`, and A must be able to argue this in
B's direction.** The room path never writes the row it locks — rooms have
no counter, unlike `GPUCluster.allocated`. What the `BLOCKED` gate needs
is for `status` not to *change* under it, so an admin `PATCH` cannot land
between the check and the INSERT. `FOR SHARE` says precisely that: blocks
writers, does not block other bookers. Taking `FOR UPDATE` would also be
correct, and would quietly serialise every booking of one room behind
every other — making the *application lock* rather than the constraint
the thing deciding a slot race. The invariant would survive; the design
claim would not.

**So the room path and the GPU path are the project's cleanest contrast,
and it is a contrast about mechanism rather than about care:** rooms have
a constraint that can express their invariant, so the database enforces
it and the lock is incidental. Quota has no such constraint — no
`EXCLUDE` can say "this user holds at most 2" — so a row lock is the only
mechanism available. Same rigour, different tool, and the reason is a
property of the invariant, not of who wrote it.

### The harness — and the finding that lands on A's file

B's `tests/concurrency/harness.py` documents three throttles between
`asyncio.gather` and a row lock: httpx `max_connections` (100 by
default), the server's anyio thread pool (40), and the SQLAlchemy pool
(15 by default). The harness owns only the first, fixes it, and
**measures** the other two — `DBConcurrencyObserver` samples
`pg_stat_activity` so a run reports the concurrency it achieved next to
the number it requested.

**The part A needs to own, because it is a consequence of A's design:**

> B's first reading was that the ceiling is the 40 worker threads. It is
> not. A connection is checked out during **dependency resolution** —
> `get_current_user` reads the user row — and the Session holds it, with
> an open transaction, until `get_db` closes it at the end of the
> request. The connection is therefore held across event-loop hops, so
> the ceiling is **requests in flight**, not threads.

That is `core/dependencies.py` and `database/session.py` — both A's, both
settled at Deadlines 1 and 3, and neither written with this consequence
in mind. Measured by B: 500 unbounded requests gave
`{201: 60, 500: 440}` with 50 connections sitting `idle in transaction`
waiting on Client — the entire pool held and doing nothing.

**Session-per-request, held across the whole request, is what caps this
system's concurrency.** Not the pool size, which is why raising the pool
did not fix it, and not the harness. A should say this in the swap review
before B has to point at it, because "500 concurrent" appears in three of
our documents and the honest number is the in-flight bound.

Two smaller pieces of the harness worth being able to explain:

- **`asyncio.Barrier` before every request.** Without it the first
  request can complete before the last coroutine is constructed, and a
  "concurrent" benchmark quietly becomes a sequential one — the async
  cousin of the Deadline 4 bug where twelve racers shared one account and
  the contended lock was never contended.
- **`max_keepalive_connections=0`.** A reused connection is a request
  that waited for an earlier one to finish, which is the opposite of what
  is being measured.

### Questions A could not answer from the code — for B

1. **Is `max_in_flight = 40` derived or coincidental?** The docstring
   argues the ceiling is requests-in-flight rather than the 40 anyio
   threads, and then the number chosen is also 40. If those are
   independent facts that happen to agree, the comment should say so;
   if 40 was picked *because* of the thread pool, the argument above it
   is the one that is wrong.
2. **What exactly is the observer counting?** It excludes its own
   `pg_backend_pid()` but counts every `active` backend on the database,
   which would include the benchmark's own bookkeeping connections and
   any stray `psql`. Is the reported peak meant to be "our requests" or
   "everything"?
3. **`Result.body` was added last with a default** so existing
   constructions stayed valid — has anything been checked for
   *positional* construction of `Result`, where a new trailing field is
   safe but a reordering would not be?
4. **A related trap A should flag, not ask about.**
   `get_current_user` puts the `User` in the identity map during
   dependency resolution — *before* any lock. `reserve_gpu` then locks
   with `select(User.id)`, a Core select that does not refresh that
   object, and `enforce_gpu_quota` reads `user.role` from the pre-lock
   copy. Harmless today, because role is not the invariant being
   protected and a stale role is already ruled out by `require_role`
   reading the database row. But it is the same shape as item 11, and it
   is worth both of us agreeing out loud that it is harmless rather than
   discovering later that it was not.

---

## Deadline 7 groundwork: the gate written before the transaction

`scripts/check_waitlist.py`, the seventh gate. **Part 1 runs and passes
today; Part 2 holds the promotion assertions and skips while the waitlist
routes do not exist.** It detects them from the live `openapi.json`
rather than from a hardcoded path, because item 10 is unratified and the
route may land as `POST /offerings/{id}/waitlist` or as a fall-through on
register.

**Why write it before the code.** This project has been bitten twice by
tests written afterwards: Benchmark 2 passed against the build it existed
to indict, and Deadline 3's room checks were green while
`dependencies.py` was still a stub accepting any token and no token
alike. A test written after the code tends to test the code, not the
claim. The Deadline 7 checkpoint is already specific, and **none of its
assertions depends on how items 9, 10 or 7 are ratified** — item 9
decides how the transaction takes its locks, not what must be true when
it commits.

### Part 1 is not filler — it proves the ground promotion stands on

The FIFO guarantee is `ORDER BY created_at, id`, and the `id` tiebreak is
load-bearing because **`func.now()` is transaction start time**, so every
row written in one transaction shares a `created_at` exactly. That has
been asserted in prose since Deadline 2 and was never tested. It is now:

``` text
3 waitlist rows, ONE transaction  -> 3 rows, 1 distinct created_at
ORDER BY created_at alone         -> cannot express FIFO. Proven, not quoted.
ORDER BY created_at, id           -> insertion order, ids strictly increasing
ROW_NUMBER() OVER (...)           -> positions 1,2,3 with nothing stored
```

If that ever stops being true the promotion transaction's ordering
becomes undefined, and **nothing in the promotion tests themselves would
catch it** — the entries would simply come back in some order and the
assertions would pass.

Part 1 also pins three things promotion will collide with:

- **no `position` column** — dropped in `c86676652ca2`; its reappearance
  would be a regression, since renumbering after a promotion transiently
  violates a unique constraint mid-UPDATE;
- **a DROPPED student still owns an enrollment row**, because
  `enrollment_unique` is unconditional — so promotion must **UPDATE, not
  INSERT**, the same trap registration hit at Deadline 4 and the one
  `courses/service.py` predicted promotion would hit next;
- **a full offering returns `409 CAPACITY_EXHAUSTED` and writes no
  waitlist row** — item 10 *observed* rather than decided, so whichever
  way it is ratified the change to what `POST /register` means shows up
  here instead of silently.

### The eight pending assertions

``` text
promotion follows FIFO by (created_at, id)
promotion respects the promoted student's course-load quota
a quota-breaching candidate is SKIPPED, next eligible promoted
2 concurrent drops -> exactly 2 DISTINCT promotions
2 concurrent drops -> no entry promoted twice
promotion DELETES the waitlist row, renumbering nothing
promoted student's enrollment is an UPDATE of the DROPPED row
GET waitlist reports position from ROW_NUMBER(), never stored
```

The gate exits 0 while they are pending and **turns red the moment
Deadline 7 is half-built** — if a waitlist route appears and Part 2 has
not been written against it, that is a failure, not a skip. A pending
test that quietly passes forever is the thing this design exists to
avoid.

---

## Extra item, carried out of Deadline 6: A's proposal on items 9, 10 and 7

> **PROPOSED, NOT RATIFIED.** All three are joint calls and none of them
> is settled by this section being written. It exists because item 9 is
> *A's transaction to design*, and arriving at the joint session with a
> recommendation is A's job rather than arriving with the question. The
> shape follows outstanding item 6, which sat here as "proposed, still
> needs ratifying" until Deadline 3 signed it off.
>
> **Nothing below is implemented.** No waitlist code exists.

### The deadlock is real, and here it is concretely

`courses.service.drop` holds **user(dropper) → offering**, verified in the
code rather than assumed. Promotion hangs off that transaction, so by the
time it runs, both of those locks are held. To check the promoted
student's course-load quota it must lock a *second* user row — and it
cannot know which one until it has read the waitlist, which needs the
offering lock it is already holding.

That ordering is offering → user, and registration is user → offering.
The cycle:

``` text
T1  student X drops offering O
      holds  user(X)        -> offering(O)
      wants  user(Y)                          to promote Y

T2  student Y registers for offering O
      holds  user(Y)
      wants  offering(O)

T1 waits on Y's row. T2 waits on O's row. Neither releases. DEADLOCK.
```

This is not hypothetical and it is not rare — Y being on the waitlist for
O makes Y *more* likely than average to be touching O.

### Why the obvious fixes do not work

**"Lock the user first, like everywhere else."** Impossible in principle.
Which user? Y's identity is the *output* of reading the waitlist, and
reading the waitlist consistently requires the offering lock. There is no
ordering of "lock Y" before "discover Y."

**"Promote in a separate transaction after the drop commits."** Removes
the cycle by removing the overlap, and it is the honest runner-up. Cost:
a window where a seat is free and a waitlist is non-empty, in which an
ordinary registration can take the seat ahead of everyone queued. That is
defensible as first-come but it defeats what a waitlist is for, and a
crash inside the window leaves the seat unclaimed with no sweeper to
notice. Rejected, recorded rather than discarded.

**"Give courses the opposite global order — offering → user everywhere."**
Internally consistent, and safe only because no transaction spans the
course and GPU subsystems. It would mean two lock orders in one codebase
distinguished by which module you are in, contradicting §14's "every
path" claim outright, and it would require reopening `register` two
deadlines before the freeze. Rejected.

### The proposal: promotion never waits for a user row

**A cycle requires a circular *wait*. Remove the waiting and the ordering
stops mattering.**

Promotion takes the offering lock (already held), reads waitlist
candidates oldest-first, and attempts each candidate's user row with
`FOR UPDATE ... SKIP LOCKED`. If that row is not immediately available,
the candidate is skipped and the next one is tried.

``` text
LOCK user(dropper)                        <- already held by drop
LOCK offering                             <- already held by drop
  decrement enrolled_count for the drop

  for each waitlist entry, ORDER BY created_at, id:
      try LOCK user(candidate) SKIP LOCKED
      if not acquired          -> skip, try the next entry
      if course-load quota full -> skip, try the next entry
      otherwise                -> promote exactly one, stop
COMMIT
```

T1 in the example above no longer waits on user(Y); it skips Y and
promotes the next eligible entry. T2 proceeds. **No wait, no cycle, and
the global order needs no exception written into it** — promotion simply
stops being a path that can participate in a deadlock, which is a
stronger statement than promotion obeying the order.

**Why skipping is not a new concession.** The Deadline 7 spec already
says *"check that student's course-load quota; if it would breach, skip
to the next eligible entry."* FIFO is therefore already defined over
*eligible* entries, not all entries. This proposal adds one clause to
eligibility — "and their row is not currently locked" — rather than
inventing a weaker guarantee.

**What it costs.** Strict FIFO is not preserved when a queued student is
concurrently doing something else. This should be stated in the README
rather than glossed: the promise is *oldest eligible*, not *oldest*.
Note it does not affect Benchmark 4 as specified — 2 concurrent drops
against 3 waitlisted students who are otherwise idle, so no candidate row
is locked and order is preserved exactly.

**The quota check stays under a real lock**, which was the other half of
item 9 and the half that matters most: an unprotected quota check is the
precise failure Benchmark 2 exists to demonstrate, and shipping one in
the promotion path would contradict the project's central claim.

### Item 10: joining is EXPLICIT — `POST /offerings/{id}/waitlist`

Recommended over auto-waitlisting on a full register, mainly for one
reason that this project has already paid to learn once:

**auto-waitlisting makes one status code mean two things.** A `201` from
`POST /register` would sometimes mean "you have a seat" and sometimes
"you are queued", and the caller would have to read the body to find out
which. That is the same defect as returning `200` for an idempotent
replay — settled at Deadline 5, where the rule was that a client
branching on the status code must not be sent down the wrong branch.
`409 CAPACITY_EXHAUSTED` keeps meaning exactly one thing.

Secondary: it is less surface two deadlines before a freeze, and it is
the reading `INIT_PLAN.md` §12 already specifies.

Against: an extra round trip, and Workflow D in
`ARCHITECTURE_AND_WORKFLOWS.md` reads `else → waitlist`. **Workflow D
needs correcting if this is ratified** — the same kind of doc correction
§13 needed at this deadline.

### Item 7: `EnrollmentStatus.WAITLISTED` is never written

Follows directly from item 10, as that item predicted. With explicit
joining, a queued student has a row in `waitlist_entries` and nothing
else. Writing an enrollment row with `status = WAITLISTED` would put the
same fact in two tables — the failure mode this project keeps finding —
and `held_course_enrollments` counts `ACTIVE` only, so such a row would
be invisible to the course-load quota that is supposed to govern it.

**Recommendation: never write it, and leave the enum value in place.**
Removing a value from a Postgres enum means recreating the type and
rewriting the column, which is a migration and a shared-file change
buying zero behavioural difference. A comment in `models/enums.py` saying
"never written; waitlist membership lives in `waitlist_entries`" is
cheaper and equally clear. If Deadline 8's integration pass wants the
enum clean, that is the moment to spend the migration — with both people
present, since it is `models/`.

### What ratifying this unblocks

Items 9, 10 and 7 are the three things standing between here and
Deadline 7. Ratified, A writes the promotion transaction and B writes the
waitlist endpoints against a settled entry point. Item 11 remains open
and is worth resolving in the same session, because promotion is the next
locked read anyone writes and it is the read most likely to be bitten.

---

# Deadline 7 groundwork — B's side of the ratification (B)

> Written before the joint session, for the same reason A's proposal was
> written before it: arriving with a position rather than with a
> question. **This is one half.** Items 9, 10 and 7 are ratified when
> both people have said so, and nothing below is ratified by having been
> written down.

## The four questions A left out of the swap review

A's session-14 entry records four questions asked out of the harness
reading, and notes that the answers were never written anywhere. Three
are answerable from the code, and are answered here rather than
remembered.

**1. Is `max_in_flight = 40` derived or coincidental?** Derived — from
the *connection* budget, not from the thread pool. The comment at
`tests/concurrency/benchmark_1_capacity.py` says the pool is 40 + 10
overflow = 50 and that 40 leaves a margin under it. The agreement with
uvicorn's 40 anyio worker threads is a second fact that happens to hold,
not the reason for the number.

A's worry was that if 40 came *from* the thread pool, then the docstring
above it — arguing the ceiling is requests-in-flight rather than threads
— would be the thing that is wrong. It does not, so that argument
stands. But A was right that the comment reads ambiguously with both
numbers at 40, and the two facts are now named separately in it.

**2. What is `DBConcurrencyObserver` counting?** Every backend on this
database in state `active`, minus its own — so it includes the
benchmark's own bookkeeping connections and any stray `psql`, not only
the requests under test. It is an **upper bound on our concurrency, not
a measurement of it**, and the docstring now says so.

That is still the right instrument for the claim it supports. The
question the peak answers is *"did this run measure a lock or a pool?"*,
and a run that asks for 40 and peaks at 3 has answered it whichever
process the 3 belonged to. It would be the wrong instrument for a
throughput number, and no benchmark uses it for one.

**3. Has anything been checked for positional construction of `Result`?**
Checked, not assumed: `Result(` appears at exactly two sites, both inside
`harness.py::_one`, and both pass every field by keyword. Nothing else in
`tests/` or `scripts/` constructs one. Adding `body` as a trailing field
with a default was safe, and a **reordering** of the existing fields
would still be safe today — but only by luck, and that is not worth
relying on twice.

**4. The pre-lock `User` in the identity map.** Agreed, and agreed out
loud as A asked: harmless *here*, for a reason narrower than "role does
not change".

`get_current_user` loads the `User` during dependency resolution, before
any lock exists. `reserve_gpu` then locks with `select(User.id)`, a Core
select that refreshes no ORM object, so `enforce_gpu_quota` reads
`user.role` from the pre-lock copy. It is harmless because **`role` is
not the invariant that lock is protecting.** The invariant is held units,
and those are read fresh under the lock by `held_gpu_units`' `SUM`. The
stale field only selects *which policy row* to compare against, and
entitlement has already been decided by `require_role` reading the
database row.

The line that makes this a rule rather than a coincidence: **it stops
being harmless the moment a quota decision reads any mutable field off
that pre-lock object.** If a per-user override ever lands on `users`, it
must be read after the lock, not off `user`.

---

## Item 9 — agreed, with one condition

`SKIP LOCKED` is the right shape. The alternatives A rejected are
rejected for the right reasons, and the argument that carries it is that
**removing the wait is stronger than ordering the wait**: a path that
never blocks on a user row cannot appear in a cycle at all, so the global
order needs no exception written into it and §14's "every path" claim
survives intact.

B agrees on one condition, and it is not a detail.

### The condition: the mechanism must be measured, and Benchmark 4 as specified cannot see it

A's proposal notes, as reassurance, that `SKIP LOCKED` *"does not affect
Benchmark 4 as specified — 2 concurrent drops against 3 waitlisted
students who are otherwise idle, so no candidate row is locked and order
is preserved exactly."*

That is exactly the problem. If no candidate row is ever locked, **the
skip clause never executes**, and Deadline 7 would ship its most subtle
mechanism with no measurement of it at all. This project has been burned
by that shape three times and has written each one down:

``` text
Benchmark 2       passed against the build it existed to indict
Deadline 3 rooms  green while dependencies.py was still a stub
Session 14        8-racer room test stopped contending the exclusion
                  constraint and WOULD STILL HAVE PASSED
```

The third is A's own finding, and it is the same failure from the same
direction: a correct assertion that quietly stops exercising the thing it
is named for.

**So Benchmark 4 gets a third column, and the gate gets a ninth
assertion.** Hold candidate 1's user row `FOR UPDATE` from a second
session, then drop a seat, and assert:

``` text
the held candidate is PASSED OVER, not waited for
the next eligible entry is promoted instead
FIFO holds among the candidates that were not locked
promotion COMPLETES while the row is still held   <- the actual claim
```

The fourth line is the one that matters and the one nothing else tests.
*"Never waits"* is a claim about time, and it is only true if promotion
returns while the lock it declined to wait for is still held by someone
else. Hold the row for 5 seconds; promotion must finish in well under
that. If it blocks, `SKIP LOCKED` is not doing what the design says —
and every other assertion in Benchmark 4 would still pass.

**This one is deterministic, which is worth noting given everything else
here is measured over trials.** Benchmarks 1-3 count over trials because
they race a sub-millisecond window and a single run is a coin flip.
Holding a lock deliberately is not a race — the row is held or it is not
— so this assertion needs one run, not twenty-five. A concurrency test
that can be made deterministic should be.

### Two smaller things B wants recorded with the ratification

**The skip must leave a trace.** A queued student passed over for a seat
they were first in line for is invisible: no row changes, nothing is
written, and the next `GET /waitlist` shows them still at position 1 with
no indication of what happened. At minimum it is logged. And the README
says **oldest *eligible*, not oldest** — A already proposes this and B is
holding them to it, because it is the one place where this system's
behaviour is weaker than the obvious reading of the word "waitlist".

**The scan is unbounded, and it runs holding two locks.** Promotion
iterates candidates until one is promoted, inside the drop transaction,
which already holds user(dropper) and the offering row. A waitlist whose
first K candidates are all at their course-load cap means K lock attempts
and K quota `SUM`s with the offering row held — and every registration
for that offering queues behind it for the duration. Bounded in practice
by waitlist length, which nothing bounds. Either cap the scan at a stated
K and promote nobody beyond it, or accept it and say so in the README.
B's preference is to accept and document at this scale, and to say why:
capping introduces a seat that stays empty while eligible students are
queued, which is a worse failure than a slow drop.

---

## Item 10 — agreed, and three interface questions it does not settle

Explicit joining, `POST /offerings/{id}/waitlist`. A's argument is the
one that decides it: auto-waitlisting makes a `201` from
`POST /register` mean either "you have a seat" or "you are queued", and
Deadline 5 already refused exactly that shape when it settled the replay
status. `409 CAPACITY_EXHAUSTED` keeps meaning one thing.

B adds one piece of evidence A's section does not have: **the assertion
already exists and already passes.** `check_waitlist.py` Part 1 asserts
today that a full offering returns `409 CAPACITY_EXHAUSTED` and writes
**no** waitlist row. Auto-waitlisting would not be a new feature landing
on untested ground; it would be a change that turns a currently-green
assertion red. That is the cheapest possible confirmation that the two
readings really are incompatible, and it lands on the explicit side.

**Workflow D in `ARCHITECTURE_AND_WORKFLOWS.md` needs correcting** — it
still reads `else → waitlist`. Same class of doc correction §13 needed at
Deadline 6, and it falls in B's half of the README at Deadline 9.

The endpoints are B's, so B needs three answers the item does not
contain. Proposed here so they are settled in the same conversation
rather than invented mid-implementation:

``` text
join a NOT-full offering     -> refuse. 409 OFFERING_NOT_FULL.
                                A queue for an available seat is not a
                                queue; the student should register.
join twice                   -> 409 ALREADY_WAITLISTED, from the
                                UNIQUE(student, offering) already on the
                                table -- caught, not pre-checked, the
                                same rule duplicate registration follows
join while ACTIVELY enrolled -> 409 ALREADY_ENROLLED  (code exists)
leave when not queued        -> 409 NOT_WAITLISTED, mirroring NOT_ENROLLED
```

**And the one that is really a quota question: queueing does not count
against the course-load quota.** `held_course_enrollments` counts
`ACTIVE` enrollments, and a queued student has no enrollment row at all —
so a student at their cap of 6 may still join waitlists, and the quota is
enforced at *promotion* time, where A's proposal already checks it and
skips. That is the correct place for it: a waitlist entry costs nothing
and holds nothing, so charging quota for one would refuse a student
something they are not yet receiving.

---

## Item 7 — agreed, no changes

`EnrollmentStatus.WAITLISTED` is never written; membership lives in
`waitlist_entries` and nowhere else. The enum value stays, with a comment
saying it is never written, because removing it costs a migration on
`models/` — a shared file needing both people — and buys no behavioural
difference.

Two tables holding one fact is the failure this project has now found
four times: the `position` column, `enrolled_count` versus the
enrollments rows, the idempotency key versus the allocation, and this.
Worth saying once in the README rather than four times.

---

## Item 11 — not B's, but the two observations cannot both mean what they appear to

Not B's item, and not resolved here. But B read the GPU path closely
enough during Deadline 6 to sharpen it into something falsifiable, which
is more useful than carrying it forward as "unexplained" for a third
deadline.

**The prediction, if the identity map is the whole story.**
`get_cluster` at step (0) puts the `GPUCluster` in the Session's identity
map holding its request-start value. Nothing between step (0) and step
(3) commits, rolls back, or expires anything — `enforce_gpu_quota` issues
two plain SELECTs, and `idempotency.claim`'s savepoint only expires on
the rollback path, which the capacity race never takes because it sends
no key. So with `populate_existing()` removed, every racer should read
its own request-start `allocated`, and the write is `cluster.allocated +=
requested` — a read-modify-write on the stale attribute, not a SQL
`allocated = allocated + n`.

Twelve racers all starting from `allocated = 0` should therefore all pass
the capacity gate and all write `2`, ending with **`allocated = 2`
against 12 committed reservations** — lost updates, catastrophic, and
exactly what the course path did with 20 of 5 seats and a counter reading
3.

**A measured the opposite** — 8/8 correct on four consecutive runs, with
`allocated` matching `SUM(active)` every time. That reconciliation is
what makes the result hard to wave away: it is the one check lost updates
could not survive.

So the two observations are not both measuring what they look like, and
the cheap next step is not more reasoning. Re-run the
no-`populate_existing()` race printing, per trial: the value read under
the lock, `allocated` at commit, and `SUM(gpu_count)` over active rows.
If the locked read is genuinely fresh in the request flow, then the
two-session probe is demonstrating something narrower than "this path
reads stale" — and that narrower sentence is what item 11 is missing.
**A's to run**; B's contribution is the prediction it has to contradict.

---

# Deadline 7 — the waitlist endpoints (B)

Written against items 9, 10 and 7 as **proposed by A and agreed by B**,
both halves recorded above. A's promotion transaction does not exist yet;
everything here is the half that can be built without it.

## The offering lock is `FOR SHARE`, and that is the same argument the room gate made

Joining reads `enrolled_count` to decide whether the offering is full and
never writes it. That is exactly the distinction Deadline 3 used to give
the room path a share lock while the GPU and registration paths take
`FOR UPDATE`: **lock exclusively only what you write.**

A share lock is still sufficient, and the reason is worth stating rather
than assuming. `register` and `drop` both take the offering row
`FOR UPDATE`, and `FOR SHARE` conflicts with `FOR UPDATE` — so a seat can
neither appear nor vanish between the fullness check and the commit. What
the share lock permits is two students joining the same full queue at
once, which is correct: they contend on nothing, and their entries differ
in `id` even when `created_at` collides.

Taking `FOR UPDATE` here would have been the safe-looking choice and
would have serialized every join of one offering behind every other, for
an exclusion nothing needs.

## Joining locks the user row, and the failure without it is the Benchmark 2 shape again

A student's concurrent `register` and waitlist-join touch **no common
row**: one writes an enrollment, the other a waitlist entry, and the
offering row is shared only under a lock that permits both. Without the
user lock the student ends up holding a seat *and* queueing for it.

That is the cross-cluster GPU quota race with different nouns. The
invariant — *a seat and a place in the queue are mutually exclusive* — is
a fact about the **user**, and no lock on an offering can see it. The
user row is the only thing the two paths share.

It also means the duplicate-join check can be a pre-check rather than a
caught `IntegrityError`: two joins by the same student serialize on that
lock, so the second one reads the first one's committed row.
`waitlist_unique` stays as the backstop, which is what would make a
mistake here fail loudly instead of queueing one student twice.

## Leaving takes the offering lock, and this is the one that is easy to miss

`leave_waitlist` deletes one row and touches no counter, so it looks like
it needs no offering lock at all. It does, and the reason is A's
transaction rather than B's:

``` text
promotion (inside drop)          leave
  holds offering FOR UPDATE
  reads oldest entry = X
                                   DELETE X        <- no lock: allowed
  promotes X
  COMMIT
```

Promotion would seat a student who had asked to be removed. The share
lock makes the leave wait for the promotion to commit, after which the
row is either already gone (promoted) or still there to delete.

And the reverse direction cannot deadlock, which is item 9 doing the job
it was proposed for: promotion takes candidate user rows `SKIP LOCKED`,
so a promotion meeting this transaction's user lock skips that candidate
rather than waiting on a transaction that is itself waiting on the
offering row.

## Leaving DELETES; dropping a seat does not

Asymmetric with `drop`, deliberately. An enrollment must keep a
`DROPPED` row because `enrollment_unique` is unconditional, so
re-registration is an UPDATE. A waitlist entry carries no history anyone
reads and **its absence is the state**.

A `LEFT` status would have to be excluded from every FIFO read — the
promotion query, the position window, the gate's row counts — and one
forgotten exclusion promotes a student who left. Deleting removes the
failure mode instead of documenting it.

The visible consequence is that leaving is not naturally idempotent the
way dropping is: a second `DELETE` gets `409 NOT_WAITLISTED` rather than
replaying the same body. That is the schema being honest — there is no
row left to describe.

## `OFFERING_NOT_FULL`: the new code that could have gone either way

Joining a queue for an offering that still has seats is refused rather
than quietly accepted. Accepting it would leave a student queued behind a
seat they could simply have taken, waiting for a promotion that only ever
fires on a **drop** — so a queue on a half-empty section is a student
waiting for nothing.

The alternative reading (accept it, promote them at the next drop) is
defensible but makes `position` meaningless: a student could be position
1 in a queue for a section with four free seats. Refusing keeps the
waitlist meaning one thing.

Three codes are new at this deadline, all `409`, all with different
remedies — which is the standing rule for why coded errors exist here:

``` text
ALREADY_WAITLISTED   you are already queued        -> nothing to do
NOT_WAITLISTED       you are not queued            -> nothing to leave
OFFERING_NOT_FULL    seats remain                  -> register instead
```

## Queueing costs no course-load quota

`held_course_enrollments` counts ACTIVE enrollments, and a queued student
has no enrollment row at all — so this falls out of the schema rather
than being a policy bolted on. A student at their cap of 6 may still
queue, and the quota is enforced at **promotion** time, where A's
transaction checks it and skips a candidate who would breach.

That is the right place for it. A waitlist entry holds nothing and costs
nothing, so charging quota for one refuses a student something they have
not yet received. Recorded with item 10's ratification.

## Position has exactly one definition, used twice

`_positions()` computes `ROW_NUMBER() OVER (ORDER BY created_at, id)` and
is called by both `GET /waitlist` and the number reported on a successful
join. The point is that the two cannot drift: "you are 3rd" from the join
response means precisely what the next `GET` will say.

The join reports its position **after** the commit and without a lock,
because it is a display value. The one place a stale position would
matter is promotion, which does not read this function at all — it
recomputes the order under the offering lock.

`DELETE` reports the position the student **held**, read before the row
is removed. It is the last true statement about an entry that no longer
exists, and recomputing it afterwards would report someone else's place.

## The gate is red on purpose, and it says one thing rather than seven

`scripts/check_waitlist.py` was written by A before either half existed,
with Part 2 failing the moment a `/waitlist` route appeared. Shipping B's
endpoints therefore turns it red, which is exactly what A built it to do
— **Deadline 7 is half-built and the gate is supposed to say so.**

What changed is precision. Part 2 now:

- runs **12 endpoint assertions** for real, all passing, including the
  eighth of A's original list — position moves 2 → 1 when the student
  ahead leaves, with the row's `id` and `created_at` untouched, which is
  what proves `ROW_NUMBER()` and not a stored column;
- probes for promotion **behaviourally** (fill, queue, drop, count) and
  reports its absence as **one** failure naming A's column, rather than
  seven separate broken-looking things;
- keeps the tripwire pointing both ways: if promotion lands while the
  seven promotion assertions are still unwritten, that is a **failure**,
  not a skip. A pending test that quietly passes forever is the thing the
  file exists to prevent.

---

# Deadline 7 — the promotion transaction and Benchmark 4 (written by B)

> **OWNERSHIP.** `EXECUTION_PLAN.md` assigns the promotion transaction to
> A. It was written by B because A's column had not started and Deadline 7
> could not close without it. It implements A's own proposal on item 9 as
> written, with one addition flagged below. **A has not reviewed any of
> it**, and that is a real cost: Deadline 10 asks each person to present
> the other's modules, and A now has one fewer module of their own.

## THE FINDING — Benchmark 4, as the plan specifies it, passes against the broken build

`EXECUTION_PLAN.md` specifies Benchmark 4 as *"2 concurrent drops on a
course with 3 waitlisted students; no offering lock -> same entry
promoted twice / seat lost."* Built and measured, the broken build
**passes it 15/15**:

``` text
2 droppers, 3 queued          promotions   (enrolled_count, active rows)
------------------------------------------------------------------------
no offering lock              {2: 15}      {(2, 2): 15}      <- PASSES
+ offering lock               {2: 15}      {(2, 2): 15}
```

This is the Benchmark 2 finding a second time, and it was found the same
way: by building the broken build first and running it, rather than
assuming the specified test would fail on it.

**Two independent reasons, and the first one is the interesting one.**

1. **`SKIP LOCKED` already prevents the double promotion.** Two drops
   racing on one queue both read the same oldest entry — but the first to
   take that candidate's user row keeps it, and the second is *skipped*
   onto the next entry. So the failure the plan predicts, *"the same
   entry promoted twice"*, cannot happen even without the offering lock.
   The mechanism item 9 introduced to avoid a **deadlock** turns out to
   also prevent this **double-write**, for free and by accident.

   Worth stating plainly because it inverts the plan's claim: the
   offering lock is *not* what stops the same student being promoted
   twice. `UNIQUE(student_id, course_offering_id)` and `SKIP LOCKED` are.

2. **With one promotion per drop, the counter arithmetic nets to zero.**
   Each transaction does `enrolled_count - 1 + 1`. A lost update writes
   back the same number it would have written anyway, so the corruption
   is real and invisible.

### What the offering lock actually protects, and the scenario that shows it

`enrolled_count` — exactly as at Benchmark 1. Promotion did not change
what that row's lock is for. Break the net-zero arithmetic by making the
drops outnumber the queue, and the builds separate cleanly:

``` text
8 droppers, 3 queued          promotions   (enrolled_count, active rows)
------------------------------------------------------------------------
no offering lock              {3: 10}      {(7, 3): 10}   <- 10/10 WRONG
+ offering lock               {3: 10}      {(3, 3): 10}
```

**Seven seats recorded as taken against three real enrollments.** Every
later registration is refused against a number that is a fiction, and
`offering_enrollment_sane` does not catch it because 7 is still ≤ the
capacity of 8.

Both scenarios ship and both run by default. The plan's is kept
deliberately: *"the specified test passes on the broken build"* is a
result, not a nuisance to be re-specified away.

> The generalisation, now twice-earned: **a concurrency test proves
> nothing until the broken build has been run against it.** Deadline 5
> learned to assert over trials rather than once; this adds that the
> trials must also be pointed at a failure the build can actually
> produce.

## Column 3: `SKIP LOCKED`, measured deterministically

B's condition on ratifying item 9 (session 15), and it found nothing
wrong — which is the point, because nothing else runs the clause at all.
Both scenarios above leave the queued students idle, so no candidate row
is ever locked.

Column 3 holds candidate 1's user row `FOR UPDATE` from a second session
for five seconds, then drops a seat:

``` text
candidate 1's user row : HELD for 5s by another session
drop returned in       : 0.05s        <- item 9's actual claim
still queued           : [candidate 1] <- skipped, kept its place
promoted               : [candidate 2] <- next eligible
```

`0.05s against a 5s hold` is the whole of item 9 in one number. *"Never
waits"* is a claim about time, and this is the only assertion in the
project that measures it.

**Deterministic on purpose.** Benchmarks 1-3 count over trials because
they race a sub-millisecond window where a single run is a coin flip.
Holding a lock is not a race — the row is held or it is not — so this
runs once. A concurrency test that can be made deterministic should be.

## The addition to A's proposal: promotion also skips a schedule clash

A's proposal skips a candidate whose **course-load quota** would breach,
following the Deadline 7 spec. It says nothing about **schedule
conflicts** — and without that check, promotion can seat a student in a
class that clashes with one they already hold, which `register` refuses
outright. The same illegal state, reached through a different door.

A schedule clash is a fact about the student, guarded by the user lock,
for exactly the reason a quota is. Both are now checked, and a candidate
failing either is *skipped* rather than refused, because "refuse" has no
meaning on a path nobody is waiting on.

**Flagged rather than folded in silently:** it widens the eligibility
rule that item 9 defined, so A should agree with it explicitly. It is one
call to `_conflicting_offering`, the same helper `register` uses.

## Every skip is logged

The other half of B's condition. A student passed over changes no row —
no status, no timestamp, nothing — so without a log line a student can
lose their turn with no record anywhere that it happened. All three skip
reasons log at INFO with the entry id, the student id and the offering:
`user row busy`, `course-load quota`, `clashes with offering N`.

This is what makes *"the promise is oldest **eligible**, not oldest"* an
auditable statement rather than a disclaimer.

## The seat moves; it is never released and re-taken

Promotion runs **inside** the drop transaction, holding the two locks the
drop already took. The counter goes `-1` for the drop and `+1` for the
promotion before a single commit, so there is no instant at which the
freed seat is visible to an ordinary `register`. A queued student cannot
lose their seat to someone who happened to be refreshing the page.

That is also why the reconciliation assertion is the sharp one: the seat
moving atomically means `enrolled_count` and the ACTIVE row count must
agree after every trial, and the broken build is caught precisely there.

---

# Deadline 7 — A's review of the promotion transaction

A owns the promotion transaction in the ownership split; **B wrote it**
(session 17, "writing A's column") because A was unavailable and the
critical path ran through it. This is A's review, which the plan says is
still owed. **Verdict: the implementation is faithful and correct. One
reachable bug found, outside the transaction, reproduced and fixed.**

## What was checked and holds

- **`SKIP LOCKED` implements item 9 as proposed.** Candidates are read
  under the offering lock the caller already holds, and each candidate's
  user row is attempted `FOR UPDATE SKIP LOCKED`. A transaction that
  never blocks on a user row cannot appear in a wait cycle, so §14's
  "every path" claim needs no exception — which was the point.
- **Two concurrent drops cannot double-promote**, and the mechanism is
  the *offering* lock, not the candidate lock: both droppers serialize on
  it, so the second reads a queue the first has already modified.
- **The quota check is under a real lock** — the half of item 9 that
  mattered. `enforce_course_quota` runs after `SKIP LOCKED` acquired the
  row.
- **`populate_existing()` on the candidate read**, and on the offering
  read in `drop`. Item 11's lesson applied without being asked.
- **`enrollment_unique` is unconditional**, so promotion UPDATEs a
  DROPPED row rather than INSERTing beside it. Both branches reachable.
- **B's one addition beyond A's proposal — the schedule-clash skip — is
  correct and A agrees with it explicitly.** A clash is a fact about the
  student guarded by the same user lock as the quota. Without it,
  promotion could seat a student in a class that clashes with one they
  hold, a state `register` refuses outright — the same invariant reached
  through a different door. It widens the eligibility rule item 9
  ratified, and widening it is right.

## The bug: a seat and a queue place were not mutually exclusive

`join_waitlist` states the invariant and enforces its own side. `register`
did not: **registering directly for a seat never cleared the student's
waitlist entry.** Reachable, and it lost a seat silently.

``` text
X queues for a full offering
a drop frees the seat, but promotion SKIPS X        <- schedule clash
X clears the clash and REGISTERS directly           <- entry left behind
X now holds a SEAT and a QUEUE PLACE
the next drop promotes X again
    -> enrolled_count = 2, ACTIVE rows = 1
```

The counter said the seat was taken and no student held it. Nothing
surfaces it at the time; **Deadline 8's reconciliation query is what would
eventually have reported it**, long after the cause.

### The first reproduction attempt failed, and why that matters

The probe was first built with a **quota** skip. It came back clean, and
the reason is worth keeping: the candidate was at their cap *only
because the seat they already held counted toward it*, so the quota gate
refused the second promotion **by accident**. Rebuilt with a
**schedule-clash** skip, which leaves the student far below their cap,
it reproduced immediately.

Two gates look like they would catch this and neither does:

- the **quota** gate catches it only when the student is coincidentally
  at their cap;
- the **schedule** gate never catches it, because
  `_conflicting_offering` excludes the target offering itself, so a
  student's own enrollment in it is not a clash.

A regression test written the first way would have passed against the
broken build — this project's oldest lesson, arriving for the fourth
time.

## The fix, in two places

1. **`register` deletes any waitlist entry for that offering.** This is
   the path that creates the inconsistent state, so this is the fix.
   Safe under locks it already holds: the user row blocks a concurrent
   join by the same student, and joins take the offering `FOR SHARE`,
   which its `FOR UPDATE` excludes.
2. **`_promote_one` skips a candidate who already holds an ACTIVE
   enrollment**, and *deletes* the stale entry rather than merely
   passing over it — a queue place for a seat you already hold can never
   be honoured. A backstop, not the fix; unreachable on a correct build.

Verified both ways: with the register-side fix disabled the gate reports
`FAIL registering directly CLEARS that student's queue place -- 1 queue
entries left`, and the promotion backstop holds the counter consistent
even then. With both in place, all nine gates pass.

## Two assertions added to `scripts/check_waitlist.py`

``` text
registering directly CLEARS that student's queue place
no double-promotion: enrolled_count == ACTIVE rows
```

The second is Deadline 8's reconciliation query run early, on the
narrowest path that breaks it.

## Still outstanding after this review

- **Items 9, 10 and 7 remain unratified on paper.** Both positions were
  written and agree, and the code is built against them. Ratifying is now
  confirmation rather than decision, but it has still not happened.
- **Item 11** is unchanged and still A's.

---

# Deadline 8 — the integration pass and the final numbers

## The error-code audit: no drift

Every code the application can emit was extracted from the **AST** rather
than by grepping — `coded_error()` calls span several lines, and a
line-oriented grep silently missed them (it reported an empty set and
made every documented code look orphaned). Parsed properly:

``` text
coded_error() call sites : 15 distinct codes
emitted but undocumented : NONE
```

All fifteen appear in `ARCHITECTURE_AND_WORKFLOWS.md` §7. The two 409s
the deadline names specifically — `CAPACITY_EXHAUSTED` and
`QUOTA_EXCEEDED` — remain distinct, on distinct remedies.

> **A false finding, caught before it was written down.** The first pass
> reported `EMAIL_ALREADY_REGISTERED` as undocumented. It is documented,
> at line 352; the `sed` range used to read §7 stopped short of it,
> because §7's code table is not one block but several separated by
> prose. The audit was wrong, not the docs. Recorded because an
> integration pass that invents discrepancies is worse than none — it
> costs someone an afternoon proving the code was fine all along.

## Final numbers — all four benchmarks, both builds

Every row below was run **in this session**, not quoted from an earlier
one. `.env` was toggled and the container recreated between columns, then
restored and re-verified.

``` text
BENCHMARK 1  capacity     200 students, 20 seats, 3 trials
  broken (no offering lock)   oversold 3/3,  counter mismatch 3/3,
                              up to 200 of 200 succeeded
  fixed                       oversold 0/3,  exactly 20 every trial,
                              peak DB concurrency 40 of 200 submitted

BENCHMARK 2  quota        1 student, 2 clusters, 25 trials
  broken (no user lock)       over-quota 25/25
  fixed                       over-quota 0/25,  held {2: 25}

BENCHMARK 3  exactly-once  8 simultaneous retries, 15 trials
  no key                      8 holds per trial  {8: 15}
  with key                    1 hold per trial   {1: 15}, 1 key row,
                              {201: 120}, divergent bodies 0/15

BENCHMARK 4  waitlist      3 queued, concurrent drops, 15 trials
  broken (no offering lock)   COUNTER DISAGREED 15/15
  fixed                       3 promotions/trial, counter reconciles 15/15,
                              FIFO broken 0/15
  column 3 (SKIP LOCKED)      candidate row held 5s; drop returned in
                              0.02s, held candidate skipped, next promoted
```

## The Benchmark 4 broken column says something we did not predict

Removing the offering lock produced **`WRONG PROMOTION COUNT 0/15` and
`FIFO BROKEN 0/15`** — the right students were promoted, in the right
order. What broke was `enrolled_count`: **15/15 counter mismatches.**

So the double-promotion the benchmark was designed to catch **did not
happen**, and the damage landed on the counter instead — concurrent drops
reading a stale `enrolled_count` and each writing back its own
increment, which is a lost update rather than a lost seat.

This is the third time this project has measured a broken build and found
the failure was not the one predicted:

``` text
Benchmark 2   the lock was not missing, it was the WRONG LOCK
Benchmark 3   no build over-allocated; UNIQUE held the row count, and
              what the fix bought was the REPLY, not the uniqueness
Benchmark 4   no build double-promoted; the offering lock protects the
              COUNTER, and the queue was never the fragile part
```

The pattern is worth naming in the README, because it is the project's
most defensible claim: **every one of these was measured rather than
reasoned about, and in each case the measurement contradicted the
intuition the benchmark was built on.** A table of four predictions that
all came true would be a weaker artifact, not a stronger one.

## Deadline 8's own checkpoint

``` text
no new features                          held -- nothing added this session
every endpoint returns the agreed codes  15/15 audited, no drift
CAPACITY_EXHAUSTED vs QUOTA_EXCEEDED     distinct, distinct remedies
bugs surfaced by the benchmarks          one, found in review at Deadline
                                         7 and fixed with a regression
                                         test that fails on the old build
re-run all four, record final numbers    done, above, both builds
```

## The clean-room test found nothing wrong with the code, and three things wrong with the README

Deadline 9's BOTH column ran clean on the first attempt: fresh image,
fresh volume, five migrations, seed, nine gates green, `6 passed` from the
harness suite, four benchmarks reproducing. No code was changed to get
through it.

What it did break was the **documentation**, in one specific and
repeatable way. Three numbers in the README had been measured from runs
with arguments — `--trials 3`, `--trials 10` — and then printed beside a
command with no arguments. The benchmarks' defaults are 5, 25, 15 and 15.
So every one of those numbers was true when taken and false as published:
a stranger running the printed command gets a different denominator.

``` text
claim as published                    what the printed command produces
----------------------------------------------------------------------
B4, 8v3, {3: 10} / {(7,3): 10}        {3: 15} / {(7,3): 15}
B1 broken, 3/3 oversold, 377-500      5/5 oversold, 500 of 500 every trial
column 3, 0.02s-0.09s                 0.10s on the clean-room fixed run
```

**The Benchmark 1 correction runs the wrong way, which is the
interesting part.** We had recorded "up to 500 of 500" from two of three
trials. Run at the documented default, the unlocked build seated **all
500 students in all five trials**, and `enrolled_count` recorded 14 to 21
of them. We had been *under-claiming our own broken build* — the honest
number is more damning than the one we had written down. A documentation
error that flatters the fixed build would have been embarrassing; one
that flatters the *broken* build is just sloppy, and it cost us the
sharpest version of our own result.

**The rule this yields, and it is cheap to follow:** a number goes in the
README only from a run of the exact command the README prints. If a
number needs a flag, the flag goes in the README next to it. `.env`
switches count as flags — Benchmarks 1, 2 and 4's broken columns are
selected that way, and the README documents the switch precisely because
of this.

**Why this is worth a page rather than a line.** The checkpoint for
Deadline 9 is not "the numbers are right" — ours were, every one of them,
as measurements. It is *"a stranger could clone the repo and reproduce
your numbers from the README alone."* Those are different claims, and the
gap between them is invisible from inside a machine where the stack has
been running for nineteen sessions. It took a run with nothing cached,
nothing already migrated, and no arguments remembered to see it.

The same run settled the `pytest-asyncio` question session 19 raised.
Six harness tests had been silently skipping in the long-lived container
because the package was in `requirements.txt` but not in the image. The
predicted fix was "a fresh build"; the clean room is a fresh build, and it
reports `6 passed`, `plugins: asyncio-0.25.0`, strict mode. That
prediction is now measured rather than assumed — which matters, because
every benchmark number in the README rests on that harness genuinely
overlapping its requests.


---

# Deadline 10 — the cross-presentation, and what it measured

The presentations themselves, with every correction kept in place, are in
`CROSS_PRESENTATION.md`. This entry is the part that belongs here: the
decisions the exercise forced, and the one measurement that came back
different.

## The finding: we were wrong about what we had read, never about what we had measured

Both presentations were scored, and the errors do not sit where effort
sat. They sit exactly where *measurement* was absent.

``` text
A on B's modules            wrong on FOR SHARE, wrong on what enforces
                            room overlap, wrong on WAITLISTED, wrong on
                            stored positions, wrong on "500 concurrent"
A on courses                RIGHT, in detail -- populate_existing(), which
                            A found at Deadline 4 by losing a deadline to it
B on A's modules            presented the Deadline 1 role-in-the-claim
                            tradeoff that Deadline 3 reversed; missed the
                            SAVEPOINT; missed the hash() salt trap
B on the GPU transaction    RIGHT, all four steps and their independence --
                            the transaction B had raced from the outside
                            in four benchmarks
```

**Neither of us was wrong about a mechanism we had personally measured.**
Every correction is about a mechanism one of us had only read.

The sharpest instance is A on promotion. A had reviewed that transaction
in session 18 closely enough to find a **reachable bug** in it — the
seat-and-a-queue-place inconsistency — and still credited the offering
lock for preventing double-promotion. It does not. `SKIP LOCKED`
prevents it, and the offering lock protects the *counter*; that is
Benchmark 4's finding and it is the one thing about that function no
amount of re-reading the source can tell you. Reading it carefully enough
to find a bug was not enough to get the mechanism right.

This is the same lesson as Deadline 5's and it arrives from a new
direction. There, a regression test written the intuitive way passed
against the broken build. Here, a careful reading produced a confident
and wrong attribution. **In both cases the only thing that separated true
from plausible was running it.**

## Decision: A presents promotion, and the plan is not amended

`EXECUTION_PLAN.md` assigns promotion to A. B wrote it, in session 17,
because A's column had not started and Deadline 7 could not close.
Deadline 10 as written would therefore have had B present B's own code,
which measures nothing.

**Reassigned for the exercise; the plan left alone.** The plan records
who *should* have written it and the log records who *did*, and editing
the first to agree with the second deletes the only evidence of the gap.
This is the second time this project has chosen to leave a document
disagreeing with events on purpose — the first was refusing to let three
dated sessions imply three met deadlines.

## Decision: the countersignature is a re-run, not a reading

Deadline 8 and Deadline 9's BOTH columns were executed solo by A. B
signed both, and the form matters: **B re-derived the error-code audit
from the AST and re-ran all four benchmarks on both builds from a real
clone**, rather than reading A's numbers and agreeing with them.

A countersignature that is a reading is worth nothing here, because the
failure mode this project has actually suffered is not arithmetic — it is
a number measured under conditions the document does not state. Only a
re-run under the documented conditions can catch that, and it did.

## The one number that did not reproduce, and the rule it yields

``` text
                                  session 20 (fresh tree)   session 21 (real clone)
--------------------------------------------------------------------------------
B1 broken, oversold trials        5/5                       5/5
B1 broken, seats sold             500 of 50, every trial    500 of 50, every trial
B1 broken, enrolled_count         14 - 21                   15 - 26     <- differs
B2 broken                         25/25, held {4: 25}       25/25, held {4: 25}
B4 broken 8v3                     {3:15} / {(7,3):15}       {3:15} / {(7,3):15}
B4 column 3                       0.10s                     0.06s
all fixed columns                 as published              as published
```

Nineteen of twenty. The exception is a **range**, and that is the point:
`enrolled_count` under a lost-update storm is not a property of the
build, it is the residue of whichever interleaving happened. We published
one run's spread as though it characterised the failure.

**The rule from Deadline 9 was:** a number goes in the README only from a
run of the exact command the README prints. **Deadline 10 adds:** and a
*range* needs more than one run before it is written as a range. A single
run gives you a point, not an interval, however many trials are inside
it.

Corrected in the README by stating both runs rather than replacing one
with the other — the two together are the honest description, and they
also make the actual claim clearer: **5/5 oversold and 500-of-50 are
stable, and only the counter's landing point moves.** The stable half was
always the damning half.

## The Deadline 9 qualification is discharged

Session 20's clean room was assembled from `git ls-files` because the
tree was uncommitted — verified *content*, unverified *repository*. The
commit landed, and the quickstart ran from an actual `git clone` into a
scratch directory against a new volume, with its own `JWT_SECRET`:

``` text
git clone                87 files; README and scripts/_db.py present
alembic upgrade head     5 revisions -> 1ca8b85b7626
scripts/seed.py          exit 0
9 gate scripts           ALL exit 0
pytest tests/ -v         6 passed, 0 skipped, asyncio-0.25.0, STRICT
4 benchmarks, both builds  as tabulated above
```

Worth stating plainly because it was the risk: **a stranger cloning this
repository now gets a README, nine gate scripts that import cleanly, and
four benchmarks that reproduce.** Before the commit they got none of
those, and the clean-room test that "passed" could not have caught it.

## Decision: item 11 ships open

Item 11 — the two-session probe says the GPU path's `FOR UPDATE` read is
stale without `populate_existing()`, and the 12-racer capacity race says
the invariant holds without it — is the oldest item in the project, open
since session 10, and it closes **unresolved**.

The decision at Deadline 10 was whether to keep it open or quietly drop
it, since nothing depends on it and no benchmark fails. **Kept open, and
promoted into the README's known limits**, where it had not been. The
argument for dropping it is that we cannot make it fail; the argument
against is that "we could not make it fail today" is not the same claim
as "the read is correct", and this project's entire thesis is that those
two are different sentences. Dropping it would contradict the thing we
are claiming to have learned.

## Decision: the demo never shows Benchmark 1

Benchmark 1 is the most visually dramatic result — 500 students in a
50-seat section — and it is unusable on stage. Measured on the clone
stack: **8-9 seconds median per request**, five trials of 500. Over a
minute of scrolling.

So the five-minute demo is built **backwards from Benchmark 2**, which is
25 trials in well under a minute and is also the argument the project is
actually about. Benchmark 1's result goes on a slide. The full script,
with the rehearsal notes, is `CROSS_PRESENTATION.md` §4.

The rehearsal produced one rule worth keeping beyond this project:
**nothing that requires a rebuild happens on stage.** Both `.env` states
are prepared and both containers recreated before the demo starts; the
live portion is two commands that print a number.

## Decision: what the résumé bullet leaves out

No throughput number, no latency number, no line count, no endpoint
count. The bullet leads on **"three independent serialization points"**
and on **"building each build broken first"**, because those are the two
phrases most likely to earn a follow-up question, and the follow-ups are
the strongest material we have.

A throughput number would be the opposite: we never optimised for it, so
it invites a question whose honest answer is "nothing".

---

# After the close — the catalogue endpoints (25 Aug 2026)

The plan closed with all ten deadlines met and three things shipping open.
One of them was a **gap rather than a decision**: `POST /courses` and
`POST /offerings` did not exist, belonged to no deadline, and had never
been assigned. This entry is what closing it cost and what it turned up.

## Decision: closing a gap is not licence to reopen the freeze

Two endpoints, both ADMIN, both `201`. Explicitly **not** built: no
`PATCH /courses/{id}`, no `DELETE`, no offering edit. Editing an offering
means editing `capacity`, and Deadline 6 already established what that
costs on the GPU side — `gpu_capacity_sane` forbids `allocated >
gpu_count`, so "lower it while seats are held" is a state Postgres
rejects and `PATCH /gpus/{id}` answers `409
CAPACITY_BELOW_ALLOCATED`. The offering analogue is
`offering_enrollment_sane` and it behaves identically. That is a locking
transaction with a refusal of its own, not a form, and it is not the gap
that was recorded.

## The gap was concealing two invariants nothing enforced

This is the finding, and it is the same shape as the four that came
before it: what broke was not what we expected to break.

The endpoints were supposed to be plumbing — a `Session.add` behind an
ADMIN gate. What made them non-trivial is that **for ten deadlines the
seed script was the only writer, and it happened to write correctly**, so
two invariants the schedule logic depends on had never been stated
anywhere they could be checked:

``` text
_times_overlap   compares "HH:MM" LEXICOGRAPHICALLY
                 -> one unpadded "9:00" sorts after "10:30" and INVERTS
                    conflict detection for every student in that section
_days_overlap    is a SET INTERSECTION over characters
                 -> set("Tu") & set("Th") == {"T"}, so a Tuesday class
                    reports a phantom clash with a Thursday one
```

Both were documented in comments — `service.py` said outright *"if an
offering-creation endpoint ever lands, this is the vocabulary it must
validate against"* — and a comment is not a constraint. Neither the
database nor any test would have caught `"9:00"`; it is a valid
`String(5)`, and the resulting corruption is silent, student-visible and
would look exactly like a locking bug.

**A gap in the API was hiding a gap in the validation**, and only the
first one was written down.

`DAY_CODES` moved to `schemas.py` as part of this, where the validating
happens, with `service.py` importing it. One definition — the `_db.py`
rule about two definitions of one budget applies verbatim, and the drift
here would surface as a phantom schedule conflict rather than as an
import error.

## Decision: the duplicate code is caught, never pre-read

`COURSE_CODE_TAKEN` is a caught `IntegrityError`, and the reasoning is
Deadline 2's on duplicate registration, reused rather than rediscovered:
two admins can both pass a "does this code exist?" read and only one can
insert, so a pre-check turns a clean `409` into a `500` **exactly when it
is contended**. Asserted rather than argued — eight barrier-released
creates at one code:

``` text
statuses          : [201, 409, 409, 409, 409, 409, 409, 409]
rows in courses   : 1
5xx               : 0
```

**One thing this cost, and it is worth recording because the guess was
wrong in a way that fails loudly only under contention.** The constraint
name was first written as `courses_code_key`, the name Postgres gives a
unique *constraint*. The model declares `unique=True, index=True`, and
that pair makes SQLAlchemy emit a unique **INDEX** — so the real name is
`ix_courses_code` and the guess matched nothing. The failure mode is the
point: sequential use is unaffected, and the first genuine duplicate
would have come back as an uncaught `IntegrityError` and a `500`. Checked
against `pg_indexes` rather than assumed, and the 8-way race is now the
regression test for it.

Codes are matched from psycopg's `diag`, never from message text — the
same discrimination `_is_overlap_violation` makes for the room
constraint.

## Decision: 404 for a missing id, coded 409 for a wrong role

`POST /offerings` resolves `course_id` and `instructor_id` explicitly
before inserting, rather than letting the foreign keys do it: **one
`ForeignKeyViolation` cannot say which of the two ids was wrong**, and
that is the only thing the caller needs to know.

The split follows §7's existing rule exactly. An id that names nothing is
a plain `404` — one remedy, nothing to branch on. A user who exists and
is a STUDENT is `409 INSTRUCTOR_NOT_FACULTY` — well-formed request,
resolving id, refused on policy, which is the case a machine-readable
code exists for. FACULTY only, not FACULTY-or-ADMIN: an admin who teaches
holds a FACULTY account, and widening the check to save them a row would
make `instructor_id` mean "someone".

## What was deliberately left undone: the instructor double-booking check

Two sections at the same hour with the same instructor are creatable.
`_days_overlap` and `_times_overlap` are right there and the check is
four lines — but a correct one holds the instructor's row `FOR UPDATE`
for the duration, and an incorrect one is an **unlocked boundary read**,
the precise anti-pattern this project spends four benchmarks refusing. A
gate two concurrent admins walk straight through reads as a guarantee
while being none, which is worse than its absence.

Stated in the README as a known limit. Students remain protected — a
student's own clash is `SCHEDULE_CONFLICT`, checked under that student's
row lock, because a schedule clash is a fact about the person.

## Verification, and what it did not change

``` text
error-code audit (AST)   17 distinct codes, undocumented: NONE
                         (15 before; the two new ones are in §7)
10 gate scripts          ALL exit 0   (check_catalog.py is 51 assertions)
pytest tests/            6 passed
alembic check            no drift, head 1ca8b85b7626 -- NO MIGRATION,
                         both tables shipped at Deadline 1
benchmark 1 capacity     oversold 0/5, exactly 50/trial
benchmark 2 quota        over-quota 0/25, held {2: 25}
benchmark 3 exactly-once no key {8: 15}, key {1: 15}, divergent 0/15
benchmark 4 waitlist     3 promotions/trial, counter reconciles 15/15
```

**No benchmark number moved, and none should have.** These endpoints take
no lock, touch no counter under contention, and write `enrolled_count = 0`
at the one moment in that column's life when writing it outside
`register`'s transaction is safe. The three serialization points are
untouched. That the four tables reproduce unchanged is the evidence that
this addition is as small as it claims to be.
