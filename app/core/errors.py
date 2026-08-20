"""The wire shape of a machine-readable error.

Deadline 1 agreed the error *codes* (`CAPACITY_EXHAUSTED`,
`QUOTA_EXCEEDED`, `IDEMPOTENCY_KEY_REUSED`) but never the JSON they
arrive in, because no endpoint had yet returned one. This is the first
one that does, so the shape is settled here rather than invented three
more times at Deadlines 4 and 5.

    {"detail": {"code": "QUOTA_EXCEEDED", "message": "..."}}

Why an envelope at all: `CAPACITY_EXHAUSTED` and `QUOTA_EXCEEDED` are
both `409` and the caller's remedy differs — wait or try another cluster,
versus release something you already hold. A client cannot tell them
apart by status code, and must not have to parse English prose to do it.
`code` is the contract; `message` is for humans and may be reworded
freely.

B's benchmarks assert on `code`, so every coded failure in the system
goes through this function and none of them hand-roll a `detail` dict.
"""
from fastapi import HTTPException


def coded_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build (do not raise) an HTTPException carrying a stable code.

    Returns rather than raises so call sites read `raise coded_error(...)`
    — the `raise` stays visible at the point of failure instead of being
    buried inside a helper, which matters when the failure is one branch
    of a transaction.
    """
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )
