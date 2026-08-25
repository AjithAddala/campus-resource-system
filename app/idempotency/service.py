"""Exactly-once — the third of the three guarantees.

**A helper called from inside another module's transaction, which never
opens one of its own.** Same contract as `quotas/service.py`, and for the
same reason: the key and the work it guards must commit or roll back
together. This module takes a `Session` it did not create, emits no
COMMIT, returns plain values and raises plain exceptions. It knows
nothing about HTTP.

The invariant is a fact about the **request**, not about a user or a
resource:

    one (key, user) pair produces at most one allocation, ever

which is a different question from quota (a fact about the user, guarded
by the user row lock) and from capacity (a fact about the resource,
guarded by the resource row lock). Three invariants, three serialization
points, and none of them substitutes for another.

**The serialization point here is not a row lock.** It is the
`UNIQUE(key, user_id)` index on `idempotency_keys`. Two simultaneous
retries of the same request race to INSERT; Postgres lets exactly one
proceed and makes the other **block on the index entry** until the first
transaction ends. That is stronger than a lock we could take ourselves,
because it works before the row exists — there is nothing to lock until
somebody creates it, which is precisely the window a retry arrives in.

    first  request:  INSERT succeeds -> does the work -> COMMIT
    second request:  INSERT blocks   -> first commits -> unique violation
                                     -> reads the stored response, replays

Why `response_body` and `status_code` are nullable, which DECISIONS.md
already called load-bearing: the sequence is INSERT the claim (response
NULL) -> do the work -> UPDATE with the response -> COMMIT. The claim
happens first so the slot is taken before any work; the response is
filled in once we know what it is. Both land in one transaction.

**A failed allocation therefore leaves no key.** If the quota gate raises
after the claim, the whole transaction rolls back and the key row goes
with it, so a later retry books cleanly instead of replaying a 409. That
is the correct behaviour and it falls directly out of committing the two
together — "exactly once" is a promise about *successes*. A design that
kept the key across a rollback would have to answer "for how long is the
caller forbidden from retrying a request that never happened?", and there
is no good answer.
"""
import hashlib
import json
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.idempotency import IdempotencyKey


class KeyReplayed(Exception):
    """This (key, user) already completed. Carries the stored response.

    Raised on the **success** path, which is unusual enough to justify:
    it is an early exit that must abandon the rest of the transaction
    without doing any of its work, and every other early exit in
    `reserve_gpu` is already an exception the router translates. Returning
    a union type instead would put an `isinstance` check on the happy
    path of every caller, to describe a case that is not happy so much as
    *already handled*.
    """

    def __init__(self, body: dict | None, status_code: int):
        self.body = body
        self.status_code = status_code
        super().__init__(f"replaying stored response {status_code}")


class KeyReuse(Exception):
    """Same key, DIFFERENT request. `422 IDEMPOTENCY_KEY_REUSED`.

    A retry is a repetition of one request. Sending a second, different
    request under the same key is a client bug, and the dangerous
    reading is the silent one: replaying the first response would tell
    the caller their new request succeeded when nothing was done for it.
    Refusing is the only answer that cannot mislead.
    """

    def __init__(self, key: str):
        self.key = key
        super().__init__(f"key {key!r} was already used for a different request")


class KeyInFlight(Exception):
    """The claim is committed but carries no response.

    Should be unreachable on any path that calls `record_response` before
    its commit -- a concurrent retry either blocks (in flight, not yet
    committed) or sees a fully populated row (committed). It exists so
    that if a future caller ever forgets the `record_response` step, the
    symptom is a clear 409 rather than a replay of `null` with a `None`
    status code, which would surface as a 500 three layers away.
    """

    def __init__(self, key: str):
        self.key = key
        super().__init__(f"key {key!r} is claimed but has no recorded response")


def request_fingerprint(payload: dict[str, Any]) -> str:
    """A stable SHA-256 over the request, for detecting key reuse.

    `sort_keys=True` and the compact separators make the digest depend on
    the request's *content* and not on dict ordering or whitespace.

    **`hash()` would be wrong here**, and silently: Python salts it per
    process, so the digest stored by one uvicorn worker would not match
    the one computed by another for the identical body, and every replay
    across workers would 422 instead. It is the kind of bug that passes
    every single-process test.

    The caller decides what goes in. For GPU reservation that is the
    cluster id *and* the unit count -- the same body posted to two
    different clusters is two different requests, and folding only the
    body in would let a key claimed for cluster 1 replay a response that
    named cluster 2.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def claim(
    db: Session, key: str, user_id: int, endpoint: str, fingerprint: str
) -> None:
    """Take the (key, user) slot, or raise.

    Returns None when the claim is fresh and the caller should proceed
    with the work. Raises `KeyReplayed` when it has already been done,
    `KeyReuse` on the same key with a different request, `KeyInFlight`
    in the shouldn't-happen case above.

    **The SAVEPOINT is load-bearing and its absence is not subtle.** A
    unique violation aborts the whole Postgres transaction: every
    statement after it fails with `InFailedSqlTransaction`, so the read
    that fetches the stored response -- the entire point of catching the
    violation -- cannot run. `begin_nested()` wraps the INSERT in a
    SAVEPOINT, so the rollback is partial and the surrounding transaction
    survives to do the replay lookup and, on the other paths, to keep
    holding the locks it may already have taken.

    The re-read is safe under Postgres's default READ COMMITTED, where
    each statement takes a fresh snapshot: by the time we see the
    violation the other transaction has necessarily committed (had it
    still been open, our INSERT would be blocked, not rejected), so its
    row is visible to the SELECT that follows.

    A Core `insert()` rather than `db.add()`, deliberately: an ORM add
    would put a pending `IdempotencyKey` in the identity map, and
    reasoning about what a savepoint rollback leaves behind there is a
    complication with nothing to buy it.
    """
    try:
        with db.begin_nested():
            db.execute(
                insert(IdempotencyKey).values(
                    key=key,
                    user_id=user_id,
                    endpoint=endpoint,
                    request_hash=fingerprint,
                    response_body=None,
                    status_code=None,
                )
            )
    except IntegrityError:
        existing = db.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.key == key, IdempotencyKey.user_id == user_id
            )
        ).scalar_one()

        if existing.endpoint != endpoint or existing.request_hash != fingerprint:
            raise KeyReuse(key)

        if existing.status_code is None:
            raise KeyInFlight(key)

        raise KeyReplayed(existing.response_body, existing.status_code)


def record_response(
    db: Session, key: str, user_id: int, body: dict, status_code: int
) -> None:
    """Fill in the response on a claim this transaction already owns.

    **Must be called before the caller's COMMIT and never after it.** The
    whole guarantee is that this UPDATE and the allocation become durable
    in the same commit: split them and the bug moves rather than
    disappearing -- commit the allocation but lose the key and a retry
    double-allocates; commit the key but roll back the allocation and a
    retry returns a fake success for work that never happened.

    No lock is taken and none is needed. The row was INSERTed by *this*
    transaction moments ago and is not yet visible to anyone else, so
    there is no other writer to serialize against.
    """
    db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.key == key, IdempotencyKey.user_id == user_id)
        .values(response_body=body, status_code=status_code)
    )
