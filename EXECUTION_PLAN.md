# Two-Person Execution Plan — Full Scope

> **Deadlines, not days.** The ten stages below are ordered milestones,
> not calendar dates. "Deadline 4" means *the fourth checkpoint*, and it
> takes as long as it takes — it may span one sitting or four. Nothing in
> this file assumes a date.
>
> This file says **what must be true** to call a deadline met.
> `WORK_LOG.md` says **when work actually happened**. Keeping those two
> apart is the point: the log had drifted into implying that three
> deadlines were met because three dated sessions existed.
>
> **Status: Deadlines 1-4 met (sessions 4, 7, 9 and 10). Deadline 5 is
> half met — A's idempotency column landed in session 11 and is verified;
> the deadline closes on B's harness and Benchmark 1.**

## 0. Scope

Nothing is cut. Effort check: 2 people across 10 deadlines ≈ **20
person-days** of work, against the original solo plan's 15, plus roughly
3 person-days for the added features. It fits — with no slack. That is an
estimate of *effort*, not a schedule; the deadlines flex, the work does
not.

``` text
IN SCOPE
  JWT auth + RBAC (require_role dependency)
  GPU:     capacity + quota + idempotency      <- flagship transaction
  Rooms:   exclusion-constraint intervals + room quota
  Courses: capacity + duplicate + schedule overlap + course-load quota
           (registration is keyed on the OFFERING -- that is the row
            holding enrolled_count, and therefore the row that is locked)
  Waitlist: FIFO by (created_at, id), quota-aware promotion,
            concurrency-safe, no stored position
  Four broken-vs-fixed benchmarks
  asyncio concurrency harness
  Docker Compose one-command startup

DELIBERATELY EXCLUDED (design decision, not a scope cut)
  Denormalized quota counter
  Locust load scenarios          <- cut Deadline 1; item 1 on the cut order
  GPU reservation start/end times <- do not re-add; see DECISIONS.md
```

**Why the counter stays out.** The MVP recomputes held units by `SUM`
under the user lock, which cannot drift. A counter is an optimization,
and your stated principle is that complexity is earned by measurement.
Building it with no benchmark showing user-row contention invites the
question "what did it improve?" with no answer. Leaving it out, and
saying *why*, is the stronger position. Revisit only if the concurrency
harness shows the user row is the bottleneck — which, with Locust cut, is
now the only instrument that could show it.

## 0.1 Pre-Agreed Cut Order

Full scope means Deadline 10 is no longer a buffer. Slippage has nowhere to
go, so decide the cut order **now**, while calm — not on Deadline 8 under
pressure. If you are behind at any checkpoint, drop strictly in this
order:

``` text
1. Locust                    CUT ON DEADLINE 1 -- the asyncio harness already
                             proves correctness, which is the claim
2. Room quota                (mechanism identical to GPU; document it)
3. Course-load quota         (same)
4. Schedule-overlap check    (orthogonal to the concurrency story)
5. Waitlist                  (largest single item; cut whole, not half)
```

Never cut in this order: the GPU transaction, RBAC, or any of the first
three benchmarks. Those are the project.

Anything cut gets a README section titled "Designed, not implemented,"
with the mechanism described. A documented deferral reads as judgment; a
missing feature reads as failure.

------------------------------------------------------------------------

## 1. Ownership Split

The core is a single ~60-line transaction and cannot be split in half.
So the division is by **layer and verification**, not by feature: A
builds the correctness core, B builds the resources and writes the tests
designed to break A's core. Each person's benchmark is the proof of the
other's work.

``` text
PERSON A - Correctness Core          PERSON B - Resources & Verification
--------------------------------------------------------------------------
core/security.py   (JWT, Argon2)     rooms/     (intervals, exclusion)
core/dependencies.py (require_role)  courses/   (enrollment, capacity)
auth/              (register/login)  reservations/ (cancel, list)
quotas/            (RoleQuota)       waitlist/  (endpoints, FIFO order)
idempotency/       (keys, replay)    Docker + docker-compose
gpus/              (THE transaction) seed script
waitlist promotion transaction       tests/concurrency/ (harness)
admin quota endpoints                all four benchmarks
                                     README assembly
```

