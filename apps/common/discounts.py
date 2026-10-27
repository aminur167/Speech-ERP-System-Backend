"""
One discount, checked the same way wherever it is given.

A discount is allowed at six places now -- the admit fee, the enrolment
month, a later month on the Due Payments page, an installment package, a
daily session and a counter sale -- and in every one of them it means the
same three things: not negative, not more than the charge it comes off, and
never silent (a discount without a reason is unexplainable money).

Keeping the rules in one function is the only way those six cannot drift
apart. It lives here, not in `enrollments`, because `materials` needs it too
and a POS sale has no business importing the enrolment service layer.

The caller supplies its own exception class so each app keeps raising the
error its views already know how to render; every one of them takes the same
`(message, code=..., extra=...)` shape.
"""

from decimal import Decimal


def validated_discount(
    amount,
    reason: str,
    *,
    ceiling: Decimal,
    what: str,
    ceiling_code: str,
    ceiling_key: str,
    error: type[Exception],
) -> Decimal:
    """
    Returns the discount as a Decimal, or raises `error`.

    `what` names the charge in the message the manager reads ("admit fee",
    "cart total"); `ceiling_code` and `ceiling_key` let the frontend tell a
    too-large discount apart from the other refusals and show the limit.
    """
    value = Decimal(amount or 0)

    if value < 0:
        raise error("A discount cannot be negative.", code="invalid_discount")
    if value > ceiling:
        raise error(
            f"The discount cannot be more than the {what} ({ceiling}).",
            code=ceiling_code,
            extra={ceiling_key: str(ceiling)},
        )
    if value > 0 and not (reason or "").strip():
        raise error(
            f"Say why a discount is being given on the {what}.",
            code="discount_reason_required",
        )
    return value
