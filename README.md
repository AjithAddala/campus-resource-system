# Campus Resource Allocation System

A concurrency-safe backend for allocating scarce campus resources — GPU
capacity, rooms, and course seats — built around one question:

> How does a backend allocate limited resources correctly when many users
> request them at the same instant, without overbooking, breaking
> per-user entitlements, or double-booking a retried request?

FastAPI · PostgreSQL 16 · SQLAlchemy 2.0 (sync) · Alembic · Docker Compose

---

## The claim

There is not one thing to guard. There are **three**, each keyed on
something different — so guarding one does nothing for the others.

| Rule | Is a fact about | Serialize on |
|---|---|---|
| A cluster holds at most N GPUs | the **resource** | the cluster row (`FOR UPDATE`) |
| A student may hold at most 2 GPUs | the **user** | the user row (`FOR UPDATE`) |
| A retry must not book twice | the **request** | `UNIQUE(key, user_id)` |

One student, quota 2, fires two concurrent 2-GPU requests at **different**
clusters. Different rows, so the cluster locks never contend. Each cluster
has room. Every capacity check passes honestly. Both commit, and the
student holds 4.

Nothing was overbooked. Both cluster locks worked perfectly.

> **The lock was not missing. It was the wrong lock for that invariant.**

---

## What we actually measured

Every benchmark here was built **broken first**, run, and only then
fixed — and the broken builds are still reachable behind two documented
flags, so both columns of every table can be reproduced from a fresh
clone.

That discipline is the point, because **three of our four benchmarks
contradicted the intuition they were built on**:

| Benchmark | What we predicted would break | What actually broke |
|---|---|---|
| 2 — quota | a missing lock | the lock was present and *correct*; it was the **wrong lock** for that invariant |
| 3 — exactly-once | retries would double-allocate | **no build over-allocated** — `UNIQUE` held the row count. The fix bought the *reply*, not the uniqueness |
| 4 — waitlist | the same entry promoted twice | **no build double-promoted** — `SKIP LOCKED` already prevented it. The offering lock protects the **counter** |
| 1 — capacity | seats oversold without the row lock | confirmed, and then some: **all 500** students seated in a 50-seat section, in every trial |

A table of four predictions that all came true would be a weaker
artifact, not a stronger one.

---

## Quickstart

```bash
cp .env.example .env          # then set JWT_SECRET to something of your own
docker compose up -d --build
docker compose exec app alembic upgrade head
docker compose exec app python scripts/seed.py
```

API and Swagger UI: <http://localhost:8000/docs> — click **Authorize** and
log in as any seeded account (login is the OAuth2 password flow, so the
button works without pasting headers).

| Account | Role | Password |
|---|---|---|
| `student@iitk.ac.in` | STUDENT | `campus123` |
| `faculty@iitk.ac.in` | FACULTY | `campus123` |
| `admin@iitk.ac.in` | ADMIN | `campus123` |

Health checks live at the **root**, outside the `/api/v1` prefix, because
they are infrastructure and shouldn't track an API version: `/health` and
`/health/db`.

> `/health/db` proves only that Postgres answers `SELECT 1`. It says
> nothing about whether the schema exists — `alembic current` is the check
> for that.

---

## Verifying it yourself

Nine gate scripts, each exiting non-zero on the first failure:

```bash
docker compose exec app python scripts/check_jwt.py
docker compose exec app python scripts/check_auth.py
docker compose exec app python scripts/check_rbac.py
docker compose exec app python scripts/check_rooms.py
docker compose exec app python scripts/check_gpus.py
docker compose exec app python scripts/check_courses.py
docker compose exec app python scripts/check_idempotency.py
docker compose exec app python scripts/check_quotas.py
docker compose exec app python scripts/check_waitlist.py
```

Each leaves the database at its post-seed state. The harness itself is
tested too — `docker compose exec app python -m pytest tests/ -v` asserts
that "concurrent" requests genuinely overlap, because a harness that
quietly serialized would make all four tables below look plausible and
prove nothing. It must report **6 passed**; if it reports 6 *skipped*,
`pytest-asyncio` is missing from the image and the suite is proving
nothing — rebuild rather than believe it.

Everything on this page — the quickstart, the nine gates, the harness
suite, and both columns of all four benchmark tables — was run end to end
against fresh volumes, and then **re-run from a real `git clone` at
Deadline 10** with nothing cached, nothing pre-migrated and no arguments
remembered. Nineteen of the twenty published numbers reproduced exactly;
the twentieth is flagged where it appears, in Benchmark 1's broken
column. See `CROSS_PRESENTATION.md` §6.2 for that run.

---

## The four benchmarks

All four run against the stack described above. The **broken** column of
each is selected by a flag in `.env`, then:

```bash
docker compose up -d --force-recreate app
```

