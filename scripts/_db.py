"""A small, tool-sized `Session` factory for everything in `scripts/`.

**Nothing in this directory may import the application's `SessionLocal`.**
That engine is sized for the server — `pool_size=40, max_overflow=10` —
so a gate script importing it opens a *second* pool of the same size
against a Postgres that allows 100 connections in total. The symptom is
not "too many clients": it is `QueuePool ... timed out` inside the
**server**, which reads exactly like the bug the pool sizing was
introduced to fix.

Measured, in the benchmark process that hit it first: a 500-request run
against a correctly sized server pool still returned 387 of 500 as `500`,
because the tool process was competing for the same 100 connections.

The sizing itself is defined once, in
`tests.concurrency.harness.tool_session_factory`, and re-exported here
rather than copied. Two definitions of one connection budget is exactly
the kind of duplication that drifts silently — and the failure it drifts
into is a `500` that looks like a server bug.

Gates need about three connections between setup, assertions and
cleanup, so five is generous.
"""
from tests.concurrency.harness import tool_session_factory

SessionLocal = tool_session_factory()
