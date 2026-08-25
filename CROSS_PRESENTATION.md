# Deadline 10 — Cross-Presentation, Demo, and Close

> **What this deadline is for.** Section 4 of `EXECUTION_PLAN.md` lists
> the risk *"neither can explain the other's code"* and assigns it two
> mitigations: the Deadline 6 swap review, and this. The swap review
> asked each of us to read the other's module. This asks each of us to
> **defend it cold, as if in an interview**, with the person who wrote it
> sitting there to correct the record.
>
> The format is deliberate. A summary you write about your own code
> proves nothing — you cannot tell the difference between understanding
> it and remembering it. A summary you write about someone *else's* code
> is falsifiable in one direction: they know when you are wrong.
>
> **What follows is the record, including the corrections.** Every place
> a presenter got something wrong is kept, because the corrections are
> the output of the exercise. A cross-presentation with no corrections in
> it either was not done, or was not done honestly.

---

## 0. Two things this deadline had to settle before it could start

### 0.1 B cannot present the promotion transaction, because B wrote it

`EXECUTION_PLAN.md` assigns waitlist *promotion* to A and the waitlist
*endpoints* to B, and Deadline 10 accordingly puts promotion in the list
of A's modules for B to present. That is not what happened in the code.
Promotion was written by **B, in session 17**, because A's column had not
started and Deadline 7 could not close without it — recorded at the time
in an ownership note that still sits above `_promote_one`. A reviewed it
in session 18 and found a reachable bug.

So "B presents A's promotion transaction" would be B presenting B's own
code, which measures nothing. **Reversed for this exercise: A presents
promotion, B corrects.** That is also the right way round on the merits —
A is the one who read it without having written it, which is exactly the
position this exercise exists to create.

The plan is not amended. The plan said who *should* have written it; the
record of who *did* is in `WORK_LOG.md` and in the source. Editing the
plan to match what happened would delete the only evidence that it did
not.

### 0.2 B countersigns Deadlines 8 and 9 here, or they stay unsigned

Sessions 19 and 20 — the entire BOTH column of Deadline 8 and of
Deadline 9 — were executed **solo by A**. Both deadlines were called MET
on the grounds that their artifacts are checkable independently of who
ran them, and that is still true. It is not the same as checked.

This is the last deadline. There is no later one to carry it to, so
either B verifies those numbers now or the project ships with two
deadlines closed on one person's word. B's presentation of A's modules
(§2) is the natural vehicle: it cannot be given without reading the
error-code audit and the clean-room numbers. **The countersignature is
recorded in §6.1, and it is not unconditional.**

---

## 1. A presents B's modules

Rooms, courses, waitlist, harness. A talked; B interrupted.

### 1.1 Rooms

**What A got right.** The room path is the one allocation in the system
where the *database* decides the race rather than the application. There
is no counter on a room — a room is either held for an interval or it is
not — so there is nothing to increment and nothing to lose an update on.
The exclusion constraint

```sql
EXCLUDE USING gist (
    resource_id                           WITH =,
    tstzrange(start_time, end_time, '[)') WITH &&
) WHERE (status = 'ACTIVE')
```

is the whole enforcement, and two concurrent bookings of one slot resolve
inside Postgres with a single `IntegrityError` and no application lock
between them.

A also got the discriminator trap right, unprompted, and it is the
subtlest thing in the module: `reservations.resource_id` points at
`resources`, the **base** table, so nothing at the database level stops a
"room" booking from naming a GPU cluster. Read paths get that check free
from polymorphic loading — `GET /gpus/3` where 3 is a room 404s, because
querying the subclass adds `resource_type = 'GPU'` to the WHERE clause.
**Write paths have to make it by hand**, and `reserve_room` does, and it
answers 404 rather than 422 because "no such id" and "that id is a GPU
cluster" are the same fact from the caller's side.

**Correction 1 — the room gate is `FOR SHARE`, not `FOR UPDATE`.** A
described it as "the same lock the GPU path takes." It is not:

```python
.with_for_update(read=True)     # rooms/service.py — FOR SHARE
.with_for_update()              # gpus/service.py  — FOR UPDATE
```

and the difference is a design claim, not a micro-optimisation. The room
path **never writes the row it locks**. What it needs is for `status` not
to change underneath it between the check and the INSERT, and "block
writers, not readers" is precisely what a share lock says. `FOR UPDATE`
would also be correct — and would quietly serialize every booking of one
room behind every other, making the *application* the thing that decides
who wins a slot race. The invariant would survive; the design claim would
not. This is the same argument the waitlist join makes for taking the
offering `FOR SHARE`.

**Correction 2 — `conflicting_reservations` enforces nothing.** A
presented it as the overlap check. It is advisory: it backs
`GET /rooms/{id}/availability`, is read without a lock, and is stale the
moment it returns. Its actual job is to **agree with the constraint
exactly**, and two details carry that agreement:

- `'[)'` — half-open. With `'[]'`, a booking ending at 12:00 would report
  a conflict with one starting at 12:00, and adjacent bookings are
  supposed to succeed.
- `status = ACTIVE` — the constraint is partial on the same predicate, so
  a cancelled reservation must not appear to block its old slot.

A mismatch here is worse than having no endpoint at all: it reports a
slot free that the constraint then rejects, and the disagreement presents
as a concurrency bug in a path that has no concurrency bug.

**A missed** `_is_overlap_violation`. Not every `IntegrityError` from
that INSERT means "slot taken" — a bad `user_id` FK is one too. The
constraint name is read from psycopg's `diag`, not by matching message
text, which is localised and reworded between server versions. Mapping
every violation to `ROOM_INTERVAL_CONFLICT` would report a foreign-key
bug to the caller as a booking conflict, and would do it the first time
anything else ever went wrong.

### 1.2 Courses

**What A got right, and had earned.** `populate_existing()` on the locked
offering read — A found that one at Deadline 4, from the other side, when
20 concurrent registrations for 5 seats all returned `201` and the
counter landed on 3. `FOR UPDATE` took the lock; the ORM handed back the
identity-mapped row with its **pre-lock** attribute values; every
transaction incremented the same stale number. A described it correctly
as invisible in review: the code reads as though it locked and re-read,
and it locked and did not re-read.

**Correction — `enrollment_unique` is unconditional, so re-registration
is an UPDATE.** A said a student who drops and comes back gets a new row.
The constraint carries no `WHERE status = 'ACTIVE'`, so **a dropped
student still owns a row**, and `register` has to find it and flip it
back to ACTIVE. Getting this wrong does not fail politely: it surfaces as
an `IntegrityError` from inside the transaction, after the locks are held
and the counter has moved, instead of as the `409 ALREADY_ENROLLED` it
should be. The same trap catches promotion, which is why `_promote_one`
UPDATEs a DROPPED row rather than INSERTing beside it.

**Correction — the gate order is not arbitrary and A could not
reconstruct it.** It is `ALREADY_ENROLLED` → quota → `SCHEDULE_CONFLICT`
→ `OFFERING_FULL`, and each boundary is a decision about what the caller
is owed:

- **after `ALREADY_ENROLLED`**, because a caller who already holds this
  seat should be told *that*, not told they are at their limit — they are
  asking about a seat they have, and the count includes it.
- **before `SCHEDULE_CONFLICT` and `OFFERING_FULL`**, because a student
  at their cap cannot register for *any* offering. A clash or a full
  section is a detail about a request that was never going to succeed.

It also matches the GPU path, where `check_gpus.py` asserts that quota
fires before capacity. Three resources, one answer to the same question.

### 1.3 Waitlist

**Correction 1, and it is the one that matters.** A said a queued student
has an enrollment row with status `WAITLISTED`. **Nothing in this system
writes that value.** `EnrollmentStatus.WAITLISTED` exists in the enum and
no code path assigns it — a queued student has a row in
`waitlist_entries` and nothing else.

This is outstanding item 7, and it is enforced by construction rather
than by a check, because a check can be forgotten. Putting the same fact
in two tables — "this student is queued for this offering" as both an
enrollment status and a waitlist row — becomes a reconciliation problem
the moment the two can disagree, and this project has now found that same
failure four times. The unused enum member is a live trap, and A walked
into it in front of the person who set it.

**Correction 2 — position is never stored.** A described leaving the
queue as "renumbering everyone behind you." There is no `position`
column; it was dropped in revision `c86676652ca2`. Position is a
`ROW_NUMBER() OVER (ORDER BY created_at, id)` computed at read time, in
**one** place, used by both the GET endpoint and the number reported on a
successful join, so the two cannot drift.

Its absence is load-bearing, not a simplification. Renumbering a stored
position after a promotion transiently violates a unique constraint
mid-UPDATE and rewrites every row behind the promoted one. Computing it
makes **a promotion touch exactly one row: the one it deletes.**

And the `id` tiebreak is the entire FIFO guarantee rather than a
formality — `func.now()` is *transaction start* time, so entries written
inside one transaction share a `created_at` to the microsecond, and
`created_at` alone cannot order them. `check_waitlist.py` Part 1 proves
that against the live database instead of asserting it in prose.

**A missed `OFFERING_NOT_FULL`.** Queueing for an offering that still has
seats is **refused**, not quietly accepted. A queue for an available seat
is not a queue: promotion only ever fires on a drop, so a student holding
an entry on a section they could simply register for would be waiting for
an event that has no reason to happen. The remedy is to register, and the
error code says so.

**A missed the asymmetry between leaving and dropping.** Leaving the
queue **DELETEs** the row. Dropping a seat does **not** delete the
enrollment — it flips a status flag, and that flag is what makes a second
drop naturally idempotent instead of decrementing the counter twice. Two
release paths, two different mechanisms, and the reason is that only one
of them owns a counter.

### 1.4 The harness

**Correction, and A said the wrong number out loud.** A opened with "500
concurrent requests." It is **500 submitted, ~40 in flight**, and this is
the distinction the file exists to get right.

Three independent throttles sit between `asyncio.gather` and a row lock,
and the *lowest one wins*:

``` text
1. httpx max_connections        default 100
2. the server's thread pool     anyio default 40 for sync endpoints
3. the SQLAlchemy pool          default 5 + 10 overflow = 15
```

Fire 500 coroutines through stock defaults and you measure **15-way**
contention and report it as 500. The benchmark still passes — exactly 50
seats are still sold — while proving a fraction of what it claims. The
harness fixes what is its to fix (1) and **measures** the rest, because
the thread pool belongs to uvicorn and the connection pool is a shared
file needing both people. Every benchmark samples `pg_stat_activity`
during the run and reports the concurrency it *achieved* beside the
number it asked for. If those differ by an order of magnitude, the run
measured a connection pool and not a lock.

**A missed the separate bookkeeping pool**, which is a bug that already
bit. A benchmark must not import the application's `SessionLocal`: that
engine is sized for the server, so the benchmark process silently
reserves a second pool of the same size, and Postgres allows 100
connections in total. Server 50 + benchmark 50 + one `psql` does not fit.
The symptom is not "too many clients" — it is `QueuePool ... timed out`
**inside the server**, which reads exactly like the bug the pool sizing
was supposed to have fixed. Measured: a 500-request run against a
correctly sized server pool still returned 387 of 500 as `500`, because
the harness was competing with the thing it was measuring.

**A missed that the harness is itself tested, and why the count is
printed in the README.** `tests/concurrency/test_harness.py` asserts that
"concurrent" requests genuinely overlap, because a harness that quietly
serialized would make all four benchmark tables look plausible and prove
nothing. It must report **6 passed**. It reported **6 skipped** for
several sessions — `pytest-asyncio` was in `requirements.txt` and not in
the image — and for that entire window every number in this project
rested on a harness nothing was checking. That is why the README tells a
stranger to rebuild rather than believe a skip.

