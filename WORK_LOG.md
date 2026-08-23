# Work Log

A running record of **what happened, when, and by whom.**

Not a duplicate of `DECISIONS.md` — the two files answer different
questions, and keeping them separate is what stops both from rotting:

| File | Answers | Written when |
|---|---|---|
| `DECISIONS.md` | *why* the system is shaped this way | a decision is made or reversed |
| `WORK_LOG.md` | *what* we did in a given session, and what it cost | end of each working session |
| `EXECUTION_PLAN.md` | what must be true to call a **Deadline** met | the plan changes |
| `ARCHITECTURE_AND_WORKFLOWS.md` | what the system *is*, right now | the models or endpoints change |

Rule: if an entry here starts explaining *why*, it belongs in
`DECISIONS.md` and this entry should link to it instead.

Entries are chronological — oldest first, newest appended at the bottom.

## Sessions are not Deadlines

**A session is one sitting. A Deadline is a milestone in
`EXECUTION_PLAN.md`.** They are numbered separately and on purpose.

This file previously numbered its entries "Day 1, Day 2, Day 3", which
silently implied three deadlines had been met. They had not: sessions 2,
3 and 4 were all still finishing **Deadline 1's** checkpoints — the
schema items, the documentation sync, and the clean-DB migration run.
Nothing from Deadline 2 has been started.

So every entry names the Deadline it advanced, and says whether that
Deadline is now met. A deadline may take several sessions; a session may
touch more than one deadline; neither implies the other.

**As of session 12 (2026-08-24): Deadlines 1-5 MET. Deadline 6 next.**

It took four sessions to meet the first of ten deadlines. That is the
number worth looking at, and it was invisible while sessions and
deadlines shared a numbering scheme.

---

## Session 1 — 2026-08-15 — JOINT

**Advances:** Deadline 1 (Foundation)

**Plan:** foundation. Repo, Docker, all models, first migration, frozen
interfaces, agreed error codes. The one deadline the plan says not to
parallelize.

**Shipped**

- FastAPI skeleton, Dockerfile, `docker-compose.yml` (Postgres 16 + app),
  `/health` and `/health/db`
- `app/database/session.py` with the three deliberate session settings
  (`autocommit=False`, `autoflush=False`, `expire_on_commit=False`) and a
  `get_db()` that does **not** commit
- All 12 models in one sitting, including `RoleQuota`, `IdempotencyKey`,
  `WaitlistEntry`, `CourseOffering`
- Migrations `705b757e5df2` (initial schema) and `e0fbfe421403` (GiST
  exclusion constraint for room overlap)
- Frozen `get_current_user` / `require_role` signatures, stubbed to a
  hardcoded ADMIN so B is unblocked
- Agreed error-code table (401 / 403 / 409 `CAPACITY_EXHAUSTED` /
  409 `QUOTA_EXCEEDED` / 422 `IDEMPOTENCY_KEY_REUSED`)
- Ownership settled: Alembic is A's exclusively; `models/` needs both
  people present from here on

**Cost incurred**

- ~1 hour lost: `enum.py` committed empty and `resource.py` never
  committed, so `app.models` could not import. See Incidents in
  `DECISIONS.md`.
- Port 5432 held by containers from an earlier project directory.

**Carried forward:** 7 outstanding schema items, listed in `DECISIONS.md`.
**Deadline 1 status:** not met — the outstanding schema items are part of
it, and the clean-DB checkpoint had only been tested incrementally.

---

## Session 2 — 2026-08-17 — A (solo)

**Advances:** Deadline 1 (schema items carried forward), *not* Deadline 2

**Plan as written:** A takes auth (Argon2, JWT, register/login); B takes
the seed script and read endpoints — i.e. Deadline 2. What actually
happened was Deadline 1 cleanup, which is why the numbering here stopped
matching the plan.

**Actually spent on schema amendments and Deadline 1 verification**, before any
auth code. Three revisions plus a fix to Deadline 1's migration.

**Shipped — `268c10da1da4`, course capacity moves to the offering**

- `capacity` moved `courses` → `course_offerings`. Seats belong to a
  section, not to a catalogue entry.
- `instructor_id` → `users.id` added to `course_offerings`, `NOT NULL`,
  indexed.
- `enrolled_count` added — the counter course registration locks, the
  direct analogue of `GPUCluster.allocated`.
- Two CHECKs: `offering_capacity_positive`, `offering_enrollment_sane`.

**Shipped — `c86676652ca2`, waitlist FIFO by `created_at`**

- `waitlist_entries.position` dropped. Order is `ORDER BY created_at, id`.
  A promotion now touches one row instead of renumbering *n*.
- `waitlist_unique (student_id, course_offering_id)` added — closes
  outstanding item 1, and the deferrable constraint that item asked for
  turned out to be unnecessary rather than merely absent.
- `ix_waitlist_entries_offering_created` for the promotion query.

**Shipped — `1ca8b85b7626`, schema hygiene (outstanding items 2–5)**

- All `created_at` → `timestamptz` (five tables — item 4 listed four;
  `idempotency_keys` was missed).
- `users.created_at` added.
- `courses.code` now UNIQUE.
- `ix_gpu_reservations_user_status`, `ix_enrollments_offering_status`,
  `ix_reservations_user_id`.

**Fixed — Deadline 1's migration chain could not be re-run from empty**

Deadline 1's "verify on a clean DB" checkpoint had only ever been tested
incrementally. `downgrade base` → `upgrade head` failed on
`type "resource_type_enum" already exists`. Two bugs in `705b757e5df2`:
enum types survived `downgrade base` (`op.drop_table` does not drop them),
and the room exclusion constraint was duplicated in two revisions. Both
fixed; details in `DECISIONS.md`.

**Verification run**

```
downgrade base → upgrade head, twice (5 revisions each way)   clean
alembic check                                                 no drift
EXPLAIN quota SUM      → Index Scan ix_gpu_reservations_user_status
EXPLAIN roster count   → Index Only Scan ix_enrollments_offering_status
waitlist FIFO, 3 transactions → correct order
duplicate waitlist join, duplicate course code → both rejected
/health, /health/db, /docs                                    200
12 models, 13 base tables, test rows cleaned up to zero
```

**Docs synced:** `ARCHITECTURE_AND_WORKFLOWS.md` data model, the GPU
request body (still showed `start_time`/`end_time` — the exact stale line
Deadline 1 flagged as an ACTION), and Deadline 7's block in `EXECUTION_PLAN.md`, which
still described renumbering positions.

**Deadline 1 status:** still not met. All five carried schema items are
verified, but the "migration applies cleanly on a clean DB **on both
machines**" checkpoint is only done on B's. That is item 0 below and it
is what session 4 closes.

**Open / carried forward**

1. **B has not seen any of this.** `models/` changed solo, which the
   shared-file protocol does not allow after Deadline 1. B's Deadline 2 column is
   exactly what these changes break: `capacity` is no longer on `courses`,
   `instructor_id` is `NOT NULL`, `courses.code` is UNIQUE.
2. Outstanding items 6 and 7 are still open — decisions, not code, and
   both need B. Item 7 blocks Deadline 7 promotion.
3. `Resource` sets `polymorphic_identity = ResourceType.COURSE` on the
   base class, so a bare `Resource()` is typed COURSE while courses never
   appear in `resources`. Same confusion as item 6.
4. A's Deadline 2 auth work not started: `core/security.py` → `auth/schemas.py`
   → `auth/service.py` → `auth/router.py`.
5. `tests/` is empty and `pytest-asyncio` is not in `requirements.txt` —
   Deadline 5's harness has nothing to build on.

---

## Session 3 — 2026-08-17 — B

**Advances:** Deadline 1 (documentation reconciled to the schema), *not*
Deadline 2

**Plan as written:** B's Deadline 2 column (seed script, read endpoints).
**Actually spent on reconciling the documentation against A's three
schema revisions**,
which is open item 1 from the entry above — "B has not seen any of this."

**Shipped — documentation only. No code, no schema, no migration.**

- `INIT_PLAN.md` synced to the schema at head. Its §11 data model was the
  pre-amendment design: GPU `start_time`/`end_time`, `Course.capacity`,
  `waitlist_entries.position`, and the `location`/`room_number`/`gpu_type`
  columns — every one of which the project has deliberately removed. It
  also predated the exactly-once guarantee entirely. Divergences are now
  marked inline with `CHANGED:` and a revision pointer.
- `ARCHITECTURE_AND_WORKFLOWS.md`: closed the
  `location`/`room_number`/`gpu_type` gap the doc itself flagged as "do
  not leave open"; recorded the dropped-enrollment-row trap next to the
  unique constraint; moved Workflow D onto an offering-keyed route.
- `EXECUTION_PLAN.md`: Locust marked cut in the scope block (it was only
  marked in the cut order); Deadline 3 no longer asks A for a migration that
  shipped on Deadline 1; Deadline 2 seed now says capacity and `instructor_id` go
  on the offering, and warns about `func.now()` on seeded waitlist rows.
- `DECISIONS.md`: document precedence written down; the course-vs-offering
  API decision recorded; open items 6 and 7 restated as still open.
- `DECISIONS.md` and this file were **not** rewritten to match today's
  schema. They are append-only records; only resolution markers added.