| Flag | Drops | Used by |
|---|---|---|
| `BENCHMARK_UNSAFE_NO_USER_LOCK=true` | the user-row lock in `reserve_gpu` | Benchmark 2 |
| `BENCHMARK_UNSAFE_NO_OFFERING_LOCK=true` | the offering-row lock in `register` / `drop` | Benchmarks 1, 4 |

Both default to **false**. Each removes exactly one lock and changes no
other logic — in particular `populate_existing()` stays, so the broken
builds read a **fresh** value and still corrupt. The bug is never a stale
read; it is a correct read of a number nothing was holding still.

**Every number below — both columns — was measured in a clean-room
session**: fresh volumes, `docker compose up -d --build`, migrate, seed,
then each benchmark at the exact command printed beside it and with no
arguments. The broken columns were re-measured by setting the flag above
and changing nothing else. Zero `5xx` or transport errors in any
fixed-build run. **All of it was then re-run from a real `git clone`** at
Deadline 10, which reproduced every number below except the one noted in
Benchmark 1's broken column.

Because the commands take no arguments, the trial counts here are the
benchmarks' own defaults — 5, 25, 15 and 15 — and a stranger running them
gets the same denominators.

> **Why every benchmark counts trials instead of asserting once.** The
> first ever run of Benchmark 2 against the unlocked build *passed* —
> against the build it exists to indict. The corruption window is well
> under a millisecond, and two requests released on a barrier do not
> reliably land inside it. One run is a coin flip; a benchmark that
> reports one number cannot separate two builds.

### Benchmark 1 — capacity

```bash
docker compose exec app python -m tests.concurrency.benchmark_1_capacity
```

500 students register simultaneously for a 50-seat offering, 5 trials.

| build | oversold trials | counter mismatches | seats sold |
|---|---|---|---|
| no offering lock | **5 / 5** | 5 / 5 | **500** of 50, every trial |
| + `SELECT … FOR UPDATE` | **0 / 5** | 0 / 5 | exactly **50**, every trial |

The fixed column is a live run at the command above — 50 × `201` and
450 × `409 CAPACITY_EXHAUSTED` in every trial, `enrolled_count` equal to
the ACTIVE row count in every trial, peak DB concurrency **39** of the 40
in flight. The broken column is that same command against the unlocked
build, and it is worse than the Deadline 5 measurement it replaces:
**all five trials seated all 500 students in a 50-seat section** — 500 ×
`201`, not a single `CAPACITY_EXHAUSTED` — while `enrolled_count`
recorded a low double-digit number of them: **14 to 21** in the
clean-room run, **15 to 26** when the same command was re-run from a
fresh clone at Deadline 10. That spread is the one number on this page
that did not reproduce as written, and the correction is the same rule
twice over: a range needs more than one run before it is published as a
range. The 5/5 oversold and the 500-of-50 are stable across both runs;
only the counter's landing point moves.

The counter is not just wrong, it is wrong in the *reassuring*
direction: a 50-seat section calmly reporting fifteen or twenty seats
filled while holding 500 enrollments. Lost updates, every transaction
incrementing the same stale number.

Note what the broken build is *not* doing: `populate_existing()` is still
there, so it reads a **fresh** `enrolled_count`. Adding that keyword does
not fix this. Only the lock does.

### Benchmark 2 — quota (the argument this project is built on)

```bash
docker compose exec app python -m tests.concurrency.benchmark_2_quota
```

One student, quota 2, two 2-unit requests fired at two **different**
clusters, 25 trials.

| build | over-quota trials | held units observed |
|---|---|---|
| resource lock only | **25 / 25** | student ends up holding 4 |
| + user-row lock | **0 / 25** | `{2: 25}` |

Point both racers at the *same* cluster and this benchmark measures
nothing: the cluster lock serializes them, the second re-reads held
units, and the broken build passes. The clusters must differ, because
that is precisely the case no resource lock can see.

### Benchmark 3 — exactly-once

```bash
docker compose exec app python -m tests.concurrency.benchmark_3_exactly_once
```

The same request fired 8 times simultaneously, with and without an
`Idempotency-Key`, 15 trials. Racers are FACULTY (GPU quota 10) so the
unkeyed column is never truncated by the quota gate — otherwise the
contrast would be measuring the wrong invariant.

| column | reservations per trial | key rows | responses |
|---|---|---|---|
| no key | `{8: 15}` — one hold per retry | — | — |
| with key | `{1: 15}` | 1 | `{201: 120}`, divergent bodies **0/15** |

Both columns are honest behaviour, not a broken build and a fixed one —
which is why the header is **optional**. Making it mandatory would delete
one column of the table it exists to produce.

**No build over-allocated.** `UNIQUE(key, user_id)` held the row count to
one in every implementation we tried. What the correct one buys is that
the retry gets *the original response*: without the SAVEPOINT the unique
violation aborts the transaction and 86 of 120 retries become a `500`;
with the key committed separately, 98 of 120 become a spurious `409`.

### Benchmark 4 — waitlist promotion

```bash
docker compose exec app python -m tests.concurrency.benchmark_4_waitlist
```

Students drop a full offering simultaneously while others are queued;
each drop frees a seat and promotes one queued student **in the same
transaction**, 15 trials.

| scenario | build | promotions | `(enrolled_count, ACTIVE rows)` |
|---|---|---|---|
| 2 droppers, 3 queued *(as specified in the plan)* | no offering lock | `{2: 15}` | `{(2,2): 15}` — **passes** |
| 2 droppers, 3 queued | + offering lock | `{2: 15}` | `{(2,2): 15}` |
| 8 droppers, 3 queued | no offering lock | `{3: 15}` | `{(7,3): 15}` — **15/15 wrong** |
| 8 droppers, 3 queued | + offering lock | `{3: 15}` | `{(3,3): 15}` |

**The scenario our own plan specified passes against the broken build**,
15/15, for two reasons worth knowing:

1. `SKIP LOCKED` already prevents the double promotion. Two drops racing
   on one queue both read the same oldest entry, but the first to lock
   that candidate's user row keeps it and the second is *skipped* onto
   the next. The mechanism introduced to avoid a **deadlock** also
   prevents this **double-write**, by accident.
2. With one promotion per drop, the counter arithmetic nets to zero
   (`enrolled_count - 1 + 1`), so a lost update writes back the number it
   would have written anyway.

Make the drops outnumber the queue and the builds separate cleanly:
**seven seats recorded as taken against three real enrollments**, and
`offering_enrollment_sane` doesn't catch it because 7 ≤ 8. Both scenarios
ship and both run by default — *"the specified test passes on the broken
build"* is a result, not a nuisance to be re-specified away.

**Column 3** runs once rather than over trials, because holding a lock is
not a race: a candidate's user row is held `FOR UPDATE` from a second
session for 5 seconds, and the drop **returns in a tenth of a second or
less** — 0.02s to 0.10s across our runs, against a hold fifty times
longer than that — skipping the held candidate
and promoting the next eligible one. *"Promotion never waits"* is a claim
about time, and this is the only assertion in the project that measures
it.

### On "500 concurrent"

It is **500 submitted, 40 in flight**, and the distinction is not
pedantry. A connection is checked out during dependency resolution
(`get_current_user` reads the user row) and held by the Session until the
request ends — so in-flight requests *are* connections, and Postgres
allows 100. Firing 500 unbounded returns 440 of them as `500`, with the
whole pool sitting `idle in transaction`. Serving 500 truly at once would
need a Postgres sized for 500 backends.

Every benchmark therefore samples `pg_stat_activity` during the run and
**reports the concurrency it achieved** next to the number it asked for.
If those differ by an order of magnitude, the run measured a connection
pool and not a lock.

---

## Role capability matrix

| Action | STUDENT | FACULTY | ADMIN |
|---|:---:|:---:|:---:|
| Register / login | ✓ | ✓ | ✓ |
| View resources & availability | ✓ | ✓ | ✓ |
| View own quota & usage | ✓ | ✓ | ✓ |
| Reserve a room | ✓ | ✓ | ✓ |
| Reserve GPU capacity | ✓ | ✓ | ✓ |
| Cancel own reservation | ✓ | ✓ | ✓ |
| Register for a course | ✓ | ✗ | ✗ |
| Join / leave a course waitlist | ✓ | ✗ | ✗ |
| View a course waitlist | ✓ | ✓ | ✓ |
| Create a resource | ✗ | ✗ | ✓ |
| Modify resource availability/status | ✗ | ✗ | ✓ |
| Configure per-role quotas | ✗ | ✗ | ✓ |
| Cancel ANY user's reservation | ✗ | ✗ | ✓ |

### Default quotas (seeded, admin-editable)

| Resource | STUDENT | FACULTY | ADMIN |
|---|---|---|---|
| GPU | 2 | 10 | unlimited |
| ROOM | 2 | 5 | unlimited |
| COURSE | 6 | — | unlimited |

`(FACULTY, COURSE)` is **deliberately absent**, not set to null. Course
registration is STUDENT-only, so the pair is unreachable behind the 403 —
which keeps three states distinguishable:

```
max_units = 5      at most five
max_units = 0      none at all
max_units = null   unlimited          <- a policy that says yes
(row absent)       no policy at all   -> 409 QUOTA_NOT_CONFIGURED
```

A missing row **fails closed**. Treating it as unlimited would be the
dangerous default: one un-seeded row would silently switch off the
invariant this project exists to enforce, and every test would still pass.

