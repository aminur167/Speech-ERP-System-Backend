"""
Race-safe sequential code generation.

The frontend mock used an in-memory `sequence += 1`. Under Gunicorn with
multiple workers that collides, and a duplicate receipt number on a financial
document is a serious defect — one already shipped in the mock, where every
seeded payment shared `RCPT-2026-00000`.

Correctness here rests on `select_for_update()`: the row is locked for the rest
of the transaction, so concurrent callers queue rather than both reading the
same value. That requires PostgreSQL — SQLite silently no-ops row locking,
which is why the test settings also use PostgreSQL (docs/00-OVERVIEW.md).

Scope keys are per-branch for codes that must be issuable offline, so a branch
never has to coordinate with any other branch to hand out a receipt number
(docs/04-payments-core.md).
"""

from django.db import transaction

from apps.common.models import CodeSequence


def next_value(scope: str, year: int) -> int:
    """
    Reserve and return the next integer for `scope`/`year`.

    Must be called inside an outer transaction — the lock is only held until
    that transaction ends, and the caller needs the number and the record it
    labels to commit together.
    """
    # get_or_create first so the row exists; select_for_update can't lock a
    # row that isn't there, and two concurrent creates are resolved by the
    # unique constraint plus the retry below.
    CodeSequence.objects.get_or_create(scope=scope, year=year)

    row = CodeSequence.objects.select_for_update().get(scope=scope, year=year)
    row.last_value += 1
    row.save(update_fields=["last_value"])
    return row.last_value


def format_code(prefix: str, year: int | None, value: int, *, width: int = 5) -> str:
    """`PT-2026-00042`, or `MAT-00042` when `year` is None."""
    number = str(value).zfill(width)
    return f"{prefix}-{year}-{number}" if year is not None else f"{prefix}-{number}"


@transaction.atomic
def generate(prefix: str, *, year: int | None = None, scope: str | None = None, width: int = 5) -> str:
    """
    Convenience wrapper for callers that aren't already in a transaction.

    Prefer calling `next_value()` directly from inside the transaction that
    writes the record, so the number and the record commit or roll back
    together.
    """
    effective_scope = scope or prefix
    effective_year = year if year is not None else 0
    value = next_value(effective_scope, effective_year)
    return format_code(prefix, year, value, width=width)