**Decision recorded: course write paths are keyed on the offering.**
`POST /courses/{id}/register` is not implementable as specified now that
`enrolled_count` lives on `course_offerings` — one course has many
offerings, so there is no single row to lock. Registration, drop, and
waitlist move to `/offerings/{id}/...`; catalogue reads stay on
`/courses`. This lands in B's Deadline 4 column.

**Also shipped — Deadline 1's last checkpoint, actually closed on B's machine**

The database on B's machine had **zero tables and no `alembic_version`
row** — the chain had never been applied to this volume. Both containers
were up and healthy the whole time. Applied all five revisions from
empty, which is the clean-DB test Deadline 1 asked for and the only version of
it that proves anything.

**Verification run**

```
alembic upgrade head        -> 5 revisions applied from base, clean
alembic current             -> 1ca8b85b7626 (head)
alembic check               -> No new upgrade operations detected
information_schema.tables   -> 13 (12 + alembic_version)
Base.metadata               -> 12 tables
pg_extension                -> btree_gist present
pg_constraint               -> 9 non-FK constraints, all present:
                               no_overlapping_room_reservations (type x)
                               gpu_capacity_sane, room_capacity_positive,
                               offering_capacity_positive,
                               offering_enrollment_sane,
                               enrollment_unique, waitlist_unique,
                               idempotency_key_user_unique, role_quota_unique
created_at (6 tables)       -> all `timestamp with time zone`
indexes                     -> ix_gpu_reservations_user_status,
                               ix_enrollments_offering_status,
                               ix_waitlist_entries_offering_created,
                               ix_reservations_user_id, ix_courses_code,
                               ix_course_offerings_instructor_id
/health, /health/db         -> 200, both ok
argon2 / jose / multipart   -> import OK in the container
```

No test rows inserted, so the database is at head and empty — the state
the Deadline 2 seed script expects.

**Cost incurred**

- Deadline 2's actual B column (seed script, read endpoints) is still not
  started. Both people are now behind their Deadline 2 plan.

**Open / carried forward**

0. **A must run `alembic upgrade head` from empty on their machine too.**
   Deadline 1 says "verify on a clean DB on BOTH machines"; only B's is
   verified. Likely fine — A wrote the revisions — but the checkpoint is
   not met on assertion.
1. Items 6 and 7 unchanged and still joint calls; item 7 blocks Deadline 7.
2. `Resource.polymorphic_identity = ResourceType.COURSE` on the base
   class — needs both people, since `models/` is frozen.
3. `tests/` still absent, `pytest-asyncio` still not in
   `requirements.txt`. Deadline 5 has nothing to build on.
4. `python-jose==3.3.0` carries known advisories and A is about to write
   `core/security.py` against it. Worth switching to `PyJWT` before the
   code exists rather than after.
5. A's Deadline 2 auth work still not started.

---

## Session 4 — 2026-08-18 — A (solo)

**Advances:** Deadline 1 — and **closes it**. Also *unblocks* Deadline 2
(the `python-jose` → PyJWT swap and `scripts/check_jwt.py`), but ships
none of it: Deadline 2 is register/login, and the API still has exactly
two routes, `/health` and `/health/db`.

A session touching two deadlines is the case the numbering split exists
to record — the first draft of this entry claimed Deadline 1 only, which
would have buried the swap under the wrong milestone.

**Plan:** clear the two items blocking `core/security.py` — the clean-DB
checkpoint on A's machine (carried item 0) and the `python-jose` swap
(carried item 4) — before any auth code exists to be written against them.

**Shipped**

- Clean-DB checkpoint met on A's machine. `downgrade base` →
  `upgrade head`, **twice**, then `alembic current` and `alembic check`.
  Carried item 0 closed; "verify on a clean DB on BOTH machines" is now
  actually true rather than asserted.
- `python-jose[cryptography]==3.3.0` → `PyJWT==2.10.1` in
  `requirements.txt`, image rebuilt. Carried item 4 closed. Nothing
  imported `jose`, so this was a one-line change with no call sites to
  migrate — which was the entire point of doing it before `security.py`.
- `scripts/check_jwt.py` — 9 assertions covering the swap, exits
  non-zero on failure so it is a gate, not a paragraph.
- **Docs renamed off the calendar: Deadlines, not days.** `10_DAY_PLAN.md`
  → `EXECUTION_PLAN.md`, `DAILY_LOG.md` → `WORK_LOG.md` (both `git mv`),
  and every "Day N" milestone is now "Deadline N" across all five docs.
  This log's entries became **Sessions**, each naming the Deadline it
  advanced. Reasoning in `DECISIONS.md`; the short version is that
  sessions and deadlines shared one numbering scheme, so three log
  entries read as three deadlines met when only Deadline 1 was in play.

**Verification run**

```
alembic downgrade base      -> 5 revisions down, both rounds
alembic upgrade head        -> 5 revisions up, both rounds
alembic current             -> 1ca8b85b7626 (head)
alembic check               -> No new upgrade operations detected.

state at base               -> only alembic_version remains
                            -> zero leftover enum types (the session-2 bug
                               that broke the chain stays fixed)
                            -> btree_gist still installed; downgrade does
                               not drop it, and CREATE EXTENSION IF NOT
                               EXISTS makes the re-upgrade idempotent

pip show python-jose        -> not found
pip show cryptography       -> not found (jose was the only thing pulling it)
python -c "import jose"     -> ModuleNotFoundError
pip show PyJWT              -> 2.10.1

scripts/check_jwt.py        -> 9 checks, all pass, exit 0
  encode                    -> str
  decode                    -> {'sub': '1', 'role': 'ADMIN', 'exp', 'iat'}
  wrong secret              -> InvalidSignatureError
  expired token             -> ExpiredSignatureError
  {'sub': 1} at encode      -> accepted silently  <- the trap
  {'sub': 1} at decode      -> InvalidSubjectError: Subject must be a string

/health, /health/db         -> 200, both ok after rebuild
alembic current             -> 1ca8b85b7626 (head)
```

Writing the check as a script rather than pasting into a shell paid for
itself immediately: the first version asserted that an int `sub` fails at
*encode*, and it does not — it fails at decode, which is a much worse
failure mode (login succeeds, every later request 401s). A one-off paste
would have confirmed the wrong belief and moved on.

**Cost incurred**

- None material. `docker compose build` fails from Git Bash with
  `docker-credential-desktop: executable file not found`; runs fine from
  PowerShell. Environment quirk, not a project problem — noted so it is
  not re-debugged.

**Deadline 1 status: MET.** Every checkpoint now verified on both
machines: `docker compose up` works, `/docs` loads, and the migration
applies cleanly from empty for both people. Four sessions, one deadline.

**Open / carried forward**

1. Items 6 and 7 unchanged and still joint calls; item 7 blocks Deadline 7.
2. `Resource.polymorphic_identity = ResourceType.COURSE` on the base
   class — needs both people, since `models/` is frozen.
3. `tests/` still absent, `pytest-asyncio` still not in
   `requirements.txt`. Deadline 5 has nothing to build on.
4. **Deadline 2 has not been started by either person.** A's auth column
   (`core/security.py` → `auth/schemas.py` → `auth/service.py` →
   `auth/router.py`) is unblocked. B's seed script and read endpoints are
   untouched. This is the whole of Deadline 2 and it is all still ahead.

---

## Session 5 — 2026-08-20 — B

**Advances:** Deadline 2 — the **first** work either person has done on
it. Does **not** close it: Deadline 2 is auth *and* read paths, and the
API still has only `/health` and `/health/db`.

**Plan:** B's Deadline 2 column — seed script and read endpoints. Got the
seed script; read endpoints not started.

**Shipped — `scripts/seed.py`**

- 3 users (one per role), 2 GPU clusters, 2 rooms, 1 course + offering,
  8 `RoleQuota` rows. `--reset` truncates first; a bare re-run against a
  non-empty database refuses with exit 1 rather than half-inserting and
  dying on a UNIQUE violation.
- Numbers are chosen by the benchmarks, not arbitrary: offering capacity
  **50** because Benchmark 1 fires 500 concurrent registrations at it;
  **two** clusters with free units because Benchmark 2 needs one student
  issuing concurrent 2-GPU requests at *different* clusters;
  `(STUDENT, GPU) = 2` so the third unit is the quota rejection.
- Passwords hashed with `argon2.PasswordHasher()` directly rather than
  waiting on A's `core/security.py`. argon2 encodes its parameters into
  the hash string, so `verify()` accepts these whatever settings A picks
  — the seed was never actually blocked on A's column.
- `--reset` truncates off `Base.metadata.sorted_tables`, not a hardcoded
  list, so a table added later cannot be silently missed.
  `alembic_version` is not in the metadata, so this resets data and never
  schema. `RESTART IDENTITY` keeps ids reproducible run to run, which the
  benchmarks need because they assert on *which* rows were affected.
- **`(FACULTY, COURSE)` is absent, not NULL.** Course registration is
  STUDENT-only, so the pair is unreachable behind the 403. That makes
  "no row" and "row with `max_units = NULL`" two different things, and
  A's quota helper must not conflate them.

**Fixed — the app image on B's machine predated the PyJWT swap**

