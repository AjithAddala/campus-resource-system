"""Fire N HTTP requests simultaneously and collect what came back.

asyncio + httpx on the client side, against the **sync** FastAPI server —
which is the whole point of the sync/async decision recorded in
DECISIONS.md. `SELECT ... FOR UPDATE` semantics are identical either way,
so the only place real concurrency is needed is here, in the client.

Used by every benchmark. Deliberately knows nothing about courses, GPUs
or rooms: it takes a list of `Call`s and gives back a list of `Result`s.

--------------------------------------------------------------------
THE THING THIS FILE EXISTS TO GET RIGHT
--------------------------------------------------------------------
"500 concurrent requests" is a claim about what reaches the **database**,
and there are three independent throttles between `asyncio.gather` and a
row lock. Each one silently caps concurrency, and the lowest one wins:

    1. httpx `max_connections`      default 100
    2. the server's thread pool     anyio default 40 for sync endpoints
    3. the SQLAlchemy pool          default 5 + 10 overflow = 15

Firing 500 coroutines through defaults measures **15-way** contention and
reports it as 500. The benchmark would still pass -- exactly 50 seats
would be sold -- while proving far less than it claimed.

This module fixes what it can (1) and **measures** the rest, because the
remaining two are not the harness's to set: the thread pool belongs to
uvicorn and the connection pool is a shared file needing both people.
`peak_db_connections()` samples what actually happened, so a benchmark
can report the concurrency it really achieved instead of the number it
asked for.
"""
import asyncio
import statistics
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

BASE_URL = "http://localhost:8000/api/v1"


def tool_session_factory():
    """A SMALL, separate pool for benchmark bookkeeping.

    Benchmarks must not import the application's `SessionLocal`. That
    engine is sized for the server (40 + 10 overflow), so a benchmark
    process importing it silently reserves a second pool of the same size
    — and Postgres `max_connections` is 100. Server 50 + benchmark 50 + a
    psql session does not fit, and the symptom is not "too many clients":
    it is `QueuePool ... timed out` inside the SERVER, which reads exactly
    like the bug the pool sizing was supposed to fix.

    Measured: a 500-request run against a correctly sized server pool
    still returned 387 of 500 as `500`, because the harness process was
    competing for the same 100 connections.

    Setup, resets and the observer need about three connections between
    them, so five is generous.
    """
    from app.core.config import get_settings

    engine = create_engine(
        get_settings().database_url,
        pool_size=5,
        max_overflow=2,
        pool_timeout=10,
        pool_pre_ping=True,
    )
    return sessionmaker(bind=engine, autocommit=False, expire_on_commit=False)


@dataclass(frozen=True)
class Call:
    """One request the harness will fire."""

    method: str
    url: str
    headers: dict[str, str] | None = None
    json: Any = None


@dataclass(frozen=True)
class Result:
    """What came back. `code` is the machine-readable error code from
    `core/errors.py`, pulled out because every benchmark asserts on it and
    none of them should be parsing prose.
    """

    status: int | None
    code: str | None
    elapsed_ms: float
    error: str | None = None


@dataclass
class Peak:
    """Observed database concurrency during a run."""

    max_active: int = 0
    samples: list[int] = field(default_factory=list)


def _code_of(response: httpx.Response) -> str | None:
    try:
        detail = response.json().get("detail")
    except Exception:
        return None
    return detail.get("code") if isinstance(detail, dict) else None


