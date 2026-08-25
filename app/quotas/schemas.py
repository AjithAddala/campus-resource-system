"""Wire shapes for the quota endpoints — Deadline 6."""
from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ResourceType, Role


class RoleQuotaRead(BaseModel):
    """One policy row.

    `max_units = null` means **unlimited**, and it is the same null a
    client sees on `GET /me/quota`. The two endpoints agree on that
    spelling deliberately: an admin reading policy and a user reading
    their own limits should not have to learn two conventions for the
    same fact.
    """

    model_config = ConfigDict(from_attributes=True)

    role: Role
    resource_type: ResourceType
    max_units: int | None


class RoleQuotaWrite(BaseModel):
    """PUT body: the new cap, or null for unlimited.

    `ge=0` rather than `ge=1`. Zero is a meaningful policy — "this role
    may hold none of this resource" — and it is the only way to express
    that without deleting the row, which would mean something different
    again: a missing row is *no policy*, and `limit_for` fails closed on
    it. Three states, three spellings:

        max_units = 5      at most five
        max_units = 0      none at all
        max_units = null   unlimited
        (row absent)       no policy -> 409 QUOTA_NOT_CONFIGURED
    """

    max_units: int | None = Field(default=None, ge=0)


class QuotaUsage(BaseModel):
    """One line of `GET /me/quota`: the policy and what the caller holds."""

    resource_type: ResourceType
    limit: int | None
    held: int
    unlimited: bool
    configured: bool


class MyQuota(BaseModel):
    """`GET /me/quota`.

    Carries `role` because the limits are a function of it, and a client
    showing "2 of 2 GPUs" is usually a click away from wanting to say
    why. It is read from the database row via `get_current_user`, not
    from the token claim — see the Deadline 3 reversal in DECISIONS.md.
    """

    user_id: int
    role: Role
    quotas: list[QuotaUsage]