`scripts/check_jwt.py` failed on `ModuleNotFoundError: No module named
'jwt'`. The container had been up two days, built before `ec7b074`, and
still had `python-jose 3.3.0` and `cryptography 50.0.0` installed while
`requirements.txt` said `PyJWT==2.10.1`. Rebuilt; the gate now passes 9/9.

A's decision to write that check as a **script rather than a shell paste**
is what caught it, on its first run on a second machine. Worth recording
as a third instance of the same lesson: `docker compose ps` was green and
`/health/db` was returning 200 throughout.

**Shipped — documentation corrections**

- `app/core/dependencies.py`: five "Day N" references → Deadlines; last
  Day-N text anywhere in the code. Also corrected the frozen-interface
  claim — the real `get_current_user` **will** take parameters the stub
  does not (a bearer token and a `Session`). What is frozen is the import
  path, the call shape, and the return type; "bodies only" was a promise
  A could not keep.
- `DECISIONS.md`: two deadline numbers in the capacity-move section were
  wrong (course registration is Deadline 4, not 6; the reconciliation
  query is Deadline 8, not 6). Both read "Day 6" before the rename, so
  the substitution carried them faithfully. Also `response_status` →
  `status_code`, which is the real column name.
- **Outstanding items 8, 9 and 10 added** — see `DECISIONS.md`. All three
  surfaced from reading `INIT_PLAN.md` against the built system.
- `EXECUTION_PLAN.md`: `GET /me` scheduled at Deadline 3, `GET /me/quota`
  at Deadline 6. Both were in two documents with **no deadline assigned**
  — the same gap the PATCH endpoints had. Workflow B opens with
  `/me/quota`, so the flagship demo has been starting on an endpoint
  nobody owned.

**Verification run**

```
docker compose build app     -> PyJWT-2.10.1 installed, jose absent
scripts/check_jwt.py         -> 9/9 PASS, exit 0
alembic current              -> 1ca8b85b7626 (head)
pg_stat_user_tables          -> 12 tables, 0 rows (pre-seed)

seed.py (bare, empty DB)     -> exit 0
seed.py (bare, seeded DB)    -> exit 1, refuses  <- guard works
seed.py --reset              -> exit 0, ids restart at 1

psql verification, not the script's own output:
  resources        -> 1,2 GPU / 3,4 ROOM, all status AVAILABLE
                      (joined-table inheritance wrote both halves and
                       set the discriminator from polymorphic_identity)
  rooms            -> ids 3,4 with building + capacity
  gpu_clusters     -> ids 1,2 -> 8 and 4 units, 0 allocated
  course_offerings -> instructor_id = 2 (the FACULTY user), capacity 50,
                      start/end "09:00"/"10:30" zero-padded
  users.created_at -> tz-aware
  password_hash    -> $argon2id$v=19$m=65536,t=3,p=4$...
  PasswordHasher().verify(hash, 'campus123') -> True
  role_quotas      -> 8 rows, (FACULTY, COURSE) absent as intended
```

**Observed, and it confirms a documented trap:** all three seeded users
share `created_at` to the microsecond, because `func.now()` is
transaction-start time. Harmless for users, since nothing orders them —
and it is exactly why waitlist seeding at Deadline 7 needs separate
commits or explicit timestamps, and why the promotion query carries the
`id` tiebreak.

**Cost incurred**

- The stale image. Not large in wall-clock, but it would have been:
  the seed script would have run fine against it, and the failure would
  have surfaced later as an import error in A's `security.py`, looking
  like A's bug.

**Deadline 2 status: still open.** B's seed script is done; B's four read
endpoints are not started, and A's `auth/` column has not begun. The
checkpoint — *login returns a token containing a role; B can list
resources* — is met by neither half.

**Open / carried forward**

1. **B's read endpoints not started**: `GET /gpus`, `/rooms`, `/courses`,
   `/{id}/availability`. A's auth column not started either.
2. **The API prefix is undecided and B hits it first.** `INIT_PLAN.md`
   §12 and `ARCHITECTURE_AND_WORKFLOWS.md` write every route as
   `/api/v1/...`; `EXECUTION_PLAN.md` writes them bare. `main.py` mounts
   no routers yet, so there is no precedent in code. Pick `/api/v1` and
   tell A before they write `auth/router.py` — retrofitting it later
   touches every route and every benchmark URL.
3. Items 6–10 in `DECISIONS.md` are joint calls. 8 blocks Deadline 4;
   7, 9 and 10 block Deadline 7.
4. `tests/` still absent, `pytest-asyncio` still not in
   `requirements.txt`. Deadline 5 has nothing to build on.
5. **The connection pool will distort Benchmark 1.** `create_engine` in
   `app/database/session.py` takes no pool arguments, so it is on
   SQLAlchemy defaults — `pool_size=5, max_overflow=10`, i.e. **15
   connections**. The harness fires 500. Requests past the 15th block on
   the pool and raise `TimeoutError`, which looks exactly like a
   concurrency bug in the thing being measured, while Postgres sits at
   `max_connections=100` with headroom. `session.py` was written jointly,
   so this needs A rather than a solo edit.

---

## Session 6 — 2026-08-20 — B

**Advances:** Deadline 2. **Completes B's half of it**; the deadline
itself stays open, because the checkpoint is *"login returns a token
containing a role; B can list resources"* and A's `auth/` column has not
started.

**Plan:** the second half of B's Deadline 2 column — the four read
endpoints.

**Shipped — `gpus/`, `rooms/`, `courses/`, mounted in `main.py`**

- Each module split router / service / schemas, per `INIT_PLAN.md` §15:
  routers know HTTP, services know the domain.
- Ten routes, all reads, all authenticated by the Deadline 1 stub — so
  the Deadline 3 swap now has real call sites and needs no change at
  those sites.

**Fixed — the modules existed but nothing mounted them**

`include_router` appeared nowhere in `app/`, so `main.py` still served
only `/health` and `/health/db` and not one of the routes was reachable.
Written code that is not wired in looks identical to finished work from
the outside; this is the same shape as a file that is written but never
committed, and it is the third variant of that failure this project has
hit.

**Decided — the API prefix is `/api/v1`, defined once in `main.py`**

`INIT_PLAN.md` §12 and `ARCHITECTURE_AND_WORKFLOWS.md` write every route
under `/api/v1`; `EXECUTION_PLAN.md` writes them bare. Two documents beat
one, and retrofitting a prefix once `auth/` and the benchmarks exist
would touch every route and every harness URL. **A must mount `auth/`
under the same constant.**

Health checks deliberately stay at the **root**, outside the prefix: they
are infrastructure, not API, and `docker-compose` should not have to
track an API version.

**Design notes worth keeping**

- Both availability schemas carry docstrings stating they are *boundary
  reads, stale on arrival*. `free` and `seats_available` must never gate
  an allocation — the capacity guarantee lives in the transaction, under
  `SELECT ... FOR UPDATE`. Writing that next to the field is cheaper than
  discovering it at Deadline 4.
- Cross-type 404s come free from the polymorphic discriminator: querying
  the `GPUCluster` subclass adds `resource_type = 'GPU'` to the WHERE
  clause, so `/gpus/3` where 3 is a room 404s without a hand-written
  check. The *write* paths get no such help — Deadline 3 still has to
  check `resource_type` in the service layer.
- `rooms/service.py` answers availability with the same `&&` on
  `tstzrange(..., '[)')` filtered to ACTIVE that the exclusion constraint
  uses, so the read is served by the GiST index the constraint already
  created. This closes the re-check that outstanding item 3 deferred:
  *"not EXPLAIN-verified, because that query does not exist yet; re-check
  on Deadline 3 when it does."* It exists now.
- `courses/router.py` carries two routers — `/courses` for catalogue
  reads, `/offerings` for offering-shaped ones — so Deadline 4's
  `POST /offerings/{id}/register` attaches to a router that already
  exists.

**Verification run**

```
openapi.json          -> 10 routes under /api/v1, 2 at root
GET /gpus             -> 2 clusters, 8 and 4 units
GET /gpus/1/availability -> total 8, allocated 0, free 8
GET /rooms            -> 2 rooms
GET /courses          -> CS641
GET /courses/1/offerings -> capacity 50, seats_available 50
GET /offerings/1      -> instructor_id 2 (the FACULTY user)
/gpus/3  (3 is a ROOM)        -> 404
/rooms/1 (1 is a GPU cluster) -> 404
start >= end                  -> 422
missing course                -> 404
/docs                         -> 200
app log                       -> no errors, only reload notices

Room availability cross-checked against the exclusion constraint itself,
because agreeing with it is the endpoint's entire job:

  window [11,13) vs booked [10,12)   endpoint available=false
                                     constraint REJECTS the insert
  window [12,14) adjacent            endpoint available=true
                                     constraint ACCEPTS the insert
  after cancelling the [10,12) hold  endpoint available=true
                                     (constraint is partial on ACTIVE)

test rows deleted; 8 seeded tables back to post-seed counts
```

A note on that cleanup: `pg_stat_user_tables` reported 2 reservations
after the DELETE while `count(*)` reported 0. `n_live_tup` is an estimate
maintained by the stats collector, not a count. Use `count(*)` when the
number matters.

**Cost incurred**

- Docker Desktop was not running at the start of the session; the daemon
  had to be started before anything could be verified.

