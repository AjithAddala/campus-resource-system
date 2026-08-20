"""Registration and authentication — the domain half.

This module knows nothing about HTTP. It raises `EmailAlreadyRegistered`,
not a 409, and returns `None` for bad credentials rather than a 401;
`router.py` maps both. Same rule the read modules follow, and the reason
the Deadline 4 transactions can be tested by calling services directly
instead of through the API.

Transaction boundary: `register()` commits, `authenticate()` does not
write at all. Per `DECISIONS.md`, `get_db()` deliberately does not commit
— the commit belongs on the line after the last write, where the
statements it makes durable are visible.
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.schemas import RegisterRequest
from app.core.security import DUMMY_PASSWORD_HASH, hash_password, verify_password
from app.models.user import User


class EmailAlreadyRegistered(Exception):
    """Raised by `register()` when `UNIQUE(users.email)` rejects the insert."""


def register(db: Session, payload: RegisterRequest) -> User:
    """Create a user, or raise `EmailAlreadyRegistered`.

    There is deliberately **no "does this email exist?" SELECT** before
    the insert. Such a check reads as defensive and is worse than
    useless: two simultaneous registrations of the same address can both
    run it, both see nothing, and both proceed — so the check would pass
    exactly when it matters least, and its presence would suggest the
    race is handled when it is not. `UNIQUE(email)` is what makes the
    second insert impossible, so the INSERT *is* the check and catching
    `IntegrityError` is how its result is read.

    This is the pattern the whole project runs on, stated in
    `DECISIONS.md`: our code handles the normal case, the database makes
    the race impossible.
    """
    user = User(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError as exc:
        # Rollback before anything else: the session is in a failed state
        # and any further statement on it would raise PendingRollbackError,
        # which would surface as a 500 and bury the real 409.
        db.rollback()
        raise EmailAlreadyRegistered(payload.email) from exc

    # `expire_on_commit=False` keeps the object readable after commit, but
    # `id` and `created_at` are server-generated and `created_at` was
    # never loaded — so serialising UserRead would emit a lazy SELECT from
    # inside the response layer. Refresh explicitly instead. The session
    # settings exist to stop statements appearing at surprising points;
    # relying on one here would spend that.
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    """The user for these credentials, or None. Does not distinguish
    "no such email" from "wrong password" — to the caller both are one
    401, and telling them apart would confirm which addresses exist.

    When the email is unknown we still run a full argon2 verify against a
    dummy hash. Skipping it would return in microseconds while a real
    address pays ~50ms, and that difference is measurable from outside —
    an identical response body with a distinguishable response time is
    still a disclosure.
    """
    user = db.query(User).filter(User.email == email).first()

    if user is None:
        verify_password(password, DUMMY_PASSWORD_HASH)
        return None

    if not verify_password(password, user.password_hash):
        return None

    return user
