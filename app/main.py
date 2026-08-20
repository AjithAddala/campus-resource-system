from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.courses.router import offerings_router
from app.courses.router import router as courses_router
from app.database.session import get_db
from app.gpus.router import router as gpus_router
from app.rooms.router import router as rooms_router

# Settled here rather than in each router so there is exactly one place
# to change it. INIT_PLAN.md §12 and ARCHITECTURE_AND_WORKFLOWS.md both
# write every route as /api/v1/...; EXECUTION_PLAN.md writes them bare.
# Two documents beat one, and retrofitting a prefix once auth/ and the
# benchmarks exist would touch every route and every harness URL.
API_PREFIX = "/api/v1"

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


# Read paths, Deadline 2. Every one of these is currently authenticated
# by the STUB in core/dependencies.py, which returns a hardcoded ADMIN --
# so they are reachable without a token today and will start requiring
# one at Deadline 3, with no change needed here.
app.include_router(gpus_router, prefix=API_PREFIX)
app.include_router(rooms_router, prefix=API_PREFIX)
app.include_router(courses_router, prefix=API_PREFIX)
# Course reads are course-shaped; offering reads are offering-shaped, and
# the write paths that land at Deadline 4 attach to the offerings router
# because the offering is the row holding enrolled_count.
app.include_router(offerings_router, prefix=API_PREFIX)
