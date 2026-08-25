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


app = FastAPI(title="Campus Resource Allocation System", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/db")
def health_db(db: Session = Depends(get_db)) -> dict:
    value = db.execute(text("SELECT 1")).scalar_one()
    return {"database": "ok", "result": value}


app.include_router(auth_router, prefix=API_PREFIX)

app.include_router(users_router, prefix=API_PREFIX)

app.include_router(gpus_router, prefix=API_PREFIX)
app.include_router(rooms_router, prefix=API_PREFIX)
app.include_router(courses_router, prefix=API_PREFIX)

app.include_router(quotas_router, prefix=API_PREFIX)
app.include_router(offerings_router, prefix=API_PREFIX)