Note the deliberate crossovers: A applies the quota helper inside B's
room and course modules (Deadline 6), and A owns the waitlist *promotion
transaction* while B owns the waitlist *endpoints*. Neither person can
finish without reading the other's code.

### Shared-file protocol

``` text
models/     -> joint Deadline 1 session ONLY. After that, changes require both
               people present. No solo edits.
alembic/    -> PERSON A OWNS EXCLUSIVELY. B never runs `alembic revision`.
```

Two people independently generating Alembic revisions creates divergent
`down_revision` chains that cost half a day to untangle. With no buffer,
that half-day is fatal. If B needs a schema change, B asks A.

### Interface-first unblocking

B is blocked on auth from Deadline 2. On **Deadline 1** jointly fix the signatures:

``` python
def get_current_user(...) -> User
def require_role(*allowed: Role) -> Callable
```

B codes against a hardcoded stub returning a fake `User`. A implements
the real one. Swap on Deadline 3 - same import path, no refactor.

------------------------------------------------------------------------

## 2. Deadline-by-Deadline

### Deadline 1 - Foundation (JOINT, do not parallelize)  ✅ MET

``` text
BOTH   repo, .env, docker-compose (Postgres + app), FastAPI skeleton
BOTH   write ALL models together in one sitting - including RoleQuota,
       IdempotencyKey, WaitlistEntry, CourseOffering
BOTH   first Alembic migration; verify on a clean DB on BOTH machines
BOTH   agree get_current_user / require_role signatures
BOTH   agree error codes: 401 / 403 / 409 CAPACITY_EXHAUSTED /
       409 QUOTA_EXCEEDED / 422 IDEMPOTENCY_KEY_REUSED
```

Divergence here poisons all nine remaining deadlines. This is the one
stage where pairing is faster than splitting.

**Checkpoint:** `docker compose up` works, `/docs` loads, migration
applies cleanly for both. — **All three verified (session 4).**

> Met by the checkpoint, with two threads still hanging off it. Neither
> is a checkpoint item, so neither reopens the deadline, but both
> originate here and both need B:
>
> - *"Write ALL models together"* held for the first sitting only.
>   `models/` was then changed **solo** across three revisions, which the
>   shared-file protocol forbids after this deadline. B has not reviewed
>   them.
> - Outstanding items 6 and 7 in `DECISIONS.md` are model-semantics
>   questions the joint session was meant to settle. Item 7 blocks
>   Deadline 7.

### Deadline 2 - Auth vs. Read Paths  ✅ MET

``` text
A      Argon2 hashing, JWT encode/decode, role as a token claim
       POST /auth/register, POST /auth/login
       duplicate-email registration -> 409, not 500: catch the
       IntegrityError, because two simultaneous registrations can both
       pass a "does this email exist?" check
B      seed script: 3 users (one per role), 2 GPU clusters, 2 rooms,
       1 course + offering
         capacity and instructor_id go on the OFFERING, not the course
         seed RoleQuota rows: (STUDENT,GPU)=2 (FACULTY,GPU)=10
                              (ADMIN,*)=NULL, ROOM 2/5, COURSE 6
         any waitlist rows must be committed separately or given explicit
         created_at -- func.now() is TRANSACTION start time, so rows
         seeded in one transaction share it and their FIFO order is undefined
       GET /gpus, /rooms, /courses, /{id}/availability (against the stub)
```

**Checkpoint:** login returns a token containing a role; B can list
resources. — **Both verified (sessions 6 and 7).**

> Met, with one thing the checkpoint does not actually test. `GET /gpus`
> returns 200 with a bearer token — and also with no token at all,
> because `dependencies.py` is still the Deadline 1 stub. The token is
> *accepted*, not *verified*. Deadline 3 is what makes it load-bearing,
> so this is a sequencing fact rather than a gap; recorded so the green
> result is not read as more than it is.
>
> One addition to the plan as written: `core/errors.py`. Deadline 1
> agreed the error codes but never the JSON envelope carrying them, and
> duplicate registration was the first endpoint to need one. Deadlines 4
> and 5 must emit `CAPACITY_EXHAUSTED`, `QUOTA_EXCEEDED` and
> `IDEMPOTENCY_KEY_REUSED` through `coded_error()` rather than inventing
> a second shape.

