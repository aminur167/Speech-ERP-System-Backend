"""
`@transactional` — `@transaction.atomic` that doesn't pay for nesting.

A service function that moves money must commit or roll back as one unit, so
each is decorated. But the composite flows call each other — enrolling calls
the bill collector, which calls the payment recorder — and a nested
`atomic` is a SAVEPOINT and a RELEASE: two extra round trips to the database
on every layer, to protect against nothing, because the outer block already
rolls everything back together.

    @transactional
    def collect_bill_payment(...): ...

* Called on its own, `collect_bill_payment(...)` behaves exactly as with
  `@transaction.atomic` — it opens a transaction and commits or rolls back.
* Called from a flow that is already in a transaction, use
  `collect_bill_payment.in_transaction(...)`: the same body, no new
  savepoint, and a hard refusal if no transaction is actually open (so the
  shortcut can never quietly run a money operation unprotected).

Don't use `.in_transaction` where the caller catches the exception and
carries on: without a savepoint, the failure poisons the surrounding
transaction. Callers here let it propagate.
"""

import functools

from django.db import connection, transaction


def transactional(function):
    @functools.wraps(function)
    def run_in_new_transaction(*args, **kwargs):
        with transaction.atomic():
            return function(*args, **kwargs)

    @functools.wraps(function)
    def run_in_open_transaction(*args, **kwargs):
        if not connection.in_atomic_block:
            raise RuntimeError(
                f"{function.__name__}.in_transaction must be called inside a "
                "transaction; call the function itself to open one."
            )
        return function(*args, **kwargs)

    run_in_new_transaction.in_transaction = run_in_open_transaction
    return run_in_new_transaction
