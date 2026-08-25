from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.auth import service
from app.auth.schemas import RegisterRequest, TokenResponse, UserRead
from app.auth.service import EmailAlreadyRegistered
from app.core.errors import coded_error
from app.core.security import create_access_token
from app.database.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    db: Session = Depends(get_db),
) -> UserRead:
    """Create an account.

    `role` is taken from the request body, as Workflow A specifies, which
    means a caller can register themselves as ADMIN. That is a real hole
    and it is accepted deliberately for this system: the alternative is
    bootstrapping the first admin out of band, which adds a seeding
    concern to every clean-room run for a project whose claim is about
    concurrency, not identity. `scripts/seed.py` already provides the
    three demo accounts. Noted in DECISIONS.md rather than silently
    shipped.
    """
    try:
        return service.register(db, payload)
    except EmailAlreadyRegistered:
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "EMAIL_ALREADY_REGISTERED",
            "An account with this email already exists.",
        )


@router.post("/login", response_model=TokenResponse)
def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Exchange credentials for a bearer token carrying the caller's role.

    The body is **form-encoded, not JSON** — this is the OAuth2 password
    flow named in `INIT_PLAN.md` §4, and `python-multipart` has been in
    `requirements.txt` since Deadline 1 for exactly this. The practical
    payoff is that `/docs` gets a working **Authorize** button once
    Deadline 3 registers the bearer scheme, which is the difference
    between demoing RBAC by clicking and demoing it by pasting headers.

    The cost is that the email arrives in a field named `username`,
    because the OAuth2 spec names it that and FastAPI's form model
    follows. There is no `email` field to rename it to; the mapping is
    made here, once.
    """
    user = service.authenticate(db, form.username, form.password)

    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenResponse(
        access_token=create_access_token(user_id=user.id, role=user.role.value)
    )
