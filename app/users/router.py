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

`GET /me/quota` landed here at Deadline 6, and it did **not** bring a
`users/service.py` after all. The prediction above was half right: there
is real domain logic, but it belongs to `quotas/`, which already owns
every other reader of that policy. `quotas.service.usage_snapshot` is
that logic; this file stays an HTTP wrapper, which is exactly how the
plan described the endpoint — *"the quotas/ helper with an HTTP
wrapper."*
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.schemas import UserRead
from app.core.dependencies import get_current_user
from app.database.session import get_db
from app.models.user import User
from app.quotas import service as quotas
from app.quotas.schemas import MyQuota

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


@router.get("/me/quota", response_model=MyQuota)
def read_my_quota(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MyQuota:
    """The caller's limits and current usage, for all three resources.

    **Read-only, and it must NOT take the user lock.** The plan says so
    and the reason belongs next to the code: this is a dashboard read. A
    number shown to a caller is stale the moment it is rendered — another
    of their requests may commit before they act on it — and that is
    fine, because the allocation transaction re-reads everything under
    the lock and is what has to be right. Locking the user row here would
    put a GET in contention with the flagship write path to buy an
    illusion of freshness.

    Same standing as `GPUAvailability` and `RoomAvailability`, which
    carry the same caveat for the same reason: boundary reads inform a
    human, they do not decide an allocation.

    **Workflow B in `ARCHITECTURE_AND_WORKFLOWS.md` opens with this
    call**, so until now the flagship demo began on an endpoint nobody
    owned — one of five specified-but-unassigned routes this project has
    had to find and place.

    Any authenticated role. There is no admin variant reading someone
    else's usage; `GET /admin/quotas/{role}/{resource}` exposes the
    policy, which is the part an admin actually administers.
    """
    return MyQuota(
        user_id=user.id,
        role=user.role,
        quotas=quotas.usage_snapshot(db, user.id, user.role),
    )