---

## Workflows

### A — Onboarding

```
POST /api/v1/auth/register    JSON: name, email, password, role
                              password -> Argon2id
                              duplicate email -> 409, from UNIQUE, not a
                                pre-flight SELECT
POST /api/v1/auth/login       FORM-ENCODED: username, password
                              -> JWT { sub: "1", role, iat, exp }
```

Two interface facts that are easy to get wrong and expensive to discover
late:

- **Login takes a form body and the email travels in a field named
  `username`.** It is the OAuth2 password flow, which is what gives
  `/docs` a working *Authorize* button. Registration is JSON; only this
  route is form-encoded.
- **`sub` is a string**, `"1"` and not `1`. PyJWT ≥ 2.10 enforces that on
  *decode*, so an int subject issues a valid-looking token and then 401s
  every later request — a bug in the issuer that only ever surfaces in
  the consumer.

The role travels in the token, but **authorization reads it from the
database row, not the claim**. The lookup the claim was meant to save had
already been spent: `get_current_user` returns a `User`, so the row is
loaded on every authenticated request anyway. Given both copies, the
fresher one wins — a demoted admin loses admin on their next request
rather than at token expiry.

### B — Reserving a GPU (the flagship path)

The only endpoint where all three guarantees meet.

```
POST /api/v1/gpus/{id}/reservations
Headers: Authorization: Bearer <JWT>
         Idempotency-Key: <uuid>        (optional)
Body:    { "gpu_count": 2 }             no times — hold-until-release
```

Three distinct `409`s, distinguishable by code because the remedies
differ:

```
QUOTA_EXCEEDED      you hold too much     -> release something
CAPACITY_EXHAUSTED  the cluster is full   -> wait, or try another
RESOURCE_BLOCKED    an admin blocked it   -> try another, do not wait
```

A retry with the same key returns **the stored response and the stored
status** — `201`, not `200`. A replay must be indistinguishable from the
original call; a client branching on `201` would take a different path on
the retry, which is the exact bug idempotency exists to remove.

A retry of a request that **failed** allocates for real: the key rolled
back with the allocation, so there is nothing to replay. Exactly-once is
a promise about *successes*.

### C — Release and re-acquire

```
DELETE /api/v1/gpus/{gpu_id}/reservations/{id}     owner or ADMIN
DELETE /api/v1/rooms/{room_id}/reservations/{id}
```

The cancel path **mirrors the POST path**. `DELETE /reservations/5` was
ambiguous: room holds live in `reservations` and GPU holds in
`gpu_reservations`, each with its own id sequence, so one id named a row
in both tables.

Cancellation is **naturally idempotent** — it flips a status flag, and
flipping an already-`CANCELLED` row is a no-op. That falls directly out
of recomputing held units by `SUM` instead of keeping a counter; with a
counter, a repeated cancel would double-decrement and need a guard of its
own.

The user lock is taken on the reservation's **owner**, not the caller: an
ADMIN releasing someone else's hold must serialize against *that user's*
allocations.

### D — Registering for a course

```
POST /api/v1/offerings/{id}/register        [STUDENT only]
DELETE /api/v1/offerings/{id}/drop
```

**Keyed on the offering, not the course**, and that is the mechanism
rather than a naming preference: capacity, `enrolled_count` and the
locked row all live on `course_offerings`, and one course has many
offerings — so `/courses/{id}/register` would have no single row to lock.
Read paths stay course-shaped; write paths are offering-shaped.

Four `409`s: `ALREADY_ENROLLED`, `QUOTA_EXCEEDED`, `SCHEDULE_CONFLICT`,
`CAPACITY_EXHAUSTED`.

The transaction locks the **student's user row before the offering row**,
and the schedule check is why — it reads the student's *other*
enrollments, and two concurrent registrations for two clashing offerings
share no offering row, so nothing would serialize them and both would
pass. A schedule clash is a fact about the student, exactly like a quota.

### E — Admin manages resources and policy

```
POST  /api/v1/gpus                      create a cluster
PATCH /api/v1/gpus/{id}                 capacity / status
PATCH /api/v1/rooms/{id}                block for maintenance
PUT   /api/v1/admin/quotas/{role}/{resource}    { "max_units": 3 }
```

Any non-admin token is rejected with `403` **before the handler body
runs** — `require_role` is a dependency, not an `if` at the top of the
function, so a rejected call cannot leave partial state behind. The gates
assert that by counting rows after the 403, not by trusting the status
code.

**Nothing is ever evicted.** Blocking a resource stops *new* allocations
and leaves existing holds alone; lowering a role quota below what a user
already holds doesn't evict them either — they simply cannot acquire more
until they drop below the new limit.

