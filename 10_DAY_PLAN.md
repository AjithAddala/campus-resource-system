# 10-Day Two-Person Execution Plan — Full Scope

## 0. Scope

Nothing is cut. Capacity check: 2 people x 10 days = **20 person-days**,
against the original solo 15-day plan's 15, plus roughly 3 person-days
for the added features. It fits — with no slack.

``` text
IN SCOPE
  JWT auth + RBAC (require_role dependency)
  GPU:     capacity + quota + idempotency      <- flagship transaction
  Rooms:   exclusion-constraint intervals + room quota
  Courses: capacity + duplicate + schedule overlap + course-load quota
  Waitlist: FIFO, quota-aware promotion, concurrency-safe
  Four broken-vs-fixed benchmarks
  asyncio concurrency harness + Locust load scenarios
  Docker Compose one-command startup

DELIBERATELY EXCLUDED (design decision, not a scope cut)
  Denormalized quota counter
```

**Why the counter stays out.** The MVP recomputes held units by `SUM`
under the user lock, which cannot drift. A counter is an optimization,
and your stated principle is that complexity is earned by measurement.
Building it with no benchmark showing user-row contention invites the
question "what did it improve?" with no answer. Leaving it out, and
saying *why*, is the stronger position. Revisit only if Locust shows the
user row is the bottleneck.

## 0.1 Pre-Agreed Cut Order

Full scope means Day 10 is no longer a buffer. Slippage has nowhere to
go, so decide the cut order **now**, while calm — not on Day 8 under
pressure. If you are behind at any checkpoint, drop strictly in this
order:

``` text
1. Locust                    (asyncio harness already proves correctness)
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
admin quota endpoints                all four benchmarks + Locust
                                     README assembly
```

Note the deliberate crossovers: A applies the quota helper inside B's
room and course modules (Day 6), and A owns the waitlist *promotion
transaction* while B owns the waitlist *endpoints*. Neither person can
finish without reading the other's code.

### Shared-file protocol

``` text
models/     -> joint Day 1 session ONLY. After that, changes require both
               people present. No solo edits.
alembic/    -> PERSON A OWNS EXCLUSIVELY. B never runs `alembic revision`.
```

Two people independently generating Alembic revisions creates divergent
`down_revision` chains that cost half a day to untangle. With no buffer,
that half-day is fatal. If B needs a schema change, B asks A.

### Interface-first unblocking

B is blocked on auth from Day 2. On **Day 1** jointly fix the signatures:

``` python
def get_current_user(...) -> User
def require_role(*allowed: Role) -> Callable
```

B codes against a hardcoded stub returning a fake `User`. A implements
the real one. Swap on Day 3 - same import path, no refactor.

------------------------------------------------------------------------

## 2. Day-by-Day

### Day 1 - Foundation (JOINT, do not parallelize)

``` text
BOTH   repo, .env, docker-compose (Postgres + app), FastAPI skeleton
BOTH   write ALL models together in one sitting - including RoleQuota,
       IdempotencyKey, WaitlistEntry, CourseOffering
BOTH   first Alembic migration; verify on a clean DB on BOTH machines
BOTH   agree get_current_user / require_role signatures
BOTH   agree error codes: 401 / 403 / 409 CAPACITY_EXHAUSTED /
       409 QUOTA_EXCEEDED / 422 IDEMPOTENCY_KEY_REUSED
```

Divergence here poisons all nine remaining days. This is the one day
where pairing is faster than splitting.

**Checkpoint:** `docker compose up` works, `/docs` loads, migration
applies cleanly for both.

### Day 2 - Auth vs. Read Paths

``` text
A      Argon2 hashing, JWT encode/decode, role as a token claim
       POST /auth/register, POST /auth/login
B      seed script: 3 users (one per role), 2 GPU clusters, 2 rooms,
       1 course + offering
       GET /gpus, /rooms, /courses, /{id}/availability (against the stub)
```

**Checkpoint:** login returns a token containing a role; B can list
resources.

### Day 3 - Authorization vs. Rooms

``` text
A      require_role dependency; apply to all admin-only routes
       verify 403 fires BEFORE the handler body executes
       hand the real dependency to B, delete the stub
B      room reservation POST
       EXCLUDE USING gist constraint (A generates the migration)
       adjacent-interval test: [10,12) and [12,14) both succeed
```

**Checkpoint:** student token on `POST /gpus` returns 403; overlapping
room booking returns 409.

### Day 4 - Flagship vs. Courses (heaviest day)

``` text
A      quotas/ module: RoleQuota table + seeded defaults
       GPU transaction: LOCK user -> SUM held -> quota check
                        LOCK cluster -> capacity check -> write
       lock order recorded in a code comment
B      course registration: LOCK offering -> capacity -> insert
       UNIQUE(student_id, course_offering_id)
       schedule-overlap check against existing enrollments
       DELETE /reservations/{id} with owner-or-admin check
```