**Deadline 2 status: still open.** B's column is complete — seed script
and read endpoints, both committed and both verified against a live
database. A's column (`core/security.py` → `auth/schemas.py` →
`auth/service.py` → `auth/router.py`) has not started, so the checkpoint
is half-met.

**Open / carried forward**

1. **A's Deadline 2 auth column, not started.** It is now the only thing
   between the project and Deadline 2.
2. **A must mount `auth/` under `API_PREFIX`** from `main.py`. This is
   new since the last entry and is the one coordination point B created.
3. Items 6–10 in `DECISIONS.md` are joint calls. 8 blocks Deadline 4;
   7, 9 and 10 block Deadline 7.
4. `tests/` still absent, `pytest-asyncio` still not in
   `requirements.txt`. Deadline 5 has nothing to build on.
5. The connection pool still defaults to 15 against a 500-request
   harness — see session 5, item 5. `session.py` needs A.

---

## Session 7 — 2026-08-20 — A

**Advances:** Deadline 2 — and **closes it.** A's auth column was the
only thing left in it after session 6.

**Plan:** `core/security.py` → `auth/schemas.py` → `auth/service.py` →
`auth/router.py`, in that order, mounted under B's `API_PREFIX`. All of
it shipped, plus one file the split did not anticipate.

**Shipped — `core/security.py`**

- Argon2 hashing and HS256 encode/decode. Imports only `config` — no
  models, no session, no FastAPI — which is why it was written first and
  why nothing in it can grow a hidden dependency on a request.
- Raises no HTTP errors. `decode_access_token` lets
  `jwt.InvalidTokenError` propagate; turning that into a 401 is Deadline
  3's job in `dependencies.py`.
- `str(user_id)` on the way out, per the PyJWT ≥ 2.10 finding from
  session 4.

**Shipped — `auth/`, mounted at `/api/v1/auth`**

