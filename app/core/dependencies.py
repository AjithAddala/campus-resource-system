"""Auth dependencies.

STUB VERSION — Day 1. Returns a hardcoded ADMIN so B can build routes
before real JWT auth exists. Replaced on Day 3 with real token decoding.
The signatures here are FROZEN: B imports them from this path, and the
Day 3 swap must not require any change on their side.
"""
from typing import Callable

from app.models.enums import Role
from app.models.user import User


def get_current_user() -> User:
    # STUB — Day 3 replaces this with JWT decode + DB lookup.
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

    STUB — allows everything. Day 3 makes it raise 403.
    """
    def _checker() -> User:
        return get_current_user()

    return _checker