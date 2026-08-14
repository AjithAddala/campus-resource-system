from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.session import get_db

app = FastAPI(title="Campus Resource Allocation System", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/db")
def health_db(db: Session = Depends(get_db)) -> dict:
    value = db.execute(text("SELECT 1")).scalar_one()
    return {"database": "ok", "result": value}