> One documented exception to the letter of that rule: shrinking a
> cluster's `gpu_count` below `allocated` is **refused** with
> `409 CAPACITY_BELOW_ALLOCATED` rather than accepted. The
> `gpu_capacity_sane` CHECK makes the alternative state impossible to
> store, and the CHECK is what makes a locking bug in the flagship
> transaction fail loudly instead of quietly overselling a cluster. The
> intent — never evict — is preserved exactly; an admin shrinks to
> `allocated` now and further as holds are released.

### F — Queueing, and being promoted

```
POST   /api/v1/offerings/{id}/waitlist    join a FULL offering  [STUDENT]
DELETE /api/v1/offerings/{id}/waitlist    leave
GET    /api/v1/offerings/{id}/waitlist    the queue, any role
```

**Joining is explicit.** There is deliberately no fall-through from a
full `POST /register`, which would make one `201` mean either "you have a
seat" or "you are queued" — the same defect the replay-status decision
refused. A full offering returns `409 CAPACITY_EXHAUSTED` and writes
nothing.

**Position is computed, never stored.** There is no `position` column;
it is `ROW_NUMBER() OVER (ORDER BY created_at, id)` evaluated at read
time. That is what makes a promotion touch exactly **one** row — the one
it deletes. A stored position would have to be renumbered for everyone
behind the promoted student, and renumbering transiently violates a
unique constraint mid-`UPDATE`.

> **The `id` tiebreak is the whole guarantee, not a formality.**
> `func.now()` is *transaction start time*, so entries written inside one
> transaction share a `created_at` to the microsecond and `created_at`
> alone cannot express FIFO between them. `check_waitlist.py` Part 1
> proves that against the live database rather than asserting it in prose.

**Queueing costs no course-load quota** — a queued student holds nothing,
so a student at their cap of 6 may still queue. The quota is enforced at
*promotion* time, where a candidate who would breach it is skipped.

Promotion runs **inside** the drop transaction, holding the locks the
drop already took, so the seat moves from one student to another
atomically. There is no instant at which the freed seat is visible to an
ordinary registration — a queued student cannot lose their place to
someone refreshing the page.

