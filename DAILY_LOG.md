# Daily Log

A running record of **what happened, when, and by whom.**

Not a duplicate of `DECISIONS.md` — the two files answer different
questions, and keeping them separate is what stops both from rotting:

| File | Answers | Written when |
|---|---|---|
| `DECISIONS.md` | *why* the system is shaped this way | a decision is made or reversed |
| `DAILY_LOG.md` | *what* we did on a given day, and what it cost | end of each working day |
| `ARCHITECTURE_AND_WORKFLOWS.md` | what the system *is*, right now | the models or endpoints change |

Rule: if an entry here starts explaining *why*, it belongs in
`DECISIONS.md` and this entry should link to it instead.

Entries are chronological — oldest first, newest appended at the bottom.

---

## Day 1 — 2026-08-15 — JOINT

**Plan:** foundation. Repo, Docker, all models, first migration, frozen
interfaces, agreed error codes. The one day the plan says not to
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
  people present after today

**Cost incurred**

- ~1 hour lost: `enum.py` committed empty and `resource.py` never
  committed, so `app.models` could not import. See Incidents in
  `DECISIONS.md`.
- Port 5432 held by containers from an earlier project directory.

**Carried forward:** 7 outstanding schema items, listed in `DECISIONS.md`.

---

## Day 2 — 2026-08-17 — A (solo)

**Plan:** A takes auth (Argon2, JWT, register/login); B takes the seed
script and read endpoints.

**Actually spent on schema amendments and Day 1 verification**, before any
auth code. Three revisions plus a fix to Day 1's migration.

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

**Fixed — Day 1's migration chain could not be re-run from empty**

Day 1's "verify on a clean DB" checkpoint had only ever been tested
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
Day 1 flagged as an ACTION), and Day 7's block in `10_DAY_PLAN.md`, which
still described renumbering positions.

**Day 1 status: complete**, all five checklist items verified.

**Open / carried forward**

1. **B has not seen any of this.** `models/` changed solo, which the
   shared-file protocol does not allow after Day 1. B's Day 2 column is
   exactly what these changes break: `capacity` is no longer on `courses`,
   `instructor_id` is `NOT NULL`, `courses.code` is UNIQUE.
2. Outstanding items 6 and 7 are still open — decisions, not code, and
   both need B. Item 7 blocks Day 7 promotion.
3. `Resource` sets `polymorphic_identity = ResourceType.COURSE` on the
   base class, so a bare `Resource()` is typed COURSE while courses never
   appear in `resources`. Same confusion as item 6.
4. A's Day 2 auth work not started: `core/security.py` → `auth/schemas.py`
   → `auth/service.py` → `auth/router.py`.
5. `tests/` is empty and `pytest-asyncio` is not in `requirements.txt` —
   Day 5's harness has nothing to build on.

---

## Template

```markdown
## Day N — YYYY-MM-DD — A / B / JOINT

**Plan:** what the 10-day plan says today is.

**Shipped**
- ...

**Verification run**       <- commands and their actual output, not "works"
- ...

**Cost incurred**          <- time lost and to what; omit if none
- ...

**Open / carried forward**
- ...
```

Two habits worth keeping, both learned the hard way on Days 1 and 2:

- **Verify by querying, never by assuming a successful command.** A
  migration that reports success may not have run the DDL you wrote.
- **`alembic upgrade head` on a database you built incrementally proves
  nothing.** The real check is `downgrade base` → `upgrade head`, twice.