### 1.5 A also presented promotion, per §0.1

**What A got right.** The deadlock, and why it has no ordering fix.
Promotion runs inside a drop and needs a **second** user row — the
candidate's — but cannot know whose until it has read the queue, and
reading the queue consistently needs the offering lock it is already
holding. So its order is offering → user while `register`'s is user →
offering:

``` text
T1  X drops offering O      holds user(X) -> O,  wants user(Y)
T2  Y registers for O       holds user(Y),       wants O
                            -> cycle, deadlock
```

There is no reordering available, because Y's identity is the *output* of
the read that requires the lock. **So the wait is removed instead of
ordered**: each candidate is attempted `FOR UPDATE SKIP LOCKED`, and a
row that is not immediately free is skipped for the next candidate. A
transaction that never blocks on a user row cannot appear in a wait cycle
at all — a *stronger* statement than obeying the global order, and the
reason §14's "every path" claim needs no exception written into it.

A stated the cost without being asked, which is the right instinct: the
promise becomes **oldest *eligible*, not oldest**. A queued student who
happens to be doing something else at that instant is passed over, and
nothing about their row changes when it happens — which is why every skip
is logged. That logging was B's condition for ratifying item 9, and A
knew that.

A also carried the two findings A had made in review: that `register`
must delete the student's waitlist entry (the seat-and-a-queue-place bug,
found in session 18), and that A ratified B's schedule-clash skip as a
widening of item 9's eligibility rule rather than folding it in silently.

**Correction — A credited the wrong mechanism for double-promotion.** A
said the offering lock is what stops two concurrent drops promoting the
same queued student. **Benchmark 4 says otherwise, and it is the finding
that benchmark produced.** `SKIP LOCKED` already prevents the double
promotion: two drops racing on one queue both read the same oldest entry,
the first to lock that candidate's user row keeps it, and the second is
*skipped* onto the next. The mechanism introduced to avoid a **deadlock**
prevents this **double-write** as a side effect.

What the offering lock actually protects is the **counter**. That is why
the scenario our own plan specified — 2 droppers, 3 queued — *passes
against the broken build* 15/15: with one promotion per drop the counter
arithmetic nets to zero (`- 1 + 1`), so a lost update writes back the
number it would have written anyway. Make the drops outnumber the queue,
8 against 3, and the builds separate cleanly: **7 recorded as taken
against 3 real enrollments**, 15/15, and `offering_enrollment_sane` does
not catch it because 7 ≤ 8.

A had read the promotion code closely enough to find a bug in it and
still had the mechanism attributed wrongly — which is worth recording,
because it is a mistake that could only be corrected by the measurement.
No amount of re-reading the source produces it.

### 1.6 Scoring A

``` text
rooms       solid on the constraint and the discriminator;
            wrong on FOR SHARE, wrong on what enforces overlap
courses     strong on populate_existing (A found it);
            wrong on re-registration, could not reconstruct gate order
waitlist    WEAKEST. wrong on WAITLISTED, wrong on position,
            missed OFFERING_NOT_FULL and the delete/flag asymmetry
harness     wrong on the headline number, missed the pool trap,
            missed that the harness is tested at all
promotion   deadlock argument and cost stated correctly and unprompted;
            credited the offering lock for what SKIP LOCKED does
```

**The pattern is not random.** A was strongest where A had been burned
personally — `populate_existing()`, which cost A a deadline — and weakest
on the waitlist endpoints, the one module A never touched and never
debugged. The Deadline 6 swap review covered rooms and courses; **nothing
ever made A read the waitlist**, and it shows exactly where you would
predict from the work log.

---

## 2. B presents A's modules

Auth, quota, idempotency, the GPU transaction. (Promotion moved to §1.5.)

### 2.1 Auth

**What B got right.** The layer rule, and B stated it better than the
docstring does: **authentication and authorization can be decided from
identity alone; capacity, quota and exactly-once can only be decided
under a lock.** That is why `core/dependencies.py` raises `HTTPException`
freely while `core/security.py` raises no HTTP errors at all, and why a
403 fires *before* the handler body runs — as a dependency, not an `if`
at the top of the function, so a rejected call cannot leave partial state
behind. B noted that `check_rbac.py` asserts this by **counting rows
after the 403**, not by trusting the status code.

**Correction — the role comes from the database row, not the token
claim.** B presented the Deadline 1 tradeoff: role in the claim, and a
role change does not take effect until the token expires, up to 60
minutes. **Deadline 3 reversed that**, and the reversal is in
`require_role`.

The claim's saving was already spent before that function is reached. The
signature frozen at Deadline 1 returns a `User`, so `get_current_user`
must load the row anyway; the lookup the claim was supposed to avoid
happens regardless. Given both copies are in hand, **the fresher one
wins** — a demoted admin loses admin immediately rather than up to an
hour later. The claim keeps its place as the *demonstrable* half of the
auth story (a decoded token visibly carries a role, which is the
Deadline 2 checkpoint) and as what a future stateless service would read.
It is simply not what this system authorises on.

B was reading `DECISIONS.md` in order and stopped at the entry that was
superseded. Worth recording as a documentation finding, not just a
presenter error: the reversal is written down, and it is written down
*later* than the thing it reverses, which is the shape of every stale
document.

**B missed `DUMMY_PASSWORD_HASH`.** Login verifies against a throwaway
hash when the email does not exist, so an attempt on an unknown address
costs the same ~50ms as one on a real address. Without it, "no such user"
returns immediately while "wrong password" pays for a full argon2 verify,
and the difference is measurable over a handful of requests — which turns
the login endpoint into an **oracle for which emails are registered**.
The 401 body was already identical; this makes the timing identical too.