The promise is stated honestly: **oldest *eligible*, not oldest.** A
candidate is skipped if their user row is busy, if their course load is
full, or if the offering clashes with one they already hold. Every skip
is logged, because a passed-over student changes no row at all — without
the log line they would lose their turn with no record anywhere.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  CLIENT — Swagger UI · httpx · the asyncio harness        │
│  Authorization: Bearer <JWT>                              │
│  Idempotency-Key: <uuid>          (mutations, optional)   │
└────────────────────────┬─────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────┐
│  API — FastAPI routers                                    │
│  Dependency chain, resolved in order, before the body:    │
│    get_current_user()  → authentication  401              │
│    require_role(...)   → authorization   403              │
│    Pydantic schema     → validation      422              │
│  Nothing here touches business state.                     │
└────────────────────────┬─────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────┐
│  SERVICE — domain logic, and the transaction boundary     │
│    idempotency → quota → capacity → write → COMMIT        │
│  All three guarantees are enforced here, in ONE tx.       │
└────────────────────────┬─────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────┐
│  PostgreSQL — the source of truth                         │
│  Row locks · unique · exclusion · check constraints       │
└──────────────────────────────────────────────────────────┘
```

**The layer rule:** authorization is a *boundary* concern; capacity,
quota and exactly-once are *correctness invariants*. A 403 can be decided
from identity alone — so it lives in a dependency and fires before the
handler body runs, meaning a rejected call cannot leave partial state
behind. An invariant can only be decided **under a lock**, so it lives
inside the transaction and never at the boundary.

That rule is why `core/security.py` raises no HTTP errors at all, and why
`quotas/` and `idempotency/` know nothing about status codes: a service
that knows about HTTP is a service that can only be called from HTTP.

```
app/
├── main.py            routers mounted under /api/v1; health at the root
├── core/
│   ├── security.py    JWT encode/decode, Argon2 — no DB, no FastAPI
│   ├── dependencies.py get_current_user (401), require_role (403)
│   ├── errors.py      the coded-error envelope
│   └── config.py      settings, API_PREFIX, the two benchmark flags
├── auth/              register, login
├── users/             GET /me, GET /me/quota
├── quotas/            RoleQuota policy, enforcement helper, admin endpoints
├── idempotency/       claim / record_response
├── rooms/             interval reservations + cancel
├── gpus/              the flagship transaction + cancel
├── courses/           registration, drop, waitlist, promotion
├── models/            SQLAlchemy ORM
└── database/          session (pool sizing), base
```

There is deliberately **no `reservations/` module**. Cancel routes mirror
their POST routes (`DELETE /gpus/{id}/reservations/{rid}`), because
`DELETE /reservations/5` named a row in two tables with independent id
sequences. Once the routes are resource-scoped, a shared module has
nothing left to share.

`quotas/` and `idempotency/` are **helpers called from inside another
module's transaction**. They take a `Session` they did not create, emit
no `COMMIT`, return plain values and raise plain exceptions. That is what
makes the guarantees atomic: the gate and the write it guards commit or
roll back together.

### Why PostgreSQL

Because the invariants are expressible *to the database*, and the ones
that aren't are exactly the interesting ones:

| Invariant | Enforced by |
|---|---|
| `allocated <= gpu_count` | `CHECK (allocated >= 0 AND allocated <= gpu_count)` |
| `enrolled_count <= capacity` | `CHECK`, same shape |
| no duplicate enrollment | `UNIQUE (student_id, course_offering_id)` |
| no overlapping room reservations | `EXCLUDE USING gist (resource_id WITH =, tstzrange(start,end,'[)') WITH &&) WHERE (status='ACTIVE')` |
| one retry books at most once | `UNIQUE (key, user_id)` |
| **a user holds at most N units** | **nothing — no constraint can express it** |

The last row is the project. A quota is a predicate over *rows the
transaction is about to write*, scoped to a user, spanning resources —
there is no constraint shape for it, so a row lock is the only mechanism
available. The room constraint is the deliberate contrast: it is the one
invariant where Postgres does the concurrency work entirely on its own
and no application lock participates.

The constraints are **defense in depth, not the mechanism**. The lock is
what makes the code correct; the `CHECK` is what makes a locking bug fail
loudly instead of quietly overselling a cluster.

## The three serialization points

| Guarantee | Keyed on | Mechanism | Failure if absent |
|---|---|---|---|
| Exactly-once | **request** | `UNIQUE(key, user_id)` insert | a retry books twice |
| Quota | **user** | `SELECT users … FOR UPDATE` | a user exceeds their cap |
| Capacity | **resource** | `SELECT cluster … FOR UPDATE` | the resource is oversold |

**Each row is a bug the other two do not prevent**, and we measured all
three rather than asserting it:

- **Capacity without the resource lock** — 500 registrations at 50 seats
  seated up to 500 of them (Benchmark 1). The user lock was present and
  irrelevant: 500 *different* students contend on no common user row.
- **Quota without the user lock** — one student went over cap in
  **25/25** trials (Benchmark 2), while *the capacity race passed on the
  very same broken build*: 12 concurrent requests for 8 units sold
  exactly 8. The cluster lock was already perfect.
- **Exactly-once without the shared commit** — the row count stayed at
  one in every build we tried, because `UNIQUE` held it. What broke was
  the *reply*: 86 of 120 retries became `500` without a SAVEPOINT, and 98
  of 120 became a spurious `409` when the key committed separately.

The serialization point for exactly-once is an **index, not a lock**, and
that is not a stylistic choice: there is nothing to lock until somebody
creates the row, and "somebody creates it" is precisely the window a
concurrent retry arrives in. Two simultaneous retries race to `INSERT`;
Postgres lets one proceed and makes the other **block on the index
entry** until the first transaction ends.

## Lock ordering

A single allocation takes two row locks, so deadlock is possible. The
order is fixed **globally**:

```
1. idempotency key INSERT     the unique index is the serialization point
2. USER row        FOR UPDATE the quota gate
3. RESOURCE row    FOR UPDATE the capacity gate
4. write both sides, then COMMIT
```

Because *every* path acquires them in this order, a transaction holding
the user lock cannot be waiting behind one holding the resource lock —
the cycle cannot form, so deadlock is **structurally impossible** rather
than merely unlikely. Allocation, cancellation, room booking, course
registration and waitlist join all obey it.

Two details that look like inconsistencies and are not:

- **Cancellation locks the reservation's OWNER, not the caller.** An
  ADMIN releasing someone else's hold must serialize against *that
  user's* allocations; locking the admin's own row would protect nothing.
- **Read-only gates take `FOR SHARE`, not `FOR UPDATE`.** The room
  booking path and the waitlist join read `status` / `enrolled_count` and
  never write them. What those gates need is for the value not to
  *change* underneath them, which is exactly what a share lock says — it
  blocks writers and not other readers. Taking `FOR UPDATE` there would
  also be correct, and would quietly serialize every booking of one room
  behind every other, making the application lock rather than the
  exclusion constraint the thing deciding who wins a slot race. The
  invariant would hold and the design claim would not.

### The one path that cannot obey the order

Waitlist promotion runs inside a drop, and needs a **second** user row —
the candidate's. It cannot know which one until it has read the queue,
and reading the queue consistently needs the offering lock it already
holds. So its order is offering → user, while registration's is user →
offering:

```
T1  X drops offering O      holds user(X) → O,  wants user(Y)
T2  Y registers for O       holds user(Y),      wants O
                            → cycle, deadlock