### Deadline 3 - Authorization vs. Rooms  ✅ MET (sessions 8 and 9)

``` text
A ✅   require_role dependency; apply to all admin-only routes
A ✅   verify 403 fires BEFORE the handler body executes
A ✅   hand the real dependency to B, delete the stub
A ✅   GET /me  -- was in ARCHITECTURE_AND_WORKFLOWS.md and INIT_PLAN.md
       §12 with no deadline assigned, same gap the PATCH endpoints had.
       It belongs here and nowhere else: it is the end-to-end proof that
       a real token decodes to a real user carrying a role, which is the
       claim this deadline makes. Assigned to A, who owns the decode.
A ✅   POST /gpus and POST /rooms [ADMIN] -- NOT in this column as written,
       and the checkpoint below could not run without them: it names
       POST /gpus, which did not exist, and no other admin-only route did
       either, so require_role had nothing to guard. Creating a resource
       is [ADMIN] in both role matrices and in INIT_PLAN.md §12, with no
       deadline assigned -- the third instance of that gap after GET /me
       and the PATCH endpoints. Assigned here by the checkpoint.
B ✅   room reservation POST
       the EXCLUDE USING gist constraint already exists -- shipped Deadline 1
       in revision e0fbfe421403, along with the btree_gist extension.
       B writes the endpoint against it; no migration needed.
B ✅   include the resources.status gate while writing the lock (below)
       -- item 6 RATIFIED as proposed; 409 RESOURCE_BLOCKED, no eviction
B ✅   adjacent-interval test: [10,12) and [12,14) both succeed
B ✅   reservations.resource_id points at `resources`, so nothing at the
       DB level stops a "room" booking naming a GPU cluster -- check the
       resource_type in the service layer

       One deviation from the sketch below, deliberate: the room gate
       takes the resource row FOR SHARE, not FOR UPDATE. The sketch is
       written for the GPU transaction, which WRITES the row it locks.
       The room path never does -- rooms have no counter -- so an
       exclusive lock would serialize every booking of one room behind
       every other and make the application lock, rather than the
       exclusion constraint, the thing deciding a concurrent slot race.
       FOR UPDATE stays correct and stays the rule at Deadline 4.
```

**Checkpoint:** student token on `POST /gpus` returns 403; overlapping
room booking returns 409. — **BOTH HALVES VERIFIED (sessions 8 and 9).**

**First half (session 8):
`scripts/check_rbac.py`, 34/34, including the row counts on both sides of
the 403 that show it fired before the handler body.
**Second half (session 9):** `scripts/check_rooms.py`, 34/34, including
eight barrier-released identical bookings landing exactly one row.

> One decision reversed while meeting the first half, recorded in
> `DECISIONS.md`: `require_role` reads the role from the **database row**,
> not the token claim. Deadline 1 accepted a 60-minute stale-role window
> to save a lookup per request — but freezing `get_current_user() -> User`
> in the same session already spent that lookup, so the window was being
> paid for nothing. It is now zero.
>
> **Outstanding item 6 is now ratified** (session 9), as proposed:
> BLOCKED stops new allocations, does not evict existing ones, and
> carries `409 RESOURCE_BLOCKED`. The GPU half of that gate lands with
> the transaction at Deadline 4.

### Deadline 4 - Flagship vs. Courses (the heaviest)  ✅ MET (session 10)

``` text
A      quotas/ module: RoleQuota table + seeded defaults
       GPU transaction: LOCK user -> SUM held -> quota check
                        LOCK cluster -> status check -> capacity check
                                     -> write
       lock order recorded in a code comment
B      course registration: POST /offerings/{id}/register
       LOCK offering -> capacity -> upsert enrollment -> enrolled_count++
       UNIQUE(student_id, course_offering_id) is unconditional, so a
       student who dropped still owns a row: re-registration is an
       UPDATE, not an INSERT
       every write to enrollments happens in the SAME transaction as the
       matching enrolled_count update -- it is derived state
       schedule-overlap check against existing enrollments
       DELETE /reservations/{id} with owner-or-admin check
```