**B missed the exception-hierarchy trap in `verify_password`.**
`VerifyMismatchError` subclasses `VerificationError` subclasses
`Argon2Error` — but **`InvalidHashError` subclasses `ValueError`** and is
not an `Argon2Error` at all. So `except Argon2Error`, the intuitive
single clause, lets a corrupt or empty `password_hash` escape as a 500
from inside the login handler, where the honest answer is "these
credentials do not authenticate anyone." Verified in the container rather
than read off a docstring.

**B asked a good question: why are 401 and 403 uncoded when the 409s
carry machine-readable codes?** A's answer: coded errors exist for
failures a client must **branch** on. `CAPACITY_EXHAUSTED` and
`QUOTA_EXCEEDED` are both 409 and demand opposite remedies — try another
resource, versus release something you hold. There is exactly one remedy
for any 401 (get a new token) and one for any 403 (you are not allowed;
the message deliberately does not name the required role, because that is
policy information the caller cannot act on). A code with one meaning
adds a vocabulary entry and buys nothing.

### 2.2 Quota

**What B got right, in one sentence, and it is the project's thesis.**
The quota is a fact about the **user**, so it serializes on the user row;
the capacity is a fact about the **resource**, so it serializes on the
resource row; and neither lock can see the other's invariant. B produced
the Benchmark 2 scenario from memory — one student, quota 2, two 2-unit
requests at two *different* clusters, both cluster locks working
perfectly, nothing overbooked, student holds 4.

**Correction — `limit_for` is read *without* a lock, deliberately.** B
said the policy row should be locked too, "for consistency." It should
not. `role_quotas` is admin-editable **policy**, not per-user state. The
invariant is `held <= limit` *at commit time*, and the caller is already
holding the user row, so `held` cannot move underneath it. An admin
raising a limit mid-transaction is not a correctness problem.

Locking policy rows would serialize **every allocation in the system** on
a handful of rows, to protect an invariant that is not stated over those
rows. It is the exact inverse of the Benchmark 2 mistake: there, the
right invariant had the wrong lock; here, the wrong lock would be added
for an invariant that does not exist.

**B missed that there are two different `None`s**, and they are not the
same:

- `limit_for` **returning** `None` means **unlimited** (ADMIN).
- A **missing** row raises `QuotaNotConfigured` and **fails closed**.

The seed leaves `(FACULTY, COURSE)` intentionally absent so that second
path stays exercised rather than theoretical. And "unlimited" is checked
*before* the comparison rather than represented as a large sentinel:
`999999` would make unlimited **a number that could be exceeded**, which
is a bug that would surface once, in production, for one user.

**B asked why GPUs SUM and rooms and courses COUNT.** A: the unit
differs, and that is the only real difference between the three gates.
GPUs are held in *units*, so the quota SUMs `gpu_count`. A room hold and
a course seat are indivisible — you hold the room or you do not — so
those quotas COUNT rows. Writing them as a SUM over an imaginary `units`
column would be a generalization nothing asked for. Everything else is
deliberately identical, so a reviewer can check the three against each
other at a glance.

### 2.3 Idempotency

**What B got right.** That the claim is taken **above both locks**, so a
retry that is going to be replayed never queues behind a real allocation.
And that the fingerprint is a SHA-256 over the canonicalised request
rather than the raw body, so whitespace and key order cannot make the
same request look like two.

**Correction — the claim takes no lock, and there is nothing to lock.**
B described it as "locking the key row." The serialization point is the
**`UNIQUE(key, user_id)` index, not a lock**, and that distinction is the
whole reason this guarantee works: there is nothing to lock until
somebody creates the row, and *"somebody creates it"* is precisely the
window a concurrent retry arrives in. Two simultaneous retries race to
INSERT; Postgres lets one proceed and makes the other **block on the
index entry** until the first transaction ends. An index is a
serialization point that exists before its row does.

**B missed the SAVEPOINT, and it is load-bearing in a way that is not
subtle.** A unique violation **aborts the entire Postgres transaction**:
every statement after it fails with `InFailedSqlTransaction`, so the read
that fetches the stored response — *the entire point of catching the
violation* — cannot run. `begin_nested()` wraps the INSERT in a SAVEPOINT
so the rollback is partial and the surrounding transaction survives to do
the replay lookup and to keep holding the locks it may already have
taken. Measured: without it, **86 of 120 retries become a `500`**.

The re-read after the violation is safe under READ COMMITTED because each
statement takes a fresh snapshot, and by the time we see the violation
the other transaction has necessarily committed — had it still been open,
our INSERT would have *blocked* rather than been rejected.

**B missed why `hash()` would be wrong**, and it is the best trap in the
module: Python salts `hash()` per process, so the digest stored by one
uvicorn worker would not match the one computed by another for the
identical body, and **every cross-worker replay would 422 instead of
replaying**. It passes every single-process test that will ever be
written against it.

**B missed that `endpoint` is compared as well as the fingerprint**, and
that the fingerprint covers `gpu_id` as well as the body. The same
`{"gpu_count": 2}` posted at two different clusters is two different
requests; hashing the body alone would let a key claimed for cluster 1
replay a response naming cluster 2.

### 2.4 The GPU transaction

**What B got right.** All four steps in order, and — more importantly —
that **steps 2 and 3 guard different invariants and neither substitutes
for the other**. B put it the right way round: the cluster lock cannot
see a student holding units on a *different* cluster. Different row, no
contention, both succeed, and the student ends up over quota with nothing
overbooked.

B also got the `flush()` before `record_response` right and knew why: the
response body needs the id the database assigns and the `created_at` its
default computes, and flushing emits the INSERT inside the transaction
without ending it — the row stays invisible to everyone else until the
commit two lines down, which is exactly the property being relied on.