```

There is no ordering fix, because Y's identity is the *output* of the
read that requires the lock. **So the wait is removed instead of
ordered:** each candidate's row is attempted `FOR UPDATE SKIP LOCKED`,
and a row that is not immediately free is skipped for the next candidate.

A transaction that never blocks on a user row **cannot appear in a wait
cycle at all** — which is a stronger statement than obeying the global
order, and it means the "every path" claim needs no exception written
into it.

The cost is stated rather than hidden: the promise becomes *oldest
**eligible**, not oldest*. A queued student who happens to be doing
something else at that instant is passed over, and nothing about their
row changes when it happens — which is why every skip is logged. See
Benchmark 4's column 3, the only measurement in the project of the claim
that promotion never waits.

## The canonical allocation transaction

This is `gpus/service.py::reserve_gpu`, the only path where all three
guarantees meet. Everything between `BEGIN` and `COMMIT` is one
transaction — there is no intermediate commit, so a failure at any gate
rolls back every earlier statement, and **both locks are held until
commit**.

```sql
BEGIN;                                            -- wraps ALL of the below

  -- (0) Does the target exist, and is it a cluster?  NO LOCK, and safe
  --     only because it reads immutable state: a row never changes from
  --     GPU to ROOM. `status` is admin-mutable and is NOT read here — it
  --     is read at step (3), against the row just locked.
  --     Without this, a caller already at quota gets QUOTA_EXCEEDED for
  --     naming a cluster that does not exist: the quota gate fires first
  --     and nobody ever checks the target. Found by running it.
  SELECT … FROM gpu_clusters JOIN resources USING (id)
   WHERE id = :cid AND resource_type = 'GPU';     -- no row → ROLLBACK, 404

  -- (1) EXACTLY-ONCE. Above BOTH locks, so a replay never even queues
  --     for them. The SAVEPOINT is load-bearing: a unique violation
  --     aborts the whole transaction, so without it the read that
  --     fetches the stored response cannot run.
  SAVEPOINT claim;
    INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash,
                                  response_body, status_code)
    VALUES (:key, :uid, 'gpu.reserve', :hash, NULL, NULL);
  -- on unique violation:
  --   ROLLBACK TO SAVEPOINT claim;
  --   SELECT … → different endpoint/hash → 422 IDEMPOTENCY_KEY_REUSED
  --            → status_code IS NULL      → 409 IDEMPOTENCY_IN_PROGRESS
  --            → otherwise                → replay it, stored status and body

  -- (2) QUOTA GATE — keyed on the USER.
  SELECT id FROM users WHERE id = :uid FOR UPDATE;
  SELECT max_units FROM role_quotas                  -- policy: NO lock
   WHERE role = :role AND resource_type = 'GPU';     -- no row → fails closed
  SELECT COALESCE(SUM(gpu_count), 0) INTO held       -- read AFTER the lock
    FROM gpu_reservations
   WHERE user_id = :uid AND status = 'ACTIVE';
  -- held + :n > max_units → ROLLBACK, 409 QUOTA_EXCEEDED

  -- (3) CAPACITY GATE — keyed on the RESOURCE.
  SELECT * FROM gpu_clusters WHERE id = :cid FOR UPDATE;
  -- status = 'BLOCKED'            → ROLLBACK, 409 RESOURCE_BLOCKED
  -- allocated + :n > gpu_count    → ROLLBACK, 409 CAPACITY_EXHAUSTED

  -- (4) WRITE. The counter and the row are derived from each other and
  --     are always written together.
  UPDATE gpu_clusters SET allocated = allocated + :n WHERE id = :cid;
  INSERT INTO gpu_reservations (gpu_cluster_id, user_id, gpu_count, status)
  VALUES (:cid, :uid, :n, 'ACTIVE');
  UPDATE idempotency_keys SET response_body = :resp, status_code = 201
   WHERE key = :key AND user_id = :uid;

