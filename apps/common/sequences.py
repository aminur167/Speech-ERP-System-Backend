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

from django.db import connection, transaction

from apps.common.models import CodeSequence

_TABLE = CodeSequence._meta.db_table

# One statement where there used to be three (get_or_create, a locking
# SELECT, an UPDATE). On a hosted database every statement is a network round
# trip, and every payment draws two numbers, so this was six trips per
# payment before the payment itself was written.
#
# Just as race-safe: an upsert that hits the existing row takes that row's
# lock for the rest of the transaction, so a concurrent caller waits and then
# sees the incremented value — the same queueing select_for_update gave. A
# first-ever call for a scope inserts 1; two first-ever calls racing are
# resolved by the (scope, year) unique constraint, the loser falling through
# to the UPDATE branch once the winner commits.
_NEXT_SQL = f"""
    INSERT INTO {_TABLE} (scope, year, last_value) VALUES (%s, %s, 1)
    ON CONFLICT (scope, year)
    DO UPDATE SET last_value = {_TABLE}.last_value + 1
    RETURNING last_value
"""


def next_value(scope: str, year: int) -> int:
    """
    Reserve and return the next integer for `scope`/`year`.

    Must be called inside an outer transaction — the lock is only held until
    that transaction ends, and the caller needs the number and the record it
    labels to commit together.
    """
    with connection.cursor() as cursor:
        cursor.execute(_NEXT_SQL, [scope, year])
        return cursor.fetchone()[0]


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
