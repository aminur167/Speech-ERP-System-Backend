"""
Branch creation and updates, including manager account provisioning.

The frontend mock kept `managerEmail` and a plaintext `managerPassword` as
columns on the branch, and called `upsertManagerAccount` to mirror them into a
separate user store. Here the manager *is* a User row: the password is hashed
by `set_password`, never stored or returned in plaintext, and branch + account
are written in one transaction so a branch can't end up without its manager.
"""

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.branches.models import Branch
from apps.common import audit
from apps.common.models import AuditLog

User = get_user_model()


@transaction.atomic
def create_branch(*, actor, data: dict) -> Branch:
    """
    Create a branch and provision its manager account together.

    Atomic on purpose: a branch with no manager can't be logged into, and an
    orphan user account attached to no branch is scoped to nothing. Either
    both exist or neither does.
    """
    manager_email = data.pop("manager_email")
    manager_name = data.pop("manager_name")
    manager_password = data.pop("manager_password")
    manager_code = data.pop("manager_code", "")

    branch = Branch.objects.create(**data)

    manager = User.objects.create_user(
        email=manager_email,
        password=manager_password,
        name=manager_name,
        role=User.Role.MANAGER,
        branch=branch,
        staff_code=manager_code,
    )

    branch.manager = manager
    branch.save(update_fields=["manager"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=branch,
        branch=branch,
        changes={"name": branch.name, "code": branch.code, "manager": manager.email},
    )
    return branch


@transaction.atomic
def update_branch(*, actor, branch: Branch, data: dict) -> Branch:
    """
    Update a branch and, where supplied, its manager's details.

    A blank/absent password means "leave the existing one alone" — the Admin
    editing a branch's phone number shouldn't have to retype the manager's
    password, and there's no way to read the current one to pre-fill it.
    """
    manager_email = data.pop("manager_email", None)
    manager_name = data.pop("manager_name", None)
    manager_password = data.pop("manager_password", None)
    manager_code = data.pop("manager_code", None)

    before = {
        "name": branch.name,
        "code": branch.code,
        "status": branch.status,
        "phone": branch.phone,
        "address": branch.address,
    }

    for field, value in data.items():
        setattr(branch, field, value)
    branch.save()

    manager = branch.manager
    if manager is not None:
        manager_updates = []
        if manager_email and manager_email.lower() != manager.email:
            manager.email = manager_email
            manager_updates.append("email")
        if manager_name and manager_name != manager.name:
            manager.name = manager_name
            manager_updates.append("name")
        if manager_code is not None and manager_code != manager.staff_code:
            manager.staff_code = manager_code
            manager_updates.append("staff_code")
        if manager_password:
            manager.set_password(manager_password)
            manager_updates.append("password")

        if manager_updates:
            manager.save()

    elif manager_email and manager_name and manager_password:
        # Branch previously had no manager (its user was removed) — provision one.
        manager = User.objects.create_user(
            email=manager_email,
            password=manager_password,
            name=manager_name,
            role=User.Role.MANAGER,
            branch=branch,
            staff_code=manager_code or "",
        )
        branch.manager = manager
        branch.save(update_fields=["manager"])

    after = {
        "name": branch.name,
        "code": branch.code,
        "status": branch.status,
        "phone": branch.phone,
        "address": branch.address,
    }

    audit.record(
        actor=actor,
        action=AuditLog.Action.UPDATE,
        target=branch,
        branch=branch,
        changes=audit.diff(before, after),
    )
    return branch