COMMIT;                                             -- key + allocation, together
```

**The load-bearing detail is step 4's last statement.** The idempotency
key and the allocation become durable in the *same* commit. Split them
and the bug moves rather than disappearing — commit the allocation but
lose the key and a retry double-allocates; commit the key but roll back
the allocation and a retry replays a success for work that never
happened. Both alternatives were built and measured; see Benchmark 3.

Three things that are easy to get wrong here and cost us time:

- **Read the value *after* taking the lock, never before.** A count read
  before the lock is a count that can move before the write, which is
  precisely the race being closed.
- **Taking the lock and reading the locked value are two different
  things.** In an ORM, a `SELECT … FOR UPDATE` that returns an
  already-identity-mapped row hands back the *existing object without
  refreshing its attributes* — the `FOR UPDATE` is really in the
  statement, the lock is really held, and the value compared is from
  before it. Proven directly: session A reads 0, session B commits 41,
  A's locked read still says 0. Every locked read in this codebase
  therefore carries `populate_existing()`.
- **`get_db()` deliberately does not commit.** A commit we did not write
  is a lock released while we still thought we held it, which is why the
  boundary lives on the line after the last write and nowhere else.

---

## Known limits, stated rather than hidden

- **Idempotency covers the GPU path only.** Course registration gets
  deduplication for free from `UNIQUE(student_id, course_offering_id)`,
  but a retry returns a duplicate error rather than the original success.
- **The room quota is not time-aware.** A reservation whose `end_time`
  has passed but whose status is still `ACTIVE` counts against the quota.
  Nothing in this system expires reservations — there is no sweeper job —
  so `ACTIVE` *is* the definition of held. Making the quota time-aware
  would invent a third state the exclusion constraint, the cancel path
  and the availability endpoint all know nothing about.
- **`role` is accepted from the registration body**, so a caller can
  register themselves as ADMIN. Accepted deliberately: the alternative is
  bootstrapping the first admin out of band, which adds a seeding concern
  to every clean-room run for a project whose claim is about concurrency,
  not identity.
- **Availability endpoints are advisory.** `free`, `available` and
  waitlist `position` are read without a lock and are stale the moment
  they are returned. They exist to show a human what is there, never to
  decide an allocation — the transaction re-reads everything under the
  lock and is what has to be right.
- **There is no `POST /courses` or `POST /offerings`.** Offerings are
  created by the seed script and by the gate scripts directly.
- **One unresolved observation, carried openly to the end.** On the GPU
  path, a two-session probe shows a `FOR UPDATE` read returning stale
  attributes without `populate_existing()` — and the 12-racer capacity
  race does *not* reproduce it: removing the call still gives a correct
  8/8, with `allocated` matching `SUM(active)` exactly. Both were run
  more than once and **they cannot both mean what they appear to.** We
  never established which is misleading us. The keyword stays, because
  relying on a read a probe says is wrong — to protect an invariant that
  merely happens to hold — is not a position we can defend. In `register`
  the same line is a measured fix (20 concurrent registrations for 5
  seats, counter landing on 3); here it is a precaution, and the
  difference is stated rather than blurred. `CROSS_PRESENTATION.md` §6.3.

### Designed, not implemented

- **Locust load scenarios** — cut on Deadline 1. The asyncio harness
  already proves correctness, which is the claim; throughput numbers we
  never optimise against invite "so what did you do with that?"
- **A denormalized quota counter** — held units are recomputed by `SUM`
  under the user lock, so they cannot drift. A counter is an optimization
  no benchmark has earned: nothing here has shown the user row to be a
  bottleneck. Revisit only if the harness shows it is.

---

## Repository map

```
app/
├── core/          security.py (JWT, Argon2) · dependencies.py (authn/authz)
│                  errors.py (the coded-error envelope) · config.py
├── auth/          register, login
├── users/         GET /me, GET /me/quota
├── quotas/        RoleQuota policy, the enforcement helper, admin endpoints
├── idempotency/   claim / record_response
├── gpus/          THE flagship transaction
├── rooms/         interval reservations (GiST exclusion constraint)
├── courses/       registration, drop, waitlist, promotion
├── models/        SQLAlchemy ORM
└── database/      session (pool sizing), base

alembic/versions/  5 revisions, head 1ca8b85b7626
scripts/           seed.py + 9 gate scripts
tests/concurrency/ harness.py, its own tests, and the 4 benchmarks
```

`quotas/` and `idempotency/` are **helpers called from inside other
modules' transactions**, never standalone services. They take a `Session`
they did not create, emit no `COMMIT`, and know nothing about HTTP —
which is what makes the guarantees atomic.

---

## Further reading

| File | What it holds |
|---|---|
| `DECISIONS.md` | what we tried, what we measured, what we chose — including every reversal |
| `ARCHITECTURE_AND_WORKFLOWS.md` | the system as built, in detail |
| `CROSS_PRESENTATION.md` | each of us defending the other's modules, and being corrected — plus the fresh-clone verification and what ships open |
| `EXECUTION_PLAN.md` | the ten deadlines and what "met" required |
| `WORK_LOG.md` | when work actually happened, and what it cost |
| `INIT_PLAN.md` | the original proposal, synced to the schema at head |

**Precedence when they disagree:** the models and migrations are the
truth; then `DECISIONS.md`; then `ARCHITECTURE_AND_WORKFLOWS.md`; then
`WORK_LOG.md` and `EXECUTION_PLAN.md`; then `INIT_PLAN.md`.
