"""The caller's own profile.

`GET /me` was specified in INIT_PLAN.md §12 and
ARCHITECTURE_AND_WORKFLOWS.md with **no deadline assigned** — the same
gap that left the admin PATCH endpoints and `GET /me/quota` ownerless
until session 5 found them. It belongs at Deadline 3 and nowhere else:
it is the end-to-end proof that a real token decodes to a real user
carrying a real role, which is the only claim Deadline 3 makes.

There is no `users/service.py`, and that is not an oversight. INIT_PLAN.md
§15 splits routers (HTTP) from services (domain) because the domain half
is the part worth testing without a client. Here the domain half is
empty: `get_current_user` has already produced the row, so a service
function would be `def get_me(user): return user`. A file that only
forwards is a file that has to be kept in sync for nothing.

`GET /me/quota` lands here at Deadline 6, and it *will* bring a service —
it reads the quota policy and SUMs held units, which is real domain
logic.
"""
from fastapi import APIRouter, Depends

from app.auth.schemas import UserRead
from app.core.dependencies import get_current_user
from app.models.user import User

router = APIRouter(tags=["users"])


@router.get("/me", response_model=UserRead)
def read_me(user: User = Depends(get_current_user)) -> User:
    """The authenticated caller. 401 if the token is missing or invalid.

    Reuses `auth.schemas.UserRead` rather than declaring a second model
    of the same row. The duplicate would drift, and the property that
    matters most about that model is a *negative* one — it has no
    `password_hash` field, so no handler can leak the hash by returning
    the ORM object. That guarantee is worth having in exactly one place.
    """
    return user