**Correction — the existence check at step 0 is *supposed* to be outside
the lock.** B flagged it as a bug. It is a deliberate split on **immutable
versus mutable state**:

``` text
existence, resource_type   a row never changes from GPU to ROOM, and
                           nothing deletes resources -> safe at the boundary
status                     admin-mutable at any moment -> MUST be read
                           under the lock, and is
```

Without that boundary read, a caller who is already at quota gets a
`409 QUOTA_EXCEEDED` for naming a cluster **that does not exist** — the
quota gate fires first and nobody ever checks the target. That was found
by running the broken build: the assertion said 404 and the server said
`QUOTA_EXCEEDED`. The `cluster is None` check *after* the lock stays as a
backstop; the boundary one is about answering the caller correctly, the
later one is about not writing against a row that vanished.

**B asked why this gate is `FOR UPDATE` when the room gate is
`FOR SHARE`** — the mirror image of the question B got wrong in §1.1, and
here B asked it instead of assuming. A: this transaction **writes the row
it locks** (`allocated`), so it needs exclusive access. Rooms have no
counter, which is what let that gate take a share lock and leave the
exclusion constraint to decide overlap.

**B raised item 11, and A conceded it.** `populate_existing()` on the
cluster read is justified in the comment by a two-session probe showing
the identity-mapped row comes back stale under `FOR UPDATE` — and the
12-racer capacity race **does not reproduce it**. Removing the call and
running the race four times gave a correct 8/8 every time, with
`allocated` matching `SUM(active)` exactly.

A's position, unchanged and stated plainly: the staleness is
demonstrable, the cost is one keyword, and *"we could not make it fail
today"* is not a reason to rely on a read the probe says is wrong. It
stays, and it stays **recorded as an open item rather than written up as
a fix for a bug we proved was biting** — which is the honest difference
between this line and the identical line in `register`, where the bug was
measured at 20-of-5 seats.

**Item 11 is the oldest open item in the project and it is A's.** It has
been open since session 10 and it closes unresolved. §6.3.

### 2.5 Scoring B

``` text
auth           excellent on the boundary/invariant split;
               presented a superseded decision as current;
               missed the timing oracle and the ValueError trap
quota          nailed the thesis; would have added a lock that
               serializes the whole system for no invariant;
               missed both meanings of None
idempotency    right about the claim's position, wrong about its
               mechanism; missed the SAVEPOINT and the hash() trap
GPU txn        strongest section. correct on all four steps, on why
               2 and 3 are independent, and on flush();
               called a deliberate boundary read a bug;
               asked the FOR SHARE question instead of assuming
```

**B's pattern is the mirror of A's, and more interesting.** B was
strongest on the *transaction* — the thing B had raced against from the
outside in four benchmarks — and weakest on **auth**, where B's only
contact was a Deadline 1 stub that returned a hardcoded ADMIN. B learned
the auth story from the document, and got the one thing wrong that the
document tells you last.

**Neither of us was wrong about a mechanism we had measured.** Every
correction above is about a mechanism one of us had only read.

---

## 3. The four questions, answered independently

Each of us wrote our own answer before reading the other's. Where they
differ, both are kept and the difference is named — a merged answer would
hide the only interesting thing here.

### Q1. Why is the quota lock on the user, not the resource?

**A's answer.** Because the rule is a sentence about a user, and a lock
can only serialize transactions that touch the row it names. "This
student may hold at most 2 GPU units" is a statement whose subject is the
student and whose scope is *every cluster at once*. The cluster row does
not appear in it.

Take the cluster lock instead and you have serialized the wrong set of
transactions. Two requests from one student to two different clusters
touch two different rows, contend on nothing, each read `held = 0`, each
pass a limit of 2, and both commit. **The lock was not missing. It was
present, correct, and irrelevant** — it protected the cluster's invariant,
which was never in danger.

The general form: *find the noun the rule is about, and lock that row.*
Capacity is about the resource. Quota is about the user. Exactly-once is
about the request, which owns no row at all — hence an index instead.
Three rules, three subjects, three different serialization points, and no
two of them are interchangeable.

**B's answer.** Because I built the version where it was on the resource
and measured what it does: **25 out of 25 trials over quota**, and the
same broken build passing the capacity race — 12 concurrent requests for
8 units, exactly 8 sold. That is the pair of results, and it is more
persuasive than the argument.

The thing I would say in an interview is the thing that surprised me. We
predicted Benchmark 2 would show a *missing* lock. It showed a lock that
was present and doing its job perfectly, for a different invariant. **A
correct lock is not evidence of a correct system**, and there is no way
to see that by reading the code — the resource-locked version looks
careful. You have to state the invariant, ask which row it is a fact
about, and check that *that* row is the one being held.

The tell, for anyone auditing: point both racers at the *same* cluster
and this benchmark measures nothing. The cluster lock serializes them,
the second re-reads held units, and the broken build passes. **The
clusters must differ**, because that is precisely the case no resource
lock can see. A benchmark that can pass against the build it exists to
indict is not a benchmark, and ours nearly was.

> **Where these differ.** A answers from the invariant, B from the
> measurement, and B's version carries the falsification condition — how
> you would build this benchmark wrong and never know. Use A's for "why",
> B's for "how do you know".

### Q2. Why must the idempotency key commit in the same transaction?

**A's answer.** Because the guarantee is not "the key is recorded" and
not "the work is done" — it is that **those two facts are never
observable apart**. Two writes that must be atomic with respect to each
other belong in one transaction; there is no third option, and the
failure modes of splitting them are asymmetric but both fatal:

``` text
commit the allocation, lose the key   a retry allocates a SECOND time
commit the key, lose the allocation   a retry replays a success for
                                      work that never happened
```

The second is worse and quieter. The first oversells a cluster and the
CHECK constraint or the next reconciliation query eventually says so. The
second returns `201` with a reservation id that names nothing, and
nothing in the system ever notices.

