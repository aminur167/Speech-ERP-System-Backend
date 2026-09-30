"""
Approval queues put what is still waiting on top.

Every list an Admin decides from — expenses, refunds, salary requests,
package change requests, proposed packages — shows the requests nobody has
decided yet first, newest first among them, and everything already decided
below. Sorted in the database, before pagination, so a pending request on
what would have been page 3 is on page 1 where it belongs, whatever status
filter is chosen ("All" included).

An explicit `?ordering=` from the client still wins: the OrderingFilter runs
after `get_queryset()` and replaces this order when asked to.
"""

from django.db.models import Case, IntegerField, QuerySet, Value, When


def pending_first(
    queryset: QuerySet,
    *,
    pending: str,
    field: str = "status",
    then: tuple[str, ...] = ("-created_at", "-id"),
) -> QuerySet:
    rank = Case(
        When(**{field: pending}, then=Value(0)),
        default=Value(1),
        output_field=IntegerField(),
    )
    return queryset.annotate(pending_rank=rank).order_by("pending_rank", *then)