Both people write a locking transaction for the first time at the same
deadline. If A's GPU path is clearly not landing by the halfway mark,
**B stops courses and pairs on it.** The GPU path is the project; courses
are supporting evidence.

### The `resources.status` gate — one line, written on Deadlines 3 and 4

`Resource.status` (AVAILABLE / BLOCKED) already exists on every resource
row. Nothing reads it yet. The check belongs **inside the transaction,
against the row you just locked** — not at the boundary — because it is
mutable state on the resource, so an admin can flip it between a boundary
read and the write:

``` text
LOCK resource row FOR UPDATE
  if status == BLOCKED            -> 409 RESOURCE_BLOCKED
  if allocated + req > gpu_count  -> 409 CAPACITY_EXHAUSTED
```

Both gates read the same locked row, so this costs nothing extra. Adding
it after the Deadline 8 freeze means reopening the flagship transaction, which
is what the freeze exists to prevent — hence writing it now.

**Proposed semantics, still needs ratifying (open item 6 in
DECISIONS.md):** blocking stops *new* allocations and does not evict
existing ones, matching the capacity-reduction rule already agreed in
`ARCHITECTURE_AND_WORKFLOWS.md` §13. Distinct code `RESOURCE_BLOCKED`,
because the remedy differs again: try a different resource — do not wait,
and do not release anything you hold. Note the database enforces none of
this; the GiST constraint is partial on the *reservation's* status, not
the resource's, so the service layer is the whole guarantee here.

**Checkpoint:** 2 GPUs reserve; a 3rd unit returns `QUOTA_EXCEEDED`.
Duplicate registration rejected; overlapping courses rejected. — **All
four verified (session 10):** `scripts/check_gpus.py` 44/44 and
`scripts/check_courses.py` 38/38.

> **Two findings from this deadline change how later ones are written.**
>
> 1. **Benchmark 2 as specified here can pass against the broken build.**
>    Its first run on the deliberately unlocked build reported `held = 2`
>    — a pass. The corruption window is sub-millisecond. Re-run as 25
>    trials it separates cleanly: **24/25 over-quota unlocked, 0/25
>    locked.** Every concurrency assertion from Deadline 5 on must be a
>    count over trials, not a single result.
> 2. **`SELECT ... FOR UPDATE` can return a stale value** through an ORM
>    identity map — lock held, SQL correct, value from before the lock.
>    It cost 20-of-5 seats sold with the counter reading 3. Every locked
>    read now carries `populate_existing()`. Deadline 7's promotion
>    transaction is the next place this can bite.

> **Outstanding item 8 ratified** (session 10): the cancel route mirrors
> the POST route — `DELETE /{resource}/{id}/reservations/{id}` — so one
> id can no longer name rows in two tables.

### Deadline 5 - Idempotency vs. Harness  ◐ A'S COLUMN MET (session 11)

``` text
A ✅   idempotency/ module: IdempotencyKey, UNIQUE(key, user_id)
       NO MIGRATION NEEDED -- the table and its UNIQUE shipped at
       Deadline 1 in 705b757e5df2; `alembic check` reports no drift.
       This deadline is pure application code, which is not what the
       column implied.
A ✅   wire in as step (1) of the GPU transaction
A ✅   CRITICAL: key insert and allocation commit in the SAME transaction
A ✅   replay path returns the stored response and status
A ✅   SAVEPOINT around the claim -- NOT in this column as written, and
       nothing works without it: a unique violation aborts the whole
       transaction, so the read that fetches the stored response cannot
       run. Measured: 86 of 120 concurrent retries return 500 without it.
A ✅   `Idempotency-Key` header is OPTIONAL, settled deliberately --
       Benchmark 3 is the contrast between sending it and not, so
       requiring it would delete one column of its own table.
B      tests/concurrency/harness.py - asyncio + httpx, fires N
       simultaneous requests, collects status codes
       BLOCKER: tests/ does not exist and pytest-asyncio is not in
       requirements.txt. Add it before starting.  <- STILL TRUE at
       session 11; verified, not assumed.
       BENCHMARK 1 (capacity): 500 concurrent registrations, capacity 50
         unlocked build -> record over-allocation
         locked build   -> exactly 50, zero over-allocation
```

