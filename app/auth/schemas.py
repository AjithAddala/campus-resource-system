from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Role


class RegisterRequest(BaseModel):
    """Registration payload.

    `email` is a plain constrained `str`, not Pydantic's `EmailStr`. That
    would pull in `email-validator`, a dependency the image does not have
    — and adding one to `requirements.txt` forces a rebuild on both
    machines for a format check that guarantees nothing we rely on. What
    the system actually needs is that two accounts cannot share an
    address, and that is `UNIQUE(email)` in the database, which no
    amount of client-side validation can substitute for.
    """

    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: Role


class UserRead(BaseModel):
    """A user as returned to a caller. Note what is absent: password_hash
    is not in this model, so it cannot be leaked by adding a field to a
    handler's return value.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: Role
    created_at: datetime


class TokenResponse(BaseModel):
    """OAuth2-shaped, so `Authorization: Bearer <access_token>` follows
    directly from the field names.

    The role is deliberately **not** duplicated as a field here. It is a
    claim inside the token, which is the copy authorization actually
    reads; a second copy in the envelope would be a number a client could
    trust that the server never checks.
    """

    access_token: str
    token_type: str = "bearer"
