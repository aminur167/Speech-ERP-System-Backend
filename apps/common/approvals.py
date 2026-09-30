"""
Approval pulse: a cheap "has anything in the approval queues changed?"

Admin's queues — expenses, refunds, salary requests, package proposals,
package change requests — need to reflect a Manager's new request within a
few seconds, and a Manager needs to see Admin's decision just as quickly.
There is no push channel in this stack (docs/00), so screens poll. Polling
the lists themselves every few seconds would re-read five tables per open
tab; instead each save of one of those records bumps a per-branch counter
(ApprovalActivity), and screens poll that one number, refetching the lists
only when it moves.

The bump happens **after the transaction commits** (`on_commit`):
  * a change that rolls back never reaches anyone's screen;
  * the counter row is not locked inside the approval's own transaction, so
    two approvals in one branch can't queue up behind each other — or
    deadlock — on it.

Signals rather than calls sprinkled through the service functions, so a new
code path that saves one of these records can't forget to announce it.
Nothing updates these models with `QuerySet.update()`, which would bypass the
signal; keep it that way, or bump explicitly there.
"""

from django.apps import apps
from django.db import connection, transaction
from django.db.models import Sum
from django.db.models.signals import post_delete, post_save
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.models import ApprovalActivity

#: Every model an approval queue is built from. All carry a `branch`.
WATCHED_MODELS = (
    "expenses.Expense",
    "payments.RefundRequest",
    "staff.SalaryPayment",
    "services.Service",
    "services.PackageActionRequest",
)

_TABLE = ApprovalActivity._meta.db_table
_BUMP_SQL = f"""
    INSERT INTO {_TABLE} (branch_id, version) VALUES (%s, 1)
    ON CONFLICT (branch_id) DO UPDATE SET version = {_TABLE}.version + 1
"""


def bump(branch_id) -> None:
    """Move the branch's counter now. Callers normally want `bump_on_commit`."""
    if branch_id is None:
        return
    with connection.cursor() as cursor:
        cursor.execute(_BUMP_SQL, [branch_id])


def bump_on_commit(branch_id) -> None:
    if branch_id is None:
        return
    transaction.on_commit(lambda: bump(branch_id))


def _on_change(sender, instance, **kwargs):
    bump_on_commit(getattr(instance, "branch_id", None))


def connect_signals() -> None:
    """Called once from CommonConfig.ready()."""
    for label in WATCHED_MODELS:
        model = apps.get_model(label)
        post_save.connect(_on_change, sender=model, dispatch_uid=f"approval-pulse-save-{label}")
        post_delete.connect(
            _on_change, sender=model, dispatch_uid=f"approval-pulse-delete-{label}"
        )


class ApprovalPulseSerializer(serializers.Serializer):
    version = serializers.CharField()


class ApprovalPulseView(APIView):
    """
    GET /api/approvals/pulse/ -> { "version": "..." }

    A Manager's version is their own branch's counter — the only branch whose
    queues they can see. Admin's covers every branch. Compare it with the
    last one seen; when it differs, refetch the approval lists.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ApprovalPulseSerializer

    @extend_schema(tags=["common"], responses=ApprovalPulseSerializer)
    def get(self, request):
        user = request.user
        if user.is_manager:
            row = ApprovalActivity.objects.filter(branch_id=user.branch_id).first()
            version = row.version if row else 0
        else:
            # Only ever increases, so any change anywhere changes the sum.
            version = ApprovalActivity.objects.aggregate(total=Sum("version"))["total"] or 0
        return Response({"version": str(version)})
