"""Auth dependencies.

STUB VERSION — Deadline 1. Returns a hardcoded ADMIN so B can build
routes before real JWT auth exists. Replaced on Deadline 3 with real
token decoding.

What is FROZEN is the import path, the call shape B writes
(`Depends(get_current_user)` / `Depends(require_role(...))`) and the
return type. The real implementations WILL take parameters the stubs do
not — a bearer token and a Session — which FastAPI injects, so the
Deadline 3 swap still requires no change on B's side.
"""
from typing import Callable

from app.models.enums import Role
from app.models.user import User


def get_current_user() -> User:
    # STUB — Deadline 3 replaces this with JWT decode + DB lookup.
    return User(
        id=1,
        name="Stub Admin",
        email="admin@iitk.ac.in",
        password_hash="",
        role=Role.ADMIN,
    )


def require_role(*allowed: Role) -> Callable:
    """Usage in a router:

        @router.post("/gpus", dependencies=[Depends(require_role(Role.ADMIN))])

    STUB — allows everything. Deadline 3 makes it raise 403.
    """
    def _checker() -> User:
        return get_current_user()

    return _checker