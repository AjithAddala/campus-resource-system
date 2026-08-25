from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.router import router as auth_router
from app.core.config import API_PREFIX
from app.courses.router import offerings_router
from app.courses.router import router as courses_router
from app.database.session import get_db
from app.gpus.router import router as gpus_router
from app.quotas.router import router as quotas_router
from app.rooms.router import router as rooms_router
from app.users.router import router as users_router

# The prefix itself now lives in core/config.py -- core/dependencies.py
# needs it for the OAuth2 tokenUrl, and importing main.py from there
# would be a cycle. The reasoning for the value is unchanged from session
# 6: INIT_PLAN.md and ARCHITECTURE_AND_WORKFLOWS.md both write every
# route under /api/v1 while EXECUTION_PLAN.md writes them bare, two
# documents beat one, and retrofitting a prefix once the benchmarks exist
# would touch every route and every harness URL.

app = FastAPI(title="Campus Resource Allocation System", version="0.1.0")


# Health checks stay at the ROOT, outside the version prefix. They are
# infrastructure, not API: docker-compose and any future monitoring hit
# them, and those callers should not have to track an API version. Note
# /health/db proves only that Postgres answers SELECT 1 -- it says
# nothing about whether the schema exists. `alembic current` is the check
# for that; see DECISIONS.md, "a healthy stack on an empty database".
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/db")
def health_db(db: Session = Depends(get_db)) -> dict:
    value = db.execute(text("SELECT 1")).scalar_one()
    return {"database": "ok", "result": value}


# Auth, Deadline 2. Mounted under the SAME constant as everything else --
# the coordination point B flagged when they settled the prefix. These
# two routes are public by design and stay public after Deadline 3: they
# are how a caller gets the token the other routes will demand.
app.include_router(auth_router, prefix=API_PREFIX)

# The caller's own profile, Deadline 3. No prefix of its own: `/me` is
# not a collection, and nesting it under /users/ would imply a users
# collection this system does not expose.
app.include_router(users_router, prefix=API_PREFIX)

# Read paths, Deadline 2. As of Deadline 3 these are authenticated for
# real: core/dependencies.py decodes the bearer token and 401s if it is
# missing, expired or tampered with. Nothing here changed to make that
# happen -- the swap was behind a frozen import path, which was the whole
# point of shipping the stub at Deadline 1.
app.include_router(gpus_router, prefix=API_PREFIX)
app.include_router(rooms_router, prefix=API_PREFIX)
app.include_router(courses_router, prefix=API_PREFIX)

# Admin quota policy, Deadline 6. Under the same version prefix as
# everything else -- /admin is a section of the API, not infrastructure,
# so unlike the health checks it is versioned. It lives in `quotas/`
# because the module that owns an invariant should own the endpoint that
# configures it.
app.include_router(quotas_router, prefix=API_PREFIX)
# Course reads are course-shaped; offering reads are offering-shaped, and
# the write paths that land at Deadline 4 attach to the offerings router
# because the offering is the row holding enrolled_count.
app.include_router(offerings_router, prefix=API_PREFIX)