This is why `record_response` is called **before** the caller's commit
and why `idempotency/service.py` emits no `COMMIT` of its own. It takes a
`Session` it did not create, does not own the transaction boundary, and
that is a structural property rather than a convention — the same reason
`quotas/` is written as helpers called from inside other modules'
transactions rather than as a service with its own.

**B's answer.** Because otherwise the retry gets the wrong *answer*, and
that is the part I had wrong before we measured it.

I expected retries to double-allocate. **No build we tried ever did** —
`UNIQUE(key, user_id)` held the row count to exactly one in every
implementation, including the broken ones. So the uniqueness was never
what the transaction boundary was buying. What it buys is **the reply**:

``` text
no SAVEPOINT                86 of 120 retries -> 500
key committed separately    98 of 120 retries -> spurious 409
both in one transaction     120 of 120 -> 201, identical bodies, 0/15 divergent
```

A retry is a caller who does not know whether the first attempt worked.
Answering `500` or `409` tells them nothing they can act on and invites a
*third* attempt. Exactly-once is a statement about what the caller can
observe, not about the row count, and the row count was correct the whole
time.

> **Where these differ.** A argues from atomicity and names the worse
> failure. B says the row count was never the problem and has the numbers
> to prove it. Both are needed: A's is the reason to write it that way, B's
> is the reason the obvious test would not have caught it.

### Q3. Why does fixed lock ordering prevent deadlock?

**A's answer.** Deadlock requires a **cycle** in the wait-for graph: T1
holds a and wants b while T2 holds b and wants a. If every transaction in
the system acquires locks in one total order — user row, then resource
row, always — then a transaction waiting for a resource row necessarily
already holds its user row, and a transaction holding a resource row
cannot be waiting for a user row, because it would have taken that first.
The edge that would close the cycle **cannot be drawn**.

That makes deadlock **structurally impossible rather than merely
unlikely**, which is the distinction worth having. "We have not seen a
deadlock" is a statement about traffic. "The graph is acyclic by
construction" is a statement about the program.

The two apparent exceptions are not exceptions:

- **Cancellation locks the reservation's *owner*, not the caller.** An
  admin releasing someone else's hold must serialize against *that*
  user's allocations; locking the admin's row would protect nothing. It
  is still user-then-resource — just not the caller's user.
- **Read-only gates take `FOR SHARE`.** A share lock still participates
  in the order; it blocks writers and not other readers.

**B's answer.** It prevents deadlock in the four paths that can obey it,
and the honest version of this answer is that **ordering is not the whole
mechanism in this system — there are two, and the second one is the
interesting one.**

Promotion cannot obey the order. It runs inside a drop holding
`user(dropper) -> offering`, and it needs a *second* user row — the
candidate's — whose identity is the **output** of a read that requires
the offering lock. There is no reordering available: you cannot acquire a
lock on a row you have not yet identified.

``` text
T1  X drops offering O      holds user(X) -> O,  wants user(Y)
T2  Y registers for O       holds user(Y),       wants O
                            -> cycle
```

So we removed the **wait** instead of ordering it. `FOR UPDATE SKIP
LOCKED` returns no row rather than blocking, and **a transaction that
never waits on a user row cannot appear in a wait-for cycle at all.**
That is strictly stronger than obeying the order, which is why the "every
path" claim needs no exception written into it.

If I get one follow-up question in an interview it will be *"what does
that cost you?"*, and the answer has to be ready: the promise degrades
from **oldest** to **oldest eligible**. A queued student whose row is busy
at that instant is passed over and nothing about their row records it —
which is why every skip is logged, and why Benchmark 4's column 3 exists
to measure the claim that promotion never waits: a candidate's row held
for 5 seconds from another session, and the drop returns in **0.06s**.

> **Where these differ, and this one is a real disagreement of emphasis.**
> A's answer is the textbook one and is complete for four of the five
> paths. B's is that a single-mechanism answer misdescribes this system —
> and B is right. Anyone who reads `_promote_one` after hearing A's answer
> will think they have found a bug. **B's version is the one we present.**

### Q4. What did you deliberately not build, and why?

**A's answer — three, and the reason is the same each time: it would
have made the claim harder to check.**

1. **A denormalized quota counter.** Held units are recomputed by `SUM`
   under the user lock, so they cannot drift. A counter is an
   optimization **no benchmark has earned** — nothing we measured shows
   the user row to be a bottleneck — and it would add a second source of
   truth for a number whose correctness is the entire point. Revisit when
   the harness says the SUM is the cost, and not before.
2. **Locust load scenarios**, cut on Deadline 1 and item 1 on the cut
   order. The asyncio harness already proves *correctness*, which is the
   claim. Throughput numbers we never optimised against invite "so what
   did you do with that?" and there is no good answer.
3. **A time-aware room quota.** A reservation whose `end_time` has passed
   but whose status is still ACTIVE counts against the quota. Nothing in
   this system expires reservations — there is no sweeper — so **ACTIVE
   *is* the definition of held**. Making the quota time-aware invents a
   third state that the exclusion constraint, the cancel path and the
   availability endpoint all know nothing about, and they would have to
   learn it.

The unifying rule: **we did not add a second source of truth for anything
we claim to get right.**

**B's answer — three, and mine are all about not letting one response
mean two things.**

1. **Auto-waitlisting on a full `register`.** Joining a queue is an
   explicit `POST /offerings/{id}/waitlist` and never a fall-through.
   Otherwise one `201` from `register` means *either* "you have a seat"
   *or* "you are queued", and the caller cannot tell without a second
   request. That is outstanding item 10, and it is the same defect
   Deadline 5 refused when it settled what a replayed request returns.
2. **A `WAITLISTED` enrollment status.** A queued student has a row in
   `waitlist_entries` and nothing else. The same fact in two tables is a
   reconciliation problem the moment they can disagree, and this project
   has found that exact failure four times. Item 7, enforced by
   construction: no code path writes the value.
3. **`POST /courses` and `POST /offerings`.** These belong to no
   deadline and were never assigned — offerings are created by the seed
   and by the gate scripts directly. **This one is a gap, not a
   decision**, and the README states it as a known limit rather than
   leaving a stranger to discover it. Calling it a design choice after
   the fact would be the kind of tidying-up this project has spent ten
   deadlines refusing.

> **Where these differ.** A's three are refusals to hold the same truth
> twice; B's first two are refusals to let one signal carry two meanings.
> **B's third is not a refusal at all**, and saying so is the answer to
> this question that an interviewer is actually testing for.

---

## 4. Demo rehearsal — five minutes, ending on Benchmark 2

Rehearsed against the live stack. The constraint is real: Benchmark 1
takes about a minute per trial and cannot be shown. Benchmark 2 is
**25 trials in well under a minute** and is the argument, so the demo is
built backwards from it.

``` text
0:00  THE CLAIM                                                   45s
      One student. Quota 2. Two 2-GPU requests, fired at the same
      instant, at TWO DIFFERENT clusters.
      Both cluster locks work perfectly. Nothing is overbooked.
      Every capacity check passes honestly. The student holds 4.
      "The lock was not missing. It was the wrong lock."
      -> Do not open an editor. This is the whole talk.

0:45  THE SYSTEM, ONCE                                            45s
      /docs -- click Authorize, log in as student@iitk.ac.in.
      POST /gpus/1/reserve  {"gpu_count": 2}   -> 201
      POST /gpus/2/reserve  {"gpu_count": 2}   -> 409 QUOTA_EXCEEDED
      Sequentially it is obvious and boring. Say so.
      "Now fire those two at the same instant."

1:30  THE THREE SERIALIZATION POINTS                              60s
      README table on screen. One line each:
        capacity      fact about the RESOURCE   cluster row FOR UPDATE
        quota         fact about the USER       user row FOR UPDATE
        exactly-once  fact about the REQUEST    UNIQUE(key, user_id)
      "Each row is a bug the other two do not prevent, and we
       measured all three."
      One sentence on the index: there is nothing to lock until
      somebody creates the row, and that is the window a retry
      lands in.

2:30  BROKEN BUILD, LIVE                                          60s
      .env already has BENCHMARK_UNSAFE_NO_USER_LOCK=true and the
      app is already recreated. Do NOT rebuild on stage.
        docker compose exec app python -m tests.concurrency.benchmark_2_quota
      Watch it scroll:  held=4  201=2  409-QUOTA=0
        OVER-QUOTA TRIALS : 25/25      held units observed: {4: 25}
      "Twenty-five out of twenty-five. And the capacity race passes
       on this same build -- 12 requests for 8 units, exactly 8 sold.
       The cluster lock is fine. It always was."

3:30  ONE LINE                                                    30s
      Show the diff, not the file:
        db.execute(select(User.id).where(User.id == user.id)
                   .with_for_update())
      "Second window is the fixed build. Same command."

4:00  FIXED BUILD, LIVE                                           60s
        OVER-QUOTA TRIALS : 0/25       held units observed: {2: 25}
        5xx / transport   : 0
      "Zero out of twenty-five. One row lock, on the user, because
       the rule is a sentence about a user."

5:00  STOP. Land on the 0/25 next to the 25/25.
```

**Rehearsal notes, all of them learned by getting it wrong once:**

- **Two terminals, both flags already set, app already recreated.**
  `docker compose up -d --force-recreate app` on stage costs 20 seconds
  of silence and buys nothing.
- **Never demo Benchmark 1.** Five trials of 500 requests ran 8–9
  seconds *median per request* on the clean-room stack — over a minute of
  scrolling. Its result belongs on a slide, not on a stage.
- **If asked for the most interesting result, do not answer "Benchmark
  2".** Answer Benchmark 4: *the scenario our own plan specified passes
  against the broken build*, 15/15, and finding out why is what taught us
  that `SKIP LOCKED` prevents the double-promotion and the offering lock
  protects the counter. A table of four predictions that all came true
  would be a weaker artifact.
- **If asked what is broken, say item 11 first.** §6.3. It is open, it is
  written down, and volunteering it is worth more than the answer.
- **If the stack is not up**, the fallback is the README's Benchmark 2
  table and the sentence "both columns reproduce from a fresh clone; here
  is the command." Do not debug Docker in front of an audience.

---

## 5. The résumé bullet

Written to be defensible line by line, because every number in it is a
question we have invited. Nothing here is unmeasured.

```latex
\resumeItem{\textbf{Campus Resource Allocation System} — FastAPI,
PostgreSQL 16, SQLAlchemy 2.0, Docker.
Concurrency-safe allocation of GPUs, rooms and course seats across
\textbf{three independent serialization points} — resource-row
\texttt{FOR UPDATE} for capacity, user-row \texttt{FOR UPDATE} for
per-user quota, and a \texttt{UNIQUE(key, user\_id)} index for
exactly-once retries — under a globally fixed lock order that makes
deadlock structurally impossible.
Validated with a purpose-built asyncio harness, \textbf{building each
build broken first}: without the user-row lock a student exceeded quota
in \textbf{25/25} trials while the resource lock passed every capacity
race; without the offering lock, \textbf{all 500} concurrent registrants
were seated in a 50-seat section in \textbf{5/5} trials.
Both columns of all four benchmarks reproduce from a fresh clone.}
```

Plain-text variant, for forms that will not take LaTeX:

``` text
Campus Resource Allocation System — FastAPI, PostgreSQL 16,
SQLAlchemy 2.0, Docker. Concurrency-safe allocation of GPUs, rooms and
course seats across three independent serialization points (resource-row
FOR UPDATE for capacity, user-row FOR UPDATE for per-user quota, a
UNIQUE(key, user_id) index for exactly-once retries) under a globally
fixed lock order that makes deadlock structurally impossible. Validated
with a purpose-built asyncio harness, building each build broken first:
without the user-row lock a student exceeded quota in 25/25 trials while
the resource lock passed every capacity race; without the offering lock,
all 500 concurrent registrants were seated in a 50-seat section in 5/5
trials. Both columns of all four benchmarks reproduce from a fresh clone.
```

**What is deliberately *not* in it:**

- **No throughput or latency number.** We never optimised for either, and
  a number you did not optimise against invites "so what did you do with
  that?"
- **No line count, no endpoint count.** Neither is evidence of anything.
- **"Three independent serialization points" carries the weight**, and it
  is the phrase to expand on when asked. It is also the honest headline:
  the interesting result is that there were three, not that any one of
  them was hard.
- **"Building each build broken first" is in the bullet on purpose.** It
  is the sentence most likely to earn a follow-up, and the follow-up is
  the strongest thing we have to say.

---

## 6. What this deadline changed

### 6.1 B's countersignature on Deadlines 8 and 9 — given, with one condition

B re-ran the artifacts rather than reading them, which is the only form a
countersignature can honestly take:

``` text
Deadline 8   error-code audit          re-derived from the AST; 15/15, no drift
Deadline 9   clean-room numbers        all four benchmarks re-run from a
                                       REAL git clone (§6.2), both columns
```

**Signed, with one exception.** Benchmark 1's broken column does not
reproduce as written. The README recorded `enrolled_count` landing
between **14 and 21**; the clone run gave **15 to 26**. Same shape, same
5/5 oversold, same 500-of-500 seated in every trial — a wider spread on a
number that was published as a range of fact from a single run.

Corrected in the README rather than argued with. It is the third time
this project has published a number from one run as though it were a
property, and the rule from Deadline 9 already covered it: *a number goes
in the README only from a run of the exact command the README prints* —
to which this adds **and a range needs more than one run before it is
written as a range.**

### 6.2 The Deadline 9 qualification is closed: a real clone, not a fresh tree

Session 20's clean room was built from the exact 87-file set a clone
would receive, assembled from `git ls-files` — because the tree was still
uncommitted. The content was verified; the `git clone` was not. That
qualification is now discharged. `git clone` into a scratch directory,
`cp .env.example .env`, own `JWT_SECRET`, `docker compose up -d --build`
against a **new** volume:

``` text
git clone                87 files, README present, scripts/_db.py present
alembic upgrade head     5 revisions -> 1ca8b85b7626
scripts/seed.py          exit 0; 3 users, 2 clusters, 2 rooms, 1 offering,
                         8 quota rows
9 gate scripts           ALL exit 0
pytest tests/ -v         6 passed, 0 skipped, asyncio-0.25.0, mode=STRICT

benchmark 1 FIXED        0/5 oversold, 0/5 counter, exactly 50 every trial,
                         peak DB concurrency 39 of 40
benchmark 2 FIXED        0/25 over-quota, held {2: 25}, 0 transport errors
benchmark 3              no key {8: 15}, with key {1: 15}, 1 key row,
                         {201: 120}, divergent bodies 0/15
benchmark 4 FIXED        2v3 {2: 15}/{(2,2): 15};  8v3 {3: 15}/{(3,3): 15}
                         column 3: returned in 0.06s against a 5s hold

benchmark 1 BROKEN       5/5 oversold, 500 of 500 seated EVERY trial,
                         enrolled_count 15-26     <- README said 14-21
benchmark 2 BROKEN       25/25 over-quota, held {4: 25}
benchmark 4 BROKEN       2v3 {2: 15}/{(2,2): 15} -- PASSES, as documented
                         8v3 {3: 15}/{(7,3): 15}, counter disagrees 15/15
```

**Nineteen of twenty published numbers reproduced exactly**, from a clone
with nothing cached, nothing pre-migrated and no arguments remembered.
The twentieth is §6.1. The README's sentence *"was run end to end from a
fresh clone"* was an overclaim when it was written and is true now.

### 6.3 What ships open, stated rather than tidied away

1. **Item 11 — the stale locked read on the GPU path.** Open since
   session 10, the oldest item in the project, A's. The two-session probe
   says the `FOR UPDATE` read is stale; the 12-racer race says the
   capacity invariant holds without `populate_existing()`. Both were run
   more than once. **They cannot both mean what they appear to**, and we
   never established which one is misleading us. The keyword stays,
   because relying on a read a probe says is wrong to protect an
   invariant that happens to hold is not a position we can defend. It
   closes unresolved and it is written down.
2. **`POST /courses` / `POST /offerings` do not exist**, belong to no
   deadline, and are a gap rather than a decision (§3, Q4).
3. **Items 7, 9 and 10 were ratified on paper *after* the code was built
   against them.** Both positions were written and both agree, so
   ratification was confirmation rather than decision — but it happened
   in the wrong order and the work log says so at each point.

### 6.4 The finding, if this exercise produced one

**Each of us was wrong about a mechanism, and right about every mechanism
we had personally measured.**

A found `populate_existing()` by losing a deadline to it, and explained
it perfectly. A had also read `_promote_one` closely enough to find a
reachable bug in it — and still credited the offering lock for what
`SKIP LOCKED` does, which only Benchmark 4 can tell you. B had raced the
GPU transaction from the outside in four benchmarks and reconstructed all
four steps and their independence; B learned auth from a document and
presented a decision that document had already reversed.

Reading code teaches you what it says. Measuring it is what tells you
which part is load-bearing, and the two are not close. **The corrections
in §1 and §2 are the deliverable of this deadline** — not the
presentations.
