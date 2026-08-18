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

**As of session 4 (2026-08-18): Deadline 1 MET. Deadline 2 not started.**

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

**Advances:** Deadline 1 — and **closes it**. First session where the
work done matches the deadline claimed.

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
