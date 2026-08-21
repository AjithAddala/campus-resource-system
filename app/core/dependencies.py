"""Authentication and authorization — the API boundary.

**Deadline 3. This file was a stub until now**, returning a hardcoded
ADMIN so B could build routes before real JWT auth existed. Deadline 2's
checkpoint was green with that stub in place, which meant `GET /gpus`
returned 200 with a bearer token *and also with no token at all*: the
token was accepted, never verified. This file is what makes the
difference, and it needed no change at any of B's ten call sites —
the import path, the call shape and the return type were frozen at
Deadline 1 precisely so this swap would cost nothing.

Layer rule, from ARCHITECTURE_AND_WORKFLOWS.md §1: authentication and
authorization are *boundary* concerns and live here. Capacity, quota and
exactly-once are *invariants* and live inside a transaction. A 403 can be
decided from identity alone; an invariant can only be decided under a
lock. That is why this file raises HTTP errors freely while
`core/security.py` raises none.
"""
from typing import Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import API_PREFIX
from app.core.security import decode_access_token
from app.database.session import get_db
from app.models.enums import Role
from app.models.user import User

# `tokenUrl` is what wires the Authorize button in /docs to POST
# /api/v1/auth/login. It is the payoff for login being form-encoded
# (OAuth2 password flow) rather than JSON — the demo at Deadline 10 is
# clicking Authorize once, not pasting a header into every request.
#
# No leading slash: Swagger resolves it relative to the docs page, so the
# button keeps working if the app is ever mounted under a sub-path.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{API_PREFIX.lstrip('/')}/auth/login")


def _unauthorized(message: str) -> HTTPException:
    """401 with the header that makes it a spec-conforming 401.

    Uncoded on purpose. `core/errors.py::coded_error` exists for failures
    a client must branch on — `CAPACITY_EXHAUSTED` and `QUOTA_EXCEEDED`
    are both 409 and demand different remedies. There is exactly one
    remedy for any 401: get a new token. So this keeps FastAPI's plain
    string `detail`, per ARCHITECTURE_AND_WORKFLOWS.md §7.
    """
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        message,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """The authenticated caller, or 401.

    Every failure below is the same 401 with the same body. Whether a
    token was expired, tampered with, or names a user who has since been
    deleted is not the caller's business — and telling them apart would
    hand an attacker a probe.

    One `except` covers expiry, a bad signature and a malformed subject
    because `InvalidTokenError` is the common base of all three in PyJWT
    (verified in `scripts/check_jwt.py`, not read off a docstring). That
    single clause is the reason `security.py` was allowed to raise no
    HTTP errors of its own.
    """
    try:
        claims = decode_access_token(token)
    except jwt.InvalidTokenError:
        raise _unauthorized("Could not validate credentials")

    # `sub` is a STRING in the token and an integer in the database. The
    # str() happens in create_access_token, the int() happens here, and
    # both halves exist because PyJWT >= 2.10 enforces a string subject
    # on decode rather than encode — an int would issue a valid-looking
    # token that 401s on every later request. See DECISIONS.md.
    try:
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        raise _unauthorized("Could not validate credentials")

    user = db.get(User, user_id)
    if user is None:
        # A correctly signed token for a user that no longer exists. Rare,
        # but this is the one place it can be caught, and returning None
        # here would push a NoneType error into every handler downstream.
        raise _unauthorized("Could not validate credentials")

    return user


def require_role(*allowed: Role) -> Callable[..., User]:
    """Authorize by role, or 403. Usage, unchanged since Deadline 1:

        @router.post("", dependencies=[Depends(require_role(Role.ADMIN))])

        def handler(admin: User = Depends(require_role(Role.ADMIN))):

    Both forms work; the second also gives the handler the caller. Being
    a *dependency* rather than an `if` at the top of a handler is what
    makes the 403 fire **before the handler body runs**, so a rejected
    call cannot leave partial state behind. `scripts/check_rbac.py`
    asserts that by counting rows after the 403, not by trusting the
    status code.

    **The role is read from the database row, not from the token claim,
    and that reverses a Deadline 1 tradeoff.** DECISIONS.md accepted "a
    role change does not take effect until the token expires" on the
    grounds that the claim saves a lookup per request. It does not: the
    frozen signature returns a `User`, so `get_current_user` must load
    that row anyway, and the claim's saving was already spent before this
    function is reached. Given both copies are in hand, the fresher one
    wins — a demoted admin loses admin immediately rather than 60 minutes
    later. The claim still earns its place as the *demonstrable* half of
    the auth story (a decoded token visibly carries a role, which is the
    Deadline 2 checkpoint) and as what a future stateless service would
    read; it is simply not what this system authorises on.
    """
    def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            # Uncoded, same reasoning as the 401: one meaning, one remedy.
            # The message deliberately does not name the required role —
            # that is policy information, and the caller cannot act on it.
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
        return user

    return _checker
