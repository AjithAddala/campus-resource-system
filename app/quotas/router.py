"""Admin quota policy endpoints — Deadline 6.

Mounted at `/admin/quotas`, and living in `quotas/` rather than a new
`admin/` package because **the module that owns an invariant should own
the endpoint that configures it.** `role_quotas` is read by
`quotas.service.limit_for` inside three transactions; splitting the
policy writer into a different package would put the read and the write
of one table in two places, and A owns both halves.

Everything here is `[ADMIN]`, enforced by `require_role` as a dependency
rather than an `if` in the body — so a non-admin's 403 fires before any
handler code runs, which `check_rbac.py` asserts by counting rows rather
than by trusting the status code.

**Nothing here takes a lock, and that is the same decision recorded in
`limit_for`.** These rows are admin-editable *policy*, not per-user
state. An admin raising a limit while an allocation is in flight is not a
correctness problem: the invariant is "held <= limit" at commit time, and
the allocating transaction holds the user row, so `held` cannot move
underneath it. Locking policy rows would serialize every allocation in
the system on three rows to protect nothing.
"""
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import require_role
from app.database.session import get_db
from app.models.enums import ResourceType, Role
from app.models.quota import RoleQuota
from app.models.user import User
from app.quotas.schemas import RoleQuotaRead, RoleQuotaWrite

router = APIRouter(prefix="/admin/quotas", tags=["admin"])


@router.get("/{role}/{resource_type}", response_model=RoleQuotaRead)
def get_quota(
    role: Role,
    resource_type: ResourceType,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(Role.ADMIN)),
) -> RoleQuotaRead:
    """The policy for one (role, resource) pair. ADMIN only.

    **A missing row is a 404, not a row with a null limit**, and the
    distinction is the whole reason `QuotaNotConfigured` exists: absent
    and NULL mean different things, and an endpoint that flattened them
    would teach an admin the wrong model of their own policy table.
    `(FACULTY, COURSE)` is deliberately unseeded and 404s here.

    `role` and `resource_type` are enums in the signature, so a bad path
    segment is a 422 from FastAPI before this body runs — no hand-written
    validation, and `/admin/quotas/WIZARD/GPU` cannot reach the query.
    """
    row = (
        db.query(RoleQuota)
        .filter(RoleQuota.role == role, RoleQuota.resource_type == resource_type)
        .one_or_none()
    )
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No quota policy is configured for ({role.value}, {resource_type.value}).",
        )
    return row


@router.put("/{role}/{resource_type}", response_model=RoleQuotaRead)
def put_quota(
    role: Role,
    resource_type: ResourceType,
    payload: RoleQuotaWrite,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(Role.ADMIN)),
) -> RoleQuotaRead:
    """Set the policy for one pair. ADMIN only. Creates it if absent.

    **An upsert, and PUT is the right verb for it precisely because it
    creates.** PUT means "make the resource at this URL look like this",
    and the URL identifies the pair rather than a row id — so the same
    call is correct whether or not a row exists, and repeating it changes
    nothing. That also gives an admin the only in-band way to fix a
    missing policy: `(FACULTY, COURSE)` 409s every faculty registration
    with `QUOTA_NOT_CONFIGURED` until someone creates it, and before this
    endpoint that meant psql.

    **Lowering a limit below what users already hold does not evict**, per
    the quota-change rule in `ARCHITECTURE_AND_WORKFLOWS.md` §13. Nothing
    here looks at anyone's holdings, and that is the implementation of the
    rule rather than an omission: the gates read this row on the *next*
    allocation, so a user over the new limit simply cannot acquire more
    until they drop below it. Raising a limit takes effect on the next
    request for the same reason.

    Unlike the GPU capacity rule at `gpus.service.update_cluster`, this
    one needs no refusal branch — there is no CHECK constraint tying
    `max_units` to anybody's usage, so the documented behaviour and the
    schema agree here.
    """
    row = (
        db.query(RoleQuota)
        .filter(RoleQuota.role == role, RoleQuota.resource_type == resource_type)
        .one_or_none()
    )

    if row is None:
        row = RoleQuota(
            role=role, resource_type=resource_type, max_units=payload.max_units
        )
        db.add(row)
    else:
        row.max_units = payload.max_units

    db.commit()
    db.refresh(row)
    return row