Both people write a locking transaction for the first time on the same
day. If by midday A's GPU path is clearly not landing, **B stops courses
and pairs on it.** The GPU path is the project; courses are supporting
evidence.

**Checkpoint:** 2 GPUs reserve; a 3rd unit returns `QUOTA_EXCEEDED`.
Duplicate registration rejected; overlapping courses rejected.

### Day 5 - Idempotency vs. Harness

``` text
A      idempotency/ module: IdempotencyKey, UNIQUE(key, user_id)
       wire in as step (1) of the GPU transaction
       CRITICAL: key insert and allocation commit in the SAME transaction
       replay path returns the stored response and status
B      tests/concurrency/harness.py - asyncio + httpx, fires N
       simultaneous requests, collects status codes
       BENCHMARK 1 (capacity): 500 concurrent registrations, capacity 50
         unlocked build -> record over-allocation
         locked build   -> exactly 50, zero over-allocation
```

**Checkpoint:** first broken-vs-fixed table with real numbers.

### Day 6 - Quota Rollout, Benchmarks 2-3, SWAP

``` text
A      apply the quota helper inside B's modules:
         room quota  (concurrent active reservations per user)
         course-load quota (active enrollments per user)
       admin quota endpoints: GET/PUT /admin/quotas/{role}/{resource}
       fix whatever B's benchmarks break
B      BENCHMARK 2 (quota): one student, 2 concurrent 2-GPU requests on
         DIFFERENT clusters
         resource lock only -> both succeed, held = 4
         + user-row lock    -> exactly one succeeds
       BENCHMARK 3 (exactly-once): identical request twice, same key
         no key -> 2 reservations;  key -> 1 reservation, identical response

BOTH (evening, 1 hour) - SWAP REVIEW
       A walks B line-by-line through the GPU transaction
       B walks A line-by-line through the exclusion constraint + harness
```

Benchmark 2 is your strongest artifact: the fix is not "add a lock" -
the resource lock was already correct. It was the **wrong lock for that
invariant**. Both of you must be able to say this unprompted.

### Day 7 - Waitlist (a fourth concurrency problem)

``` text
A      promotion transaction. The race: two students drop the same
       offering simultaneously, and both promote the SAME waitlist entry.
       LOCK the offering row -> read lowest-position ACTIVE entry
       -> check that student's course-load quota
       -> if it would breach, skip to the next eligible entry
       -> promote exactly one, renumber positions
B      waitlist endpoints: join on full course, leave, GET waitlist
       FIFO position assignment
       BENCHMARK 4 (waitlist): 2 concurrent drops on a course with 3
         waitlisted students
         no offering lock -> same entry promoted twice / seat lost
         with lock        -> exactly 2 distinct promotions, order preserved
```

**Checkpoint:** promotion follows FIFO, respects quota, and never
double-promotes under concurrent drops.

### Day 8 - FEATURE FREEZE

``` text
BOTH   no new features from here. None.
       integration pass: every endpoint returns the agreed codes
       distinct machine-readable codes for CAPACITY_EXHAUSTED vs
       QUOTA_EXCEEDED (different caller remedy)
       fix all bugs surfaced by the four benchmarks
       re-run all four, record final numbers
B      Locust scenarios if and only if the above is done
```

If you are behind here, apply the Section 0.1 cut order. Do not extend
the freeze.

### Day 9 - Docs & Clean-Room Test

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

### Day 10 - Cross-Presentation & Demo

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

## 3. Daily Ritual (15 minutes, non-negotiable)

``` text
MORNING (5 min)   What am I touching today? Any shared file?
                  Any schema change? (-> A generates the migration)

EVENING (10 min)  Both push. Both pull. Both run the other's tests.
                  Log to DECISIONS.md: decisions, failures + fixes,
                  benchmark numbers.
```

`DECISIONS.md` is your interview cheat-sheet. "We tried X, measured Y,
so we chose Z" is the most credible thing you can say about a project,
and it is impossible to reconstruct two months later.

------------------------------------------------------------------------

## 4. Risk Table

``` text
Risk                              Mitigation
--------------------------------------------------------------------------
No buffer day                     Pre-agreed cut order (Section 0.1),
                                  applied at the Day 8 checkpoint
Alembic revision conflict         A owns migrations exclusively
Models diverge                    Written jointly Day 1, frozen after
B blocked on auth                 Stub dependency, signature agreed Day 1
Day 4 spills (most likely)        B abandons courses and pairs on GPU
Day 7 waitlist spills             Cut waitlist whole; it is item 5 on the
                                  cut list and the largest single item
Neither can explain the other's   Day 6 swap review + Day 10 cross-present
  code
Async/SQLAlchemy learning curve   Use SYNC SQLAlchemy + psycopg
Scope creep after Day 8           Freeze is absolute
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