async def _one(
    client: httpx.AsyncClient,
    call: Call,
    barrier: asyncio.Barrier,
    gate: asyncio.Semaphore | None = None,
) -> Result:
    # Every coroutine waits here until the last one is ready, so the
    # requests leave together rather than trickling out as the event loop
    # creates tasks. Without it the first request can be finished before
    # the last is constructed, and a "concurrent" benchmark quietly
    # becomes a sequential one.
    await barrier.wait()
    started = time.perf_counter()
    try:
        # `gate` caps how many requests are IN FLIGHT, which is a real
        # ceiling of this architecture and not a property of the test.
        # See `fire_async` for why it exists.
        if gate is not None:
            async with gate:
                response = await client.request(
                    call.method, call.url, headers=call.headers, json=call.json
                )
        else:
            response = await client.request(
                call.method, call.url, headers=call.headers, json=call.json
            )
    except Exception as exc:  # noqa: BLE001 - the error IS the datum here
        return Result(
            status=None,
            code=None,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )
    return Result(
        status=response.status_code,
        code=_code_of(response),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


async def fire_async(
    calls: Sequence[Call],
    *,
    base_url: str = BASE_URL,
    timeout: float = 120.0,
    max_in_flight: int | None = None,
) -> list[Result]:
    """Release every call at once; return results in submission order.

    `max_connections` is raised to the number of calls. httpx's default is
    100, so 500 calls through a default client would run 100 at a time and
    the benchmark would report a concurrency it never had -- the first of
    the three throttles above, and the only one this file owns.

    `max_keepalive_connections=0` because connection reuse is exactly what
    must not happen here: a reused connection is a request that waited for
    an earlier one to finish.

    ------------------------------------------------------------------
    `max_in_flight` -- why "500 concurrent" is not achievable here
    ------------------------------------------------------------------
    Measured, not assumed. Firing 500 unbounded gives:

        responses: {(201, None): 60, (500, None): 440}
        pg_stat_activity: 50 connections `idle in transaction`, waiting
        on Client -- the entire pool, held and doing nothing

    The reason is structural. A connection is checked out during
    DEPENDENCY RESOLUTION -- `get_current_user` reads the user row -- and
    the Session holds it, with an open transaction, until `get_db` closes
    it at the end of the request. The connection is therefore held across
    event-loop hops, so the ceiling is **requests in flight**, not the
    server's 40 worker threads. N in-flight requests need N connections,
    and Postgres allows 100.

    So a bigger pool cannot buy 500-way concurrency; only a Postgres
    sized for 500 backends could, at roughly 10 MB each. This is a real
    property of sync-FastAPI-with-a-session-per-request, and the honest
    thing is to bound the window and REPORT it rather than fire 500 and
    describe the resulting pile of 500s as a benchmark.

    All calls still wait on the barrier, so the first `max_in_flight` of
    them hit the seat gate simultaneously -- which is the contention the
    benchmark is actually about. 40-way contention oversells the unlocked
    build eightfold; the claim does not need 500.
    """
    limits = httpx.Limits(
        max_connections=len(calls) + 10, max_keepalive_connections=0
    )
    gate = asyncio.Semaphore(max_in_flight) if max_in_flight else None
    async with httpx.AsyncClient(
        base_url=base_url, limits=limits, timeout=timeout
    ) as client:
        barrier = asyncio.Barrier(len(calls))
        return list(
            await asyncio.gather(*(_one(client, c, barrier, gate) for c in calls))
        )


def fire(
    calls: Sequence[Call],
    *,
    base_url: str = BASE_URL,
    timeout: float = 120.0,
    max_in_flight: int | None = None,
) -> list[Result]:
    """Synchronous wrapper, so a benchmark script needs no event loop."""
    return asyncio.run(
        fire_async(
            calls, base_url=base_url, timeout=timeout, max_in_flight=max_in_flight
        )
    )


def tally(results: Iterable[Result]) -> Counter:
    """`{(status, code): count}` — the shape every benchmark reports."""
    return Counter((r.status, r.code) for r in results)


def latency(results: Iterable[Result]) -> dict[str, float]:
    times = sorted(r.elapsed_ms for r in results)
    if not times:
        return {}
    return {
        "min_ms": round(times[0], 1),
        "median_ms": round(statistics.median(times), 1),
        "max_ms": round(times[-1], 1),
    }


class DBConcurrencyObserver:
    """Samples how many backends are actually running our queries.

    This is the honest half of the "500 concurrent" claim. It polls
    `pg_stat_activity` from its own connection while the run is in flight
    and keeps the maximum, so a benchmark can print the concurrency it
    ACHIEVED next to the number it requested. If those two numbers differ
    by an order of magnitude, the run measured a pool and not a lock.

    Uses its own engine connection deliberately -- borrowing one from the
    application pool would consume the very resource being measured.
    """

    def __init__(self, session_factory, interval: float = 0.005):
        self._session_factory = session_factory
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak = Peak()

    def _run(self) -> None:
        db = self._session_factory()
        try:
            while not self._stop.is_set():
                n = db.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND state = 'active' AND pid <> pg_backend_pid()"
                    )
                ).scalar_one()
                db.rollback()
                self.peak.samples.append(n)
                self.peak.max_active = max(self.peak.max_active, n)
                time.sleep(self._interval)
        finally:
            db.close()

    def __enter__(self) -> "DBConcurrencyObserver":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
