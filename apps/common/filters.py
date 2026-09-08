"""
Shared query-parameter filters.

`dateFrom`/`dateTo` is one spelling read by every list the branch Summary page
shows — payments, expenses, refunds, closings. It lives here rather than being
re-implemented per view: four slightly different range filters is exactly how
"the same range" starts returning four inconsistent sets of rows.
"""

from datetime import datetime


def parse_date(value):
    """An ISO "YYYY-MM-DD" query param, or None when absent or malformed."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def apply_date_range(queryset, params, *, field="created_at", is_date_field=False):
    """
    Narrow `queryset` to `dateFrom`..`dateTo`, both ends inclusive.

    Inclusive on purpose: someone picking the 1st to the 31st means the whole
    month. That's also why a DateTimeField is compared through `__date__lte`
    rather than a plain `__lte`, which would cut the last day off at midnight
    and silently drop everything collected on it.

    `is_date_field=True` for a real DateField (daily closing's `date`), where
    there is no time component to truncate.
    """
    lookup = field if is_date_field else f"{field}__date"

    date_from = parse_date(params.get("dateFrom"))
    if date_from:
        queryset = queryset.filter(**{f"{lookup}__gte": date_from})

    date_to = parse_date(params.get("dateTo"))
    if date_to:
        queryset = queryset.filter(**{f"{lookup}__lte": date_to})

    return queryset
