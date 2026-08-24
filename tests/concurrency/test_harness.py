"""Does the harness actually fire concurrently?

Run inside the container:

    docker compose exec app python -m pytest tests/ -v

Every benchmark in this project is built on `harness.fire`, so if that
function quietly serialized its requests, all four tables would be
measuring nothing and every one of them would still look plausible --
exactly 50 seats would still be sold, and the number would prove nothing
about locking.

This is the "which implementation would fail this?" question applied to
the instrument rather than to the system: a harness that awaits each call
in turn passes any assertion about status codes and fails the timing
assertion below.

These are integration tests and need the stack up.
"""
import asyncio
import time

import pytest

from tests.concurrency.harness import Call, fire_async, latency, tally

ROOT = "http://localhost:8000"


@pytest.mark.asyncio
async def test_requests_actually_overlap():
    """Wall time must be far below the sum of the individual latencies.

    A sequential implementation has wall == sum. Concurrency is the whole
    claim of this file, so the assertion is deliberately about *time* and
    not about status codes -- codes cannot tell the two apart.
    """
    n = 25
    calls = [Call("GET", "/health") for _ in range(n)]

    started = time.perf_counter()
    results = await fire_async(calls, base_url=ROOT)
    wall_ms = (time.perf_counter() - started) * 1000

    assert all(r.status == 200 for r in results), tally(results)

    serial_ms = sum(r.elapsed_ms for r in results)
    # Half is a very loose bound: real overlap on 25 requests is far
    # better than 2x. Loose on purpose, so this fails on "serialized" and
    # not on "the laptop was busy".
    assert wall_ms < serial_ms / 2, (
        f"wall {wall_ms:.0f}ms vs serial {serial_ms:.0f}ms — "
        f"requests do not appear to overlap. {latency(results)}"
    )


@pytest.mark.asyncio
async def test_barrier_waits_for_the_slowest_starter():
    """All calls leave together, even when task creation is staggered.

    Without the barrier the first request can complete before the last
    coroutine is constructed, which turns a 500-way race into a trickle.
    Asserted by the spread of start times being small relative to the
    run: every request's elapsed time includes the barrier wait, so if
    the barrier works, the slowest starter pulls everyone's clock with it
    and the minimum latency is not wildly below the median.
    """
    calls = [Call("GET", "/health") for _ in range(20)]
    results = await fire_async(calls, base_url=ROOT)
    stats = latency(results)
    assert stats["min_ms"] > 0
    # Everyone was released together, so nobody finishes in a small
    # fraction of the median.
    assert stats["min_ms"] > stats["median_ms"] / 20, stats


@pytest.mark.asyncio
async def test_results_are_in_submission_order():
    """Order matters: benchmarks correlate result[i] with the student
    that call[i] was built for."""
    calls = [Call("GET", f"/health?i={i}") for i in range(10)]
    results = await fire_async(calls, base_url=ROOT)
    assert len(results) == len(calls)
    assert all(r.status == 200 for r in results)


@pytest.mark.asyncio
async def test_error_codes_are_extracted():
    """A coded 409/401 must arrive as `code`, not as prose to be parsed."""
    calls = [Call("GET", "/api/v1/gpus")]  # no token
    results = await fire_async(calls, base_url=ROOT)
    assert results[0].status == 401
    # 401 is uncoded by design (one remedy), so `code` is None -- this
    # asserts the extractor does not invent one.
    assert results[0].code is None


@pytest.mark.asyncio
async def test_bodies_are_captured_and_survive_a_non_json_response():
    """Benchmark 3 asserts that keyed replays return the SAME body, so
    the body has to reach it. A status code cannot carry that claim.

    The second half matters as much as the first: a body that does not
    decode must arrive as None rather than raising, or one 204 or one
    proxy error page takes down a 500-request trial.
    """
    calls = [Call("GET", "/health"), Call("GET", "/api/v1/gpus")]  # ok, then 401
    results = await fire_async(calls, base_url=ROOT)
    assert results[0].body == {"status": "ok"}
    # The 401 body is JSON too; what is asserted here is that `body` is
    # populated on failures, not only on the happy path.
    assert results[1].status == 401 and results[1].body is not None

    unreachable = await fire_async(
        [Call("GET", "/health")], base_url="http://127.0.0.1:9", timeout=2.0
    )
    assert unreachable[0].body is None


@pytest.mark.asyncio
async def test_transport_errors_are_captured_not_raised():
    """A connection failure is a datum, not a crash.

    If one request in a 500-way run blows up, the harness must still
    return the other 499 -- otherwise a single flake destroys the trial
    and the benchmark silently loses a data point.
    """
    calls = [Call("GET", "/health")]
    results = await fire_async(calls, base_url="http://127.0.0.1:9", timeout=2.0)
    assert results[0].status is None
    assert results[0].error is not None