**Checkpoint:** first broken-vs-fixed table with real numbers.
— **A's half produced one** (`scripts/check_idempotency.py`, 37/37, three
builds measured; see DECISIONS.md). The checkpoint as written names
Benchmark 1, which is B's, so **the deadline is not met until B's harness
lands.**

> **The A column here did not depend on B at any point**, which is worth
> recording because the plan's phrasing implied a shared deliverable.
> `check_idempotency.py` uses threads exactly as `check_gpus.py` does, so
> the exactly-once evidence exists without the asyncio harness.
>
> **Still owed, and now blocking B rather than A:** `session.py` sets no
> `pool_size`, so SQLAlchemy defaults to 5 + 10 overflow = 15 against a
> 500-request harness. Carried since session 5. It is a shared file and
> needs both people.

### Deadline 6 - Quota Rollout, Benchmarks 2-3, SWAP

``` text
A      apply the quota helper inside B's modules:
         room quota  (concurrent active reservations per user)
         course-load quota (active enrollments per user)
       admin quota endpoints: GET/PUT /admin/quotas/{role}/{resource}
       admin resource-status endpoints (same shape, same ADMIN gate):
         PATCH /rooms/{id}   -- block a room for maintenance
         PATCH /gpus/{id}    -- change capacity / status
         these were in ARCHITECTURE_AND_WORKFLOWS.md Workflow E but had
         no deadline assigned. They only WRITE resources.status; gates on
         Deadlines 3 and 4 are what READ it.
       GET /me/quota -- limits + current usage. Same missing-deadline gap.
         Here rather than Deadline 4, because it reads the quota policy
         AND the held-units SUM, so it is the quotas/ helper with an
         HTTP wrapper -- and Workflow B opens with this call, so the
         flagship demo has been starting on an endpoint nobody owned.
         Read-only: it must NOT take the user lock. A stale number shown
         to a caller is fine; the allocation transaction is what has to
         be right.
       fix whatever B's benchmarks break
B      BENCHMARK 2 (quota): one student, 2 concurrent 2-GPU requests on
         DIFFERENT clusters
         resource lock only -> both succeed, held = 4
         + user-row lock    -> exactly one succeeds
       BENCHMARK 3 (exactly-once): identical request twice, same key
         no key -> 2 reservations;  key -> 1 reservation, identical response

BOTH (1 hour, before closing the deadline) - SWAP REVIEW
       A walks B line-by-line through the GPU transaction
       B walks A line-by-line through the exclusion constraint + harness
```

Benchmark 2 is your strongest artifact: the fix is not "add a lock" -
the resource lock was already correct. It was the **wrong lock for that
invariant**. Both of you must be able to say this unprompted.

### Deadline 7 - Waitlist (a fourth concurrency problem)

``` text
A      promotion transaction. The race: two students drop the same
       offering simultaneously, and both promote the SAME waitlist entry.
       LOCK the offering row -> read oldest entry
       (ORDER BY created_at, id -- no position column; the id tiebreak
        is load-bearing, entries written in one transaction share a
        created_at)
       -> check that student's course-load quota
       -> if it would breach, skip to the next eligible entry
       -> promote exactly one: DELETE its waitlist row, no renumbering
B      waitlist endpoints: join on full course, leave, GET waitlist
       (position is a display value: ROW_NUMBER() OVER (ORDER BY
        created_at, id) at read time, never stored)
       BENCHMARK 4 (waitlist): 2 concurrent drops on a course with 3
         waitlisted students
         no offering lock -> same entry promoted twice / seat lost
         with lock        -> exactly 2 distinct promotions, order preserved
```

**Checkpoint:** promotion follows FIFO, respects quota, and never
double-promotes under concurrent drops.

### Deadline 8 - FEATURE FREEZE

``` text
BOTH   no new features from here. None.
       integration pass: every endpoint returns the agreed codes
       distinct machine-readable codes for CAPACITY_EXHAUSTED vs
       QUOTA_EXCEEDED (different caller remedy)
       fix all bugs surfaced by the four benchmarks
       re-run all four, record final numbers
B      enrolled_count reconciliation query after Benchmark 1 -- prove the
       counter and the enrollments table agree (see DECISIONS.md)
```

If you are behind here, apply the Section 0.1 cut order. Do not extend
the freeze.

### Deadline 9 - Docs & Clean-Room Test

``` text
A      README: architecture, three serialization points, lock ordering,
       canonical transaction with BEGIN/COMMIT boundaries correct
B      README: workflows, role matrix, four benchmark tables with real
       measured numbers
BOTH   CLEAN-ROOM TEST: fresh clone, fresh volumes, docker compose up,
       seed, run all four benchmarks
       If it fails on a clean machine, it fails in a demo.
```

**Checkpoint:** a stranger could clone the repo and reproduce your
numbers from the README alone.

### Deadline 10 - Cross-Presentation & Demo

``` text
BOTH   A presents B's modules (rooms, courses, waitlist, harness) as if
       in an interview. B corrects and fills gaps.
       Then B presents A's modules (auth, quota, idempotency, GPU
       transaction, promotion). A corrects.

       Each writes their OWN answer to:
       - Why is the quota lock on the user, not the resource?
       - Why must the idempotency key commit in the same transaction?
       - Why does fixed lock ordering prevent deadlock?
       - What did you deliberately NOT build, and why?

       Demo rehearsal: 5 minutes, ending on Benchmark 2.
       Final README pass. LaTeX resume bullet.
```

------------------------------------------------------------------------

## 3. Session Ritual (15 minutes, non-negotiable)

Per working session, not per calendar day — a deadline may take several
sessions, and this runs at the edges of each one.

``` text
SESSION START (5 min)   What am I touching? Any shared file?
                        Any schema change? (-> A generates the migration)
                        Which Deadline does this session advance?

SESSION END (10 min)    Both push. Both pull. Both run the other's tests.
                        Log to DECISIONS.md: decisions, failures + fixes,
                        benchmark numbers.
                        Log to WORK_LOG.md, and say plainly whether the
                        Deadline is now MET or still open. A session is
                        not a deadline.
```

`DECISIONS.md` is your interview cheat-sheet. "We tried X, measured Y,
so we chose Z" is the most credible thing you can say about a project,
and it is impossible to reconstruct two months later.

------------------------------------------------------------------------

## 4. Risk Table

``` text
Risk                              Mitigation
--------------------------------------------------------------------------
No buffer deadline                Pre-agreed cut order (Section 0.1),
                                  applied at the Deadline 8 checkpoint
Alembic revision conflict         A owns migrations exclusively
Models diverge                    Written jointly Deadline 1, frozen after
B blocked on auth                 Stub dependency, signature agreed Deadline 1
Deadline 4 spills (most likely)        B abandons courses and pairs on GPU
Deadline 7 waitlist spills             Cut waitlist whole; it is item 5 on the
                                  cut list and the largest single item
Neither can explain the other's   Deadline 6 swap review + Deadline 10 cross-present
  code
Async/SQLAlchemy learning curve   Use SYNC SQLAlchemy + psycopg
Scope creep after Deadline 8           Freeze is absolute
```

**On sync vs. async:** pick sync. `SELECT ... FOR UPDATE` semantics are
identical, the locking story is unchanged, and every error message you
will search for assumes sync. The only place you need real concurrency
is the *test harness*, which is asyncio + httpx on the client side and
unaffected by a sync server. Async would double the debugging surface to
buy throughput you are not measuring.

------------------------------------------------------------------------

## 5. Definition of Done

``` text
docker compose up on a clean machine -> working API
Student token gets 403 on admin endpoints
500 concurrent registrations -> exactly 50 enrolled, 0 over-allocation
Concurrent cross-cluster GPU requests -> quota never exceeded
Retried request -> one reservation, identical response
Concurrent drops -> no double-promotion, FIFO preserved
Four broken-vs-fixed tables with real measured numbers in the README
BOTH people can explain every file without the other present
```

The last line is the one that gets tested in an interview.