- `POST /register` → 201, `POST /login` → token carrying `role`.
- 12 routes under `/api/v1` now (B's 10 + these 2), 2 at root.
- **`A must mount auth/ under API_PREFIX`, session 6's one carried
  coordination item, is closed.**

**Shipped — `core/errors.py`, which was not in the plan**

Deadline 1 agreed the error *codes* and never the JSON they arrive in.
Nobody noticed because no endpoint had returned one. Duplicate
registration is the first, so the envelope
(`{"detail": {"code", "message"}}`) is settled now rather than improvised
separately at Deadlines 4 and 5. Reasoning in `DECISIONS.md`.

**Found by running, not by reading — `InvalidHashError` is a `ValueError`**

`VerifyMismatchError` → `VerificationError` → `Argon2Error`, but
`InvalidHashError` → `ValueError`, and is **not** an `Argon2Error`. So
`except Argon2Error` — the clause anyone would write first — catches a
wrong password and lets a malformed `password_hash` escape the login
handler as a 500. Checked the MRO in the container before writing the
`except`, which is the only reason it says
`(VerificationError, InvalidHashError)`.

Third instance of the same lesson, after the enum-drop bug and the PyJWT
`sub` trap: **the hierarchy you assume is not the one the library has.**

**Verification run**

```
alembic current              -> 1ca8b85b7626 (head)   [run first, per DECISIONS.md]
seed.py                      -> this machine's DB was at head and EMPTY
                                (B seeded theirs in session 5; seeding is
                                 per-volume, and nothing had said so)

scripts/check_auth.py        -> 25/25 PASS, exit 0
  register                   -> 201, role echoed, password_hash absent
  duplicate email            -> 409 {"code": "EMAIL_ALREADY_REGISTERED"}
  8 SIMULTANEOUS duplicates  -> 1 x 201, 7 x 409, zero 500s
                                one row in the database   <- see audit below
  password < 8 / bad role    -> 422
  login                      -> 200, token_type bearer
  token claims               -> {'sub': '1', 'role': 'STUDENT', 'iat', 'exp'}
  sub is a str               -> '4'         <- the PyJWT 2.10 trap
  wrong password             -> 401
  unknown email              -> 401, byte-identical body
  JSON body to /login        -> 422         <- documents the form contract
  seeded student / faculty   -> log in, correct role in claim

scripts/check_jwt.py         -> 9/9 PASS, exit 0 (regression, still clean)

psql, not the script's own output:
  password_hash    -> $argon2id$v=19$m=65536,t=3,p=4$..., 97 chars
  created_at       -> timestamp with time zone
  users            -> back to 3 after cleanup

security.py directly, no HTTP:
  round-trip       -> {'sub': '42', 'role': 'FACULTY', 'iat', 'exp'}
  tampered token   -> InvalidSignatureError
  verify_password vs garbage hash -> False, no 500  <- the ValueError above
  verify_password vs empty hash   -> False

openapi.json     -> 14 paths: 12 under /api/v1, 2 at root
/docs            -> 200
app log          -> no tracebacks, only reload notices
```

**Observed, and it confirms a documented trap:** the three seeded users
share `created_at` to the microsecond while a registered user gets its
own — `func.now()` is transaction-start time, exactly as session 5
predicted. Visible in real data now rather than in a test fixture.

**Cost incurred**

- None material. The empty database on this machine cost one `seed.py`
  run, and only because seed state is per-volume and no document had
  said so. Now it does.

**Deadline 2 status: MET.** The checkpoint is *"login returns a token
containing a role; B can list resources"* and both halves are verified:
the claim above, and `GET /api/v1/gpus` returning both clusters with that
token in the header.

One honest qualification on that second half: the token is *accepted*,
not yet *verified* — `dependencies.py` is still the Deadline 1 stub and
ignores the header entirely. Making it load-bearing is Deadline 3, which
is precisely what Deadline 3 is for. Recorded so nobody reads the green
result as more than it is; a route that returns 200 with a good token and
also with no token at all has proved only one of those.

Two deadlines met, seven sessions.

**Audit, same session — Deadline 2 re-checked against the plan line by
line, on this machine**

Prompted by "check all the files whether Deadline 2 is met", i.e. not
taking the entry above at its word. Every line of the Deadline 2 block in
`EXECUTION_PLAN.md` was walked against the built system, and B's ten read
routes were re-run **here** rather than trusted from session 6's run on
B's machine — the Deadline 1 lesson about "verified on one machine" is
not specific to migrations.

```
all 14 routes on THIS machine  -> 12 under /api/v1 + 2 at root, all as expected
cross-type 404s                -> /gpus/3 (a room) 404, /rooms/1 (a GPU) 404
availability start >= end      -> 422 ; missing params -> 422
alembic check                  -> No new upgrade operations detected
role_quotas vs the plan's list -> exact match, 8 rows, (FACULTY,COURSE) absent
courses.capacity column        -> 0 rows in information_schema  (it is on the
                                  offering, where the plan says it goes)
offering                       -> instructor_id 2 = the FACULTY user,
                                  capacity 50, "09:00"/"10:30" zero-padded
```

**One requirement was only half-tested, and the audit is what caught
it.** The plan does not merely say duplicate registration returns 409; it
says *"409, not 500: catch the IntegrityError, **because** two
simultaneous registrations can both pass a 'does this email exist?'
check."* The original check registered the same email **twice in a row** —
which passes just as happily against the naive pre-flight check the plan
is warning about. It tested the return code and not the reason for it.

Fired 8 registrations of one address, released together on a barrier:
`1 x 201, 7 x 409, zero 500s`, and **one row** in the database — asserted
by counting rows, not by reading status codes, since those are only what
the server said. Now part of `check_auth.py` rather than a one-off, so it
stays true.

Worth naming as a category, because it will recur at Deadlines 4, 5 and
7: **a test that passes against the broken implementation is not a test
of that requirement.** It is the same argument as `DECISIONS.md`'s "build
the broken version first", arriving from the other direction.

**Open / carried forward**

0. **`JWT_SECRET` in `.env` is still `change-me-in-production`**, the
   literal `.env.example` default. Harmless today — `.env` is gitignored
   and every environment is thrown away — but the README will claim
   JWT-based auth, and a demo signed with a published secret is a
   question nobody wants at Deadline 10. Deadline 9's clean-room test is
   the natural place to fix it, since that run needs a fresh `.env`
   anyway. Also: `.claude/` is untracked and not in `.gitignore`.
1. **Deadline 3 is next**, and A's half is the swap: real
   `get_current_user` (decode → 401 on `InvalidTokenError` → load the
   user) and real `require_role` (→ 403), then delete the stub. The
   import path and call shape B coded against do not change.
   `GET /me` also belongs to A there.
2. `core/errors.py` exists but only one code flows through it so far.
   Deadlines 4 and 5 must use `coded_error()` for `CAPACITY_EXHAUSTED`,
   `QUOTA_EXCEEDED` and `IDEMPOTENCY_KEY_REUSED` rather than
   hand-rolling a second envelope.
3. Items 6–10 in `DECISIONS.md` are joint calls, still open. 8 blocks
   Deadline 4; 7, 9 and 10 block Deadline 7. **Item 9 is the one to
   settle first** — the global lock order and the waitlist promotion
   design contradict each other, and it is A's transaction.
4. `tests/` still absent, `pytest-asyncio` still not in
   `requirements.txt`. Deadline 5 has nothing to build on. Note the two
   `scripts/check_*.py` gates are **not** a substitute: they are
   sequential smoke tests, and Deadline 5 needs concurrency.
5. The connection pool still defaults to 15 against a 500-request
   harness — session 5, item 5. `session.py` needs both people.

## Session 8 — 2026-08-21 — A

**Advances:** Deadline 3 — **A's column, complete.** Does not close the
deadline: the checkpoint is two claims, *"student token on `POST /gpus`
returns 403"* **and** *"overlapping room booking returns 409"*, and the
second is B's room-reservation endpoint, not started.

**Plan:** replace the Deadline 1 stub in `core/dependencies.py` with real
JWT decoding and role enforcement, ship `GET /me`, and prove the swap by
running it rather than by reading it.

**Shipped — `core/dependencies.py`, the stub deleted**

- Real `get_current_user`: decode → 401 on `jwt.InvalidTokenError` →
  `int(sub)` → load the user → 401 if that row is gone. Real
  `require_role`: 403 on a role mismatch, as a dependency so it resolves
  before the handler body.
- **Zero edits at B's ten call sites.** The import path, call shape and
  return type were frozen at Deadline 1 for this moment, and the freeze
  held exactly as designed.
- `OAuth2PasswordBearer` registers the bearer scheme, so `/docs` now has
  a working **Authorize** button — the payoff for login being
  form-encoded, argued at Deadline 2 and only now cashable.

**Shipped — `app/users/`, `GET /me`**

Assigned to Deadline 3 in session 5 after being found in two documents
with no deadline at all. It is the end-to-end proof that a real token
decodes to a real user carrying a real role. Reuses
`auth.schemas.UserRead`; no `users/service.py`, because the domain half
is empty until `GET /me/quota` arrives at Deadline 6.

**Shipped — `POST /gpus` and `POST /rooms`, both ADMIN**

Not in the Deadline 3 column as written, and unavoidable: the checkpoint
names `POST /gpus`, **which did not exist**, and no other admin-only
route did either — so `require_role` had nothing to guard and the
checkpoint could not have been run. Third instance of the
specified-but-unassigned endpoint gap, after `GET /me` and the admin
PATCH endpoints. Reasoning in `DECISIONS.md`.

**Shipped — `API_PREFIX` moved to `core/config.py`**

`dependencies.py` needs it for `tokenUrl`, and importing `main.py` from a
dependency is a cycle. Value and reasoning unchanged from session 6; one
place to change it, as before.

**Verification run**

```
alembic current              -> 1ca8b85b7626 (head)   [run first, per DECISIONS.md]
alembic check                -> No new upgrade operations detected
routes                       -> 21 total: 15 under /api/v1, 2 at root, 4 from FastAPI

scripts/check_rbac.py        -> 34/34 PASS, exit 0
  no token / garbage / wrong signature / expired
    / int sub / signed token for a nonexistent user
                             -> 401 x 6   <- ALL SIX returned 200 yesterday
  401 carries WWW-Authenticate: Bearer
  student token              -> 200, both seeded clusters
  GET /me                    -> the seeded student, role STUDENT,
                                no password_hash field
  GET /me, no token          -> 401
  POST /gpus, no token       -> 401, NOT 403
  POST /gpus, student        -> 403   <- THE CHECKPOINT
  POST /gpus, faculty        -> 403
  POST /rooms, student       -> 403
  cluster/room counts across the 403s -> 2 -> 2, 2 -> 2
                                <- the 403 fired BEFORE the handler body;
                                   status codes cannot show this
  ADMIN-claim token on the STUDENT row -> 403, nothing created
                                <- the role is read from the DB, not the claim
  POST /gpus, admin          -> 201, allocated = 0, readable at GET /gpus/{id}
  gpu_count = 0, admin       -> 422
  gpu_count = 0, student     -> 403, not 422  <- authn -> authz -> validation
  openapi securitySchemes    -> OAuth2PasswordBearer,
                                tokenUrl api/v1/auth/login,
                                declared on protected routes

scripts/check_auth.py        -> 25/25 PASS, exit 0 (regression, still clean)
scripts/check_jwt.py         ->  9/9  PASS, exit 0 (regression, still clean)

psql, not the scripts' own output:
  resources 4, gpu_clusters 2, rooms 2, users 3, role_quotas 8
                                <- post-seed counts exactly; deleting the
                                   created cluster through the subclass took
                                   its `resources` row with it, leaving no
                                   orphan typed row behind
app log                      -> no tracebacks, only request lines
```

**Observed, and it is the point of the whole deadline:** the six 401
assertions above are the ones that describe what changed. Every one of
them was a **200** against the build that passed Deadline 2's checkpoint.
The token was accepted, not verified; it is verified now.

**Cost incurred**

- Docker Desktop was not running at session start (second time — session
  6 lost time the same way). It is not installed under
  `C:\Program Files\Docker`; the executable is at
  `%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe`. Noted so
  it is not re-searched.
- One self-inflicted near-miss: a file written through Python's
  `write_text` on Windows picked up **cp1252**, putting a raw `0xA7`
  byte where a `§` was meant. Python source is read as UTF-8, so that
  file would have failed to import — a syntax error with nothing
  syntactically wrong in it. Caught by running `iconv -f UTF-8` over
  every touched file before starting the stack, not by reading them.
  Write with an explicit encoding, or stay in ASCII.

**Deadline 3 status: still open.** A's column is complete and verified;
B's column — `POST /rooms/{id}/reservations`, the `resource_type` check
in the service layer, the `resources.status` gate, and the adjacent-
interval test — is not started. Half the checkpoint is met.

**Open / carried forward**

1. **B's Deadline 3 column is the only thing between the project and
   Deadline 3.** Note the `resources.status` gate in it depends on
   outstanding item 6, which is still unratified — the proposal is
   written up in `EXECUTION_PLAN.md` at Deadline 4 (blocking stops new
   allocations, does not evict, distinct code `RESOURCE_BLOCKED`) and
   needs a yes before the endpoint is written, because adding it after
   the Deadline 8 freeze means reopening a frozen transaction.
2. `GET /me/quota` (Deadline 6) and the admin `PATCH` endpoints
   (Deadline 6) are the remaining half of the endpoint set that had no
   deadline. `POST /courses` and `POST /offerings` — `[ADMIN | FACULTY]`
   in `INIT_PLAN.md` §12 — are **a fourth instance of the same gap** and
   still belong to no deadline. Worth assigning before a checkpoint trips
   over them the way this one did.
3. Items 6–10 in `DECISIONS.md` are joint calls. 6 blocks B's half of
   Deadline 3; 8 blocks Deadline 4; 7, 9 and 10 block Deadline 7. **Item
   9 remains the one to settle first** — the global lock order and the
   waitlist promotion design contradict each other, and it is A's
   transaction.
4. `tests/` still absent, `pytest-asyncio` still not in
   `requirements.txt`. Deadline 5 has nothing to build on. The three
   `scripts/check_*.py` gates are not a substitute: `check_auth.py` fires
   8 simultaneous registrations with threads, which is real concurrency,
   but they are smoke tests against a running server and Deadline 5 needs
   a harness that reports collected status codes.
5. The connection pool still defaults to 15 against a 500-request
   harness — session 5, item 5. `session.py` needs both people.
6. `JWT_SECRET` in `.env` is still `change-me-in-production` — session 7,
   item 0. Now slightly more load-bearing than it was: tokens are
   actually verified against it, so a published secret is a forgeable
   admin token rather than a theoretical one. Still fine for a
   throwaway environment; still due at Deadline 9's clean-room run.

## Session 9 — 2026-08-21 — B

**Advances:** Deadline 3 — and **closes it.** B's room reservation
endpoint was the only thing left after session 8.

Ran straight on from session 8 in the same sitting, logged separately
because the columns are separate and the two halves fail differently: A's
half is a boundary that must reject, B's half is a transaction that must
not double-book.

**Ratified first — outstanding item 6, open since Deadline 1**

`resources.status = BLOCKED` stops **new** allocations, does **not**
evict existing ones (same rule as the capacity reduction in
`ARCHITECTURE_AND_WORKFLOWS.md` §13), and carries its own code
`409 RESOURCE_BLOCKED` because the remedy differs from both existing
409s. Checked inside the transaction against the row just locked, never
at the boundary — `status` is mutable, so an admin can flip it between a
boundary read and the write. As proposed in `EXECUTION_PLAN.md`; the
proposal had been sitting unratified for five sessions and this endpoint
is the first code that reads the flag.

**Shipped — `POST /rooms/{id}/reservations`**

- Any authenticated role, per the role matrix — `get_current_user`, not
  `require_role`. How many a caller may hold is a *quota*, a different
  question answered at Deadline 6 under the user lock.
- The `resource_type` check in the service layer. `reservations.
  resource_id` is an FK to `resources`, and a GPU cluster **is** a
  resource, so nothing in the database stops a "room booking" naming a
  cluster. Read paths get this free from the polymorphic discriminator;
  write paths do not.
- The BLOCKED gate, per the ratification above.
- No pre-flight "is this slot free?" SELECT. The exclusion constraint is
  what makes the second INSERT impossible, so the INSERT is the check —
  the same shape as duplicate registration at Deadline 2.
- `_is_overlap_violation()` reads the **constraint name** from psycopg's
  diagnostics and re-raises anything else, so an unrelated FK bug is not
  reported to the caller as a booking conflict.
- `INTERVAL_CONFLICT` added to the coded errors. Two 409s now share this
  endpoint with different remedies (pick another slot / pick another
  room), which is exactly what the envelope was created for.

**Changed while writing it — `FOR SHARE`, not `FOR UPDATE`**

The status gate was first written with `FOR UPDATE`, following the sketch
in `EXECUTION_PLAN.md`. That sketch is correct **for the GPU
transaction**, which writes the row it locks. The room transaction never
writes it — rooms have no counter — so an exclusive lock buys nothing the
gate needs and quietly serializes every booking of one room behind every
other, making the application lock rather than the constraint the thing
that decides a concurrent slot race.

Caught by writing the router docstring, which claimed no application lock
was involved in the overlap invariant, and noticing that by then one was.
The fix was to make the lock match the claim rather than soften the
claim. Reasoning in `DECISIONS.md`.

**Verification run**

```
routes                       -> 22 total: 16 under /api/v1, 2 at root

scripts/check_rooms.py       -> 34/34 PASS, exit 0
  no token                   -> 401  (the Deadline 3 boundary, on a write route)
  student books [10,12)      -> 201, status ACTIVE, user_id = the caller
  overlapping [11,13)        -> 409 INTERVAL_CONFLICT   <- THE CHECKPOINT
  inner / enclosing / identical windows -> 409 x 3
                                <- a hand-written overlap test with one
                                   comparison inverted passes the simple
                                   case and fails these
  adjacent [12,14) and [8,10) -> 201 x 2   <- the whole reason for '[)'
  same slot, different room  -> 201
  8 SIMULTANEOUS identical bookings -> 1 x 201, 7 x 409, zero 500s
                                and ONE row, counted in the database
                                <- every other assertion here passes
                                   against check-then-insert; only this
                                   one does not
  slot of a CANCELLED hold   -> bookable again (constraint is partial)
  booking a GPU cluster via /rooms -> 404, and zero reservation rows
                                      against that cluster
  booking room 999999        -> 404
  BLOCKED room               -> 409 RESOURCE_BLOCKED
  existing hold on it        -> survives   <- blocking does not evict
  ADMIN on a BLOCKED room    -> 409 too    <- BLOCKED is a fact about the
                                              resource, not a permission
  start == end / inverted / missing body -> 422 x 3
  naive datetime             -> 201, and the tz-aware repeat 409s
                                <- proves it was stored as UTC, not
                                   resolved against the session TimeZone
  availability vs reality    -> booked slot false, free slot true

  the lock mechanism itself, two sessions and a 750ms lock_timeout:
    SHARE lock blocks an admin write to the row -> LockNotAvailable
    SHARE lock does NOT block another booker    -> acquired
                                <- both halves matter: FOR UPDATE would
                                   satisfy the first and break the second

scripts/check_rbac.py        -> 34/34 PASS, exit 0 (regression, still clean)
scripts/check_auth.py        -> 25/25 PASS, exit 0 (regression)
scripts/check_jwt.py         ->  9/9  PASS, exit 0 (regression)

psql:  reservations 0, rooms 2, gpu_clusters 2, resources 4
       no room left BLOCKED
alembic current / check      -> 1ca8b85b7626 (head), no drift
```

**Cost incurred**

- None material. No schema change, so no migration: the exclusion
  constraint and `btree_gist` have been in place since revision
  `e0fbfe421403` at Deadline 1, and this endpoint is the first code to
  use either.

**Deadline 3 status: MET.** Both halves of the checkpoint verified on
this machine: *student token on `POST /gpus` returns 403* (session 8,
with the row counts showing it fired before the handler) and *overlapping
room booking returns 409* (above, with eight concurrent bookings landing
exactly one row).

Three deadlines met, nine sessions.

**Open / carried forward**

1. **Deadline 4 is next, and it is the heaviest** — A's GPU transaction
   and B's course registration, both people writing a locking
   transaction for the first time at the same deadline. The plan's
   fallback stands: if A's GPU path is not landing by the halfway mark,
   B stops courses and pairs on it.
2. **Outstanding item 8 blocks B's Deadline 4 column** and is unchanged:
   `DELETE /reservations/{id}` names a row in two tables with separate id
   sequences. Now slightly more concrete than it was — `reservations`
   holds real room rows as of this session, and `gpu_reservations` gets
   its first rows at Deadline 4, which is when the ambiguity becomes
   reachable rather than theoretical.
3. Items 7, 9 and 10 in `DECISIONS.md` remain joint calls and all block
   Deadline 7. **Item 9 is still the one to settle first** — the global
   lock order and the waitlist promotion design contradict each other,
   and it is A's transaction. Note this session added a *third* lock mode
   to the picture (`FOR SHARE` on a row that is only read), which is
   worth having in mind when that order is finally written down: the rule
   is about the order locks are taken in, not about their strength.
4. `POST /courses` and `POST /offerings` — `[ADMIN | FACULTY]` in
   `INIT_PLAN.md` §12 — still belong to no deadline. Fourth instance of
   the specified-but-unassigned gap. B's Deadline 4 column needs an
   offering to register against, and the seed provides exactly one, so
   this is reachable at Deadline 4.
5. `tests/` still absent, `pytest-asyncio` still not in
   `requirements.txt`. Deadline 5 has nothing to build on. Note the four
   `scripts/check_*.py` gates now include two barrier-released
   concurrency cases (8 duplicate registrations, 8 duplicate bookings),
   which is real contention but still threads-in-a-script; Deadline 5
   wants asyncio + httpx firing 500.
6. The connection pool still defaults to 15 against a 500-request
   harness — session 5, item 5. `session.py` needs both people. This is
   now the oldest untouched item on the list.
7. `JWT_SECRET` in `.env` is still `change-me-in-production` — session 7,
   item 0. Due at Deadline 9's clean-room run.

## Session 10 — 2026-08-23 — JOINT (A's and B's columns)

**Advances:** Deadline 4 — and **closes it.** Both columns, logged as one
entry because the two halves cannot be told apart honestly: the bug that
dominated this session was found in B's course path and fixed in both,
and the benchmark finding came out of A's flagship and rewrote how B's
concurrency assertions are written.

**Plan:** the heaviest deadline. `quotas/` and the GPU transaction (A);
course registration, drop, and the cancel routes (B). No schema change,
so no migration.

**Ratified first — outstanding item 8**, open since session 5.
`DELETE /reservations/{id}` named a row in two tables. The cancel route
now mirrors the POST route on both resources. Reasoning in `DECISIONS.md`.

**Shipped — A's column**

- `app/quotas/service.py`. Helper called from inside another module's
  transaction; opens no transaction, takes no lock, knows no HTTP.
  `limit_for` distinguishes a **missing** policy row from
  `max_units = NULL` and fails closed on the former
  (`409 QUOTA_NOT_CONFIGURED`).
- `POST /gpus/{id}/reservations` — the flagship. User row locked, held
  units SUMmed, quota compared; then the cluster row locked, status
  gated, capacity compared; then counter and reservation written and
  committed together. Lock order in a comment at the top, as the plan
  requires.
- `DELETE /gpus/{id}/reservations/{id}` — owner-or-admin, same lock
  order, locking the **reservation's owner** rather than the caller.
- `BENCHMARK_UNSAFE_NO_USER_LOCK`, default false. See below.

**Shipped — B's column**

- `POST /offerings/{id}/register` — STUDENT only. User row locked (see
  below), offering row locked, capacity gated, enrollment upserted and
  `enrolled_count` bumped in one transaction.
- `DELETE /offerings/{id}/drop` — the fifth specified-but-unassigned
  endpoint, and not optional here: "re-registration is an UPDATE, not an
  INSERT" is untestable without a DROPPED row to re-register over.
- Schedule-overlap check, single-character day codes, zero-padded
  half-open time comparison.
- `DELETE /rooms/{id}/reservations/{id}` — no locks, deliberately, and
  the docstring says why it differs from the GPU twin.

**Departure from the plan, deliberate: registration takes the user lock
now.** The plan sketches it offering-lock-only. But schedule overlap reads
the student's *other* enrollments, and two concurrent registrations for
two clashing offerings share no offering row — nothing would serialize
them. Same failure shape as the cross-cluster GPU race, different
invariant. Deadline 6 now adds the course-load quota inside a lock that is
already held.

**THE FINDING — `SELECT ... FOR UPDATE` returned a stale value**

``` text
20 concurrent registrations, 5 seats:  {201: 20}
enrolled_count = 3        active rows = 20
```

Every request succeeded; the counter said 3. Lost updates. The lock was
acquired, the SQL was right, and the value compared was from **before**
the lock — SQLAlchemy's identity map returns the already-loaded object
without refreshing its attributes, and the 404 check had loaded that row
moments earlier.

Proven in two sessions rather than reasoned about:

```
A reads enrolled_count                 = 0
B commits enrolled_count               = 41
A: SELECT ... FOR UPDATE               = 0    <- lock held, value stale
A: same statement + populate_existing  = 41
raw column value                       = 41
```

Fixed with `populate_existing()` on every locked read, in both modules.

**And the part that is not tidy.** The same probe shows the same
staleness on `GPUCluster` — yet removing `populate_existing()` from the
GPU path and running the 12-racer capacity race **four times** produced a
correct 8/8 every time. The latent read is stale; that request flow does
not hit it; no explanation was established. The keyword stays in both
paths, and this is written up as an open question rather than as a fix
for a bug we proved was biting. Carried forward as item 11.

**THE OTHER FINDING — Benchmark 2, as specified, passes against the
broken build**

Building the unlocked version first, as `DECISIONS.md` requires, produced
`held = 2` on its first run — a pass, against the build it exists to
indict. The corruption window is sub-millisecond and two barrier-released
HTTP requests do not reliably land inside it.

Rerun as a measurement over trials:

| build | over-quota trials | held units |
|---|---|---|
| resource lock only | **24 / 25** | `{2: 1, 4: 24}` |
| + user-row lock | **0 / 25** | `{2: 25}` |

The contrast that makes the point: on that same broken build the
**capacity race passed** — 12 concurrent requests, 8 units, exactly 8
succeeded. The cluster lock was already perfect. The quota rule broke
anyway, because it is a fact about the user and nothing held the user
still.

**Verification run**

```
routes                     -> 27 total: 21 under /api/v1, 2 at root

scripts/check_gpus.py      -> 44/44 PASS, exit 0
  student reserves 2               -> 201, allocated 2
  3rd unit                         -> 409 QUOTA_EXCEEDED   <- CHECKPOINT
  2 more on a DIFFERENT cluster    -> 409 QUOTA_EXCEEDED
  faculty 4 (quota 10) / admin NULL-> 201, 201
  full cluster                     -> 409 CAPACITY_EXHAUSTED, not QUOTA
  over-quota caller, full cluster  -> QUOTA first (gate order)
  room id / missing id via /gpus   -> 404 x 2
  blocked cluster                  -> 409 RESOURCE_BLOCKED, admin too
  cancel wrong cluster id          -> 404      <- item 8, path is load-bearing
  cancel someone else's            -> 403, nothing cancelled
  cancel twice                     -> 200, counter NOT double-decremented
  admin cancels another's hold     -> 200
  BENCHMARK 2, 25 trials           -> 0/25 over quota, held {2: 25}
  capacity race, 12 DISTINCT users -> {201: 8, 409: 4}, allocated 8/8
  allocated == SUM(active)         -> after every phase

scripts/check_courses.py   -> 38/38 PASS, exit 0
  faculty / admin register         -> 403 x 2, nobody enrolled
  duplicate                        -> 409 ALREADY_ENROLLED
  drop / drop twice                -> 200, counter not double-decremented
  re-register after drop           -> 201, SAME enrollment id, one row
  overlapping schedule             -> 409 SCHEDULE_CONFLICT
  adjacent times / different days  -> 201 x 2
  capacity 1, second student       -> 409 CAPACITY_EXHAUSTED
  drop something never registered  -> 409 NOT_ENROLLED
  BENCHMARK 1 (mini) 20 on 5 seats -> {201: 5, 409: 15}, count 5, rows 5
  reconciliation counter == rows   -> 5 == 5   <- Deadline 8 query, early
  schedule race, 15 trials         -> 0/15 double-booked

scripts/check_rooms.py     -> 34/34 PASS   (regression)
scripts/check_rbac.py      -> 34/34 PASS   (regression)
scripts/check_auth.py      -> 25/25 PASS   (regression)
scripts/check_jwt.py       ->  9/9  PASS   (regression)

alembic current / check    -> 1ca8b85b7626 (head), no drift
psql                       -> users 3, gpu_reservations 0, enrollments 0,
                              offerings 1, clusters all allocated = 0
```

**Cost incurred**

- Docker Desktop not running again at session start (third time).
- Two of this session's own tests were wrong, and both are recorded in
  `DECISIONS.md` rather than quietly fixed: a capacity race where all 12
  racers shared one ADMIN account (the user lock serialized them, so the
  cluster gate was never contended and the test could not fail), and a
  schedule assertion that failed for a correct reason the test had not
  accounted for.

**Deadline 4 status: MET.** Checkpoint verified on this machine: *2 GPUs
reserve; a 3rd unit returns `QUOTA_EXCEEDED`* and *duplicate registration
rejected; overlapping courses rejected* — all four, plus the concurrency
evidence the checkpoint does not ask for and which is the only part that
proves the locks do anything.

Four deadlines met, ten sessions.

**Open / carried forward**

1. **NEW, item 11 — why does the GPU path not reproduce the stale locked
   read?** The probe says the read is stale; four runs of the capacity
   race say the request flow never hits it. Unresolved. It matters
   because Deadline 7's promotion transaction is the next locked read
   written, and because the answer determines whether
   `populate_existing()` is a fix or a talisman.
2. **Deadline 5 is next**: A wires idempotency in as step (1) of the GPU
   transaction — the code comment marking its position is already there.
   B builds `tests/concurrency/harness.py`. `tests/` still does not exist
   and `pytest-asyncio` is still not in `requirements.txt`; that is now
   the immediate blocker rather than a standing note.
3. **Benchmark 1 at Deadline 5 must be written as trials, not a single
   run** — see the Benchmark 2 finding. The mini version in
   `check_courses.py` (20 racers, 5 seats) is the shape to scale up, and
   its reconciliation assertion is Deadline 8's query arriving early.
4. Items 7, 9 and 10 remain joint calls and all block Deadline 7. **Item
   9 is now urgent** — the global lock order versus waitlist promotion.
   Two more paths were written against user-first this session, so the
   cost of promotion contradicting it has gone up.
5. `POST /courses` and `POST /offerings` still belong to no deadline —
   the fourth and fifth instances of that gap were `GET /me/quota` and
   `drop`. `check_courses.py` creates offerings by direct insert because
   no endpoint exists.
6. The connection pool still defaults to 15 against a 500-request
   harness — session 5, item 5. **This is now due**: Deadline 5's
   harness is the thing it distorts, and `session.py` needs both people.
7. `JWT_SECRET` in `.env` is still the published default — due at
   Deadline 9's clean-room run.

---

## Session 11 — 2026-08-23 — A

**Advances:** Deadline 5 (idempotency) — A's column only. **Not the
deadline**, which closes on B's harness and Benchmark 1.

**Plan:** build the exactly-once guarantee: `idempotency/` module, wired
in as step (1) of the GPU transaction, with the key and the allocation
committing together.

**Shipped**

- `app/idempotency/service.py` — new. `request_fingerprint`, `claim`,
  `record_response`, and three exceptions. Same contract as `quotas/`:
  takes a Session it did not create, never commits, knows nothing
  about HTTP.
- `app/gpus/service.py` — `reserve_gpu` takes an optional
  `idempotency_key`. Claim at step (1) above both locks;
  `record_response` after `flush()` and **before** the existing commit.
  No second commit was added.
- `app/gpus/router.py` — `Idempotency-Key` header (optional), and three
  new branches: replay via `JSONResponse` carrying the stored status,
  `422 IDEMPOTENCY_KEY_REUSED`, `409 IDEMPOTENCY_IN_PROGRESS`.
- `scripts/check_idempotency.py` — new, sixth gate, 37 assertions.

**No migration.** `alembic check` reports no drift: `idempotency_keys`
and its `UNIQUE(key, user_id)` shipped at Deadline 1. Checked rather
than assumed, which is the only reason a pointless empty revision did
not get generated.

**Verification run**

``` text
scripts/check_idempotency.py -> 37/37 PASS
  no key, twice                    -> 2 reservations, distinct ids
  same key, twice                  -> 1 reservation, 201, identical body
  same key, different body         -> 422 IDEMPOTENCY_KEY_REUSED
  same key, different cluster      -> 422      <- fingerprint covers gpu_id
  same key STRING, different users -> 2 reservations, 2 key rows
  over quota with a key            -> 409, ZERO key rows left behind
  same key after cancelling        -> 201, allocates for real
  BENCHMARK 3, 8 retries x 15      -> {201: 120}, exactly 1 row per trial

BROKEN-VS-FIXED, three builds actually run (not reconstructed):
  correct (savepoint, one commit)   201:120  409:0   500:0   rows 1
  check-then-insert, no savepoint   201:34   409:0   500:86  rows 1
  key commits separately            201:22   409:98  500:0   rows 1

regression after touching the flagship transaction:
  check_jwt / check_auth / check_rbac / check_rooms /
  check_gpus / check_courses        -> all pass
alembic check                       -> no drift, head 1ca8b85b7626
```

**The finding that changes what we claim.** **No build over-allocated —
not one.** `UNIQUE(key, user_id)` holds the row count to exactly one
even in the deliberately broken builds. "Idempotency prevents
double-booking" is therefore *false for this system*, and we were one
README sentence away from asserting it. What the correct implementation
actually buys is that the retry gets the **original response**: without
the savepoint 86 of 120 retries become 500s, with a split commit 98 of
120 become spurious 409s. The corrected claim is in DECISIONS.md and
`ARCHITECTURE_AND_WORKFLOWS.md` §6.

**Two things the plan's Deadline 5 column did not mention**

- **The SAVEPOINT.** A unique violation aborts the whole transaction, so
  the read that fetches the stored response cannot run. `begin_nested()`
  is not a detail — without it the feature does not work under
  concurrency at all.
- **The `Idempotency-Key` header must be optional.** Benchmark 3 is the
  contrast between sending it and not; requiring it would delete one
  column of its own table.

**A doc contradiction, settled.** §7 said a replay returns `200`, §14
said `200/201`. It returns **the stored status** — `201` — because a
replay must be indistinguishable from the original call. Both lines
fixed.

**Deadline 5 status: A's column MET, deadline still OPEN.** The
checkpoint is *"first broken-vs-fixed table with real numbers"* and names
Benchmark 1, which is B's. A's half produced a broken-vs-fixed table with
real numbers; that is not the same as the checkpoint being met.

**Open / carried forward**

1. **B is still blocked and nothing changed there.** `tests/` is still
   empty and `pytest-asyncio` is still absent from `requirements.txt`.
   Verified this session, not assumed.
2. **The connection pool is now the top joint item.** `session.py` sets
   no `pool_size`, so it is 5 + 10 overflow = 15 against a 500-request
   harness. Carried since session 5; Benchmark 1 is the thing it
   distorts. Shared file — needs both people.
3. Item 11 (stale locked read on the GPU path) is **unchanged and
   untouched** this session. Still the thing to resolve before Deadline
   7's promotion transaction.
4. Items 7, 9, 10 remain joint calls blocking Deadline 7. Item 9 (lock
   order vs. promotion) is still the urgent one.
5. `POST /courses` / `POST /offerings` still belong to no deadline.
6. Idempotency is wired into **the GPU path only**. Room and course
   registration do not take a key. That matches the plan — the flagship
   is where exactly-once is claimed — but it should be said out loud in
   the README rather than left for someone to notice.
7. No expiry or cleanup on `idempotency_keys`. Rows accumulate forever.
   Fine for a demo, and a real answer would be a TTL sweep; worth a
   sentence in "Designed, not implemented."

## Session 12 — 2026-08-24 — B

**Advances:** Deadline 5 — and **closes it.** B's harness and Benchmark 1
were the only things left after A's column in session 11.

**Plan:** clear the two blockers carried since session 2, build
`tests/concurrency/harness.py`, and produce Benchmark 1 as a
broken-vs-fixed table.

**Unblocked first, both carried for ten sessions**

- `pytest-asyncio==0.25.0` added to `requirements.txt`; image rebuilt.
- `tests/` and `tests/concurrency/` created, with `pytest.ini` setting
  `asyncio_mode = strict` — **not** `auto`, because under `auto` a test
  that loses its marker is collected as a coroutine that never runs and
  reported as a PASS.

**Shipped — `tests/concurrency/harness.py`**

asyncio + httpx, all calls released on an `asyncio.Barrier`, results
returned in submission order with status, error code and latency. Plus
`DBConcurrencyObserver`, which samples `pg_stat_activity` during a run so
every benchmark reports the concurrency it **achieved** next to the one
it requested.

**Shipped — `tests/concurrency/test_harness.py`, 5 tests**

The instrument gets tested. `test_requests_actually_overlap` asserts wall
time is under half the summed latencies — a harness that awaited each
call in turn passes every status-code assertion in this project and fails
only that one.

**Shipped — `tests/concurrency/benchmark_1_capacity.py`**

``` text
build                      201      409 CAPACITY   enrolled_count   active rows   oversold
--------------------------------------------------------------------------------------------
no offering lock       377 - 500     0 - 123          24 - 50        377 - 500      3/3
+ SELECT ... FOR UPDATE       50           450              50             50      0/3
```

Two trials of the unlocked build seated **all 500 students in a 50-seat
section**, counter reading 34 and 24 against 500 ACTIVE rows. The locked
build sold exactly 50 in all three trials, counter and rows agreeing,
zero 5xx.

**THE FINDING — "500 concurrent" was never achievable, and not because
of tuning**

Firing 500 unbounded against a *correctly sized* pool still gave
`{201: 60, 500: 440}` with 86 s median. Sampled during the run:

```
state                 wait_event   count
idle in transaction   Client        50     <- the entire pool
active                              1
```

Fifty connections open, in a transaction, doing nothing. The cause is
structural: a connection is checked out during **dependency resolution**
(`get_current_user` reads the user row) and held by the Session until
`get_db` closes it at the end of the request — so it spans event-loop
hops and the ceiling is **requests in flight**, not the 40 worker
threads. N in flight needs N connections; Postgres allows 100.

500-way concurrency would need a Postgres sized for 500 backends, ~10 MB
each. The benchmark now submits 500 at once with **40 in flight**, and
says so. 40-way contention oversells the unlocked build tenfold — the
claim never needed 500, it needed to be stated accurately. `ARCHITECTURE
_AND_WORKFLOWS.md` §12 corrected.

**Fixed — the connection pool, carried since session 5**

Not a distortion. On the defaults Benchmark 1 does not produce a bad
number, it produces none: `201=0 409=0 errors=500/500`, median 126 s,
`QueuePool limit of size 5 overflow 10 reached`. The pool was set
**below the server's own thread ceiling** — 40 threads competing for 15
connections. Now 40 + 10, `pool_timeout` 30 s → 10 s.

**`session.py` is a shared file and was changed solo.** A must review it;
the measured failure is the argument.

**Fixed — a benchmark must not import the application's `SessionLocal`**

With the server pool correct, 500 requests *still* gave 387 × `500`. The
benchmark process was building a second 50-connection engine from the
same module, against a 100-connection Postgres, and the symptom surfaced
in the **server's** log as `QueuePool timed out`. Benchmarks now get a
5-connection engine of their own.

**Fixed — my own benchmark was hiding the answer**

An early run reported `201=0 409=0 err=0` for 500 requests: three buckets
that summed to nothing, with 500 responses falling outside all of them.
It now prints the full `Counter` per trial and warns when the buckets do
not sum to the request count. Two long runs were wasted before the
numbers were readable.

**Verification run**

```
pytest tests/ -q                -> 5 passed
benchmark 1, broken, 3 trials   -> oversold 3/3, max 500 seated in 50 seats
benchmark 1, fixed,  3 trials   -> exactly 50 x3, 450 x 409, 0 mismatches
                                   peak_db_conns 37-39 of 40 in flight

regressions, all on THIS machine after touching session.py:
  check_jwt 9 / check_auth 25 / check_rbac 34 / check_rooms 34
  check_gpus 44 / check_courses 38 / check_idempotency 40   all exit 0
psql  -> users 3, enrollments 0, offerings 1, courses 1  (post-seed)
```

**Cost incurred**

- Four full 500-request runs produced unusable data before the fifth was
  readable: the default pool, then the duplicated pool, then the missing
  tally buckets, then unbounded in-flight. Each was a real defect and
  each is written up, but the sequence is the cost of building a
  measuring instrument without measuring the instrument first.
- ~25 ms baseline latency for `/health` on loopback inside the container
  was investigated and is **not** `--reload` (tested with a second
  uvicorn without it: 24 ms vs 28 ms). Unexplained, environmental, and
  it sets a floor on every latency figure above.

**Deadline 5 status: MET.** The checkpoint is *"first broken-vs-fixed
table with real numbers"* and there are now two of them: A's three-build
idempotency table (session 11) and Benchmark 1 above.

Five deadlines met, twelve sessions.

**Open / carried forward**

1. **`session.py` needs A's review** — shared file, changed solo, with a
   measured failure as justification. Top of the next joint session.
2. **The in-flight ceiling belongs in the README**, not just here. "500
   concurrent" is the phrase in three documents and it is not what the
   system does; what it does is serve 40 concurrently and refuse the rest
   correctly. Deadline 9.
3. Item 11 (stale locked read on the GPU path) **unchanged and still
   unresolved** — and Deadline 7's promotion transaction is the next
   locked read to be written.
4. Items 7, 9, 10 remain joint calls blocking Deadline 7; item 9 (lock
   order vs. promotion) is still the urgent one and is now **the** thing
   between the project and Deadline 6.
5. Benchmarks 2, 3 and 4 should move onto this harness. Benchmark 2 and 3
   currently live inside `check_gpus.py` and `check_idempotency.py` as
   threads, which works and is not wrong — but Deadline 9 asks a stranger
   to reproduce four tables, and four scripts in two styles is a worse
   answer than four benchmarks in one.
6. `POST /courses` / `POST /offerings` still belong to no deadline;
   Benchmark 1 creates offerings by direct insert for want of an endpoint.

---

## Template

```markdown
## Session N — YYYY-MM-DD — A / B / JOINT

**Advances:** Deadline M (name)   <- which milestone this served; say
                                     "not Deadline M+1" if the plan said
                                     otherwise and reality differed

**Plan:** what this session set out to do.

**Shipped**
- ...

**Verification run**       <- commands and their actual output, not "works"
- ...

**Cost incurred**          <- time lost and to what; omit if none
- ...

**Deadline M status:** MET / still open, and what is missing if open.
                       A session ending is not a deadline being met.

**Open / carried forward**
- ...
```

Two habits worth keeping, both learned the hard way in sessions 1 and 2:

- **Verify by querying, never by assuming a successful command.** A
  migration that reports success may not have run the DDL you wrote.
- **`alembic upgrade head` on a database you built incrementally proves
  nothing.** The real check is `downgrade base` → `upgrade head`, twice.
