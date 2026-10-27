# 12 — Discounts

**Status:** daily, online and counter-sale discounts built 2026-10-27, joining the monthly and installment ones already in `05-enrollments.md` and `07-due-payments.md`. This file is the one place the whole rule is written down, because it is now given in six places and they must not drift.

## The rule

One function: `apps/common/discounts.py::validated_discount`. Every discount in the product goes through it.

| | |
|---|---|
| Never negative | a "discount" that adds money is a price change wearing the wrong name |
| Never more than the charge it comes off | the ceiling is passed in by the caller and named in the message, so the manager reads *"the discount cannot be more than the admit fee (3000.00)"* |
| Never silent | above zero, a reason is required — a discount without one is money that cannot be explained to the Admin reading the branch report |

It lives in `common`, not in `enrollments`, because `materials` needs it too and a counter sale has no business importing the enrolment service layer. The caller passes its own exception class so each app keeps raising the error its views already render.

## Where a discount is recorded

**On the record that holds the charge.** That is the whole convention, and it is what keeps a reconciliation from getting two answers for one discount.

| flow | charge record | fields |
|---|---|---|
| Admit fee, enrolment month, later months | `MonthlyBill` | `gross_amount` / `discount_amount` / `discount_reason` |
| Installment package | `InstallmentPlan` | same three |
| Online session | `Booking` | same three, plus `fee_amount` |
| Daily session | `Payment` | same three |
| Counter sale | `Payment` | same three |

A payment against a bill, a plan or a booking is a **settlement**, not the charge — so it carries no discount of its own. Daily and counter sales raise nothing to settle, which is why there the Payment *is* the charge record and the discount belongs on it.

`gross_amount` is null when no discount was given, everywhere. Set, it means "a discount happened here"; `amount` is always what is actually owed or was actually taken, so balances, refunds and reports read it unchanged and need to know nothing about discounts at all.

## Discounted to nothing

Two different outcomes, and the difference is whether anything would be left to show the service happened.

- **A bill, plan or booking discounted to zero** is allowed. No payment is taken (a `Payment` must be at least 0.01) and no receipt is printed — the bill is marked settled, or the Booking simply exists with `advance_amount` of nothing. The row is the record.
- **A daily session or a counter sale cannot be.** The ceiling is one paisa short of the charge. There is no bill, so a free daily session would leave no row anywhere; and every sale line and stock movement hangs off the payment, so a free cart would take stock out of the branch with nothing to show where it went.

## Returning a discounted cart

The discount applied to the whole cart, so every line in it was effectively sold for less. A return is valued at `line_total × amount / gross_amount` — the discount shared out by what the returned lines were worth.

`MaterialSaleItem.unit_price` keeps the shelf price it was sold at: that is what the sale history and the stock movements show, and what the catalogue agrees with. Only the money handed back is scaled.

Before this, the refund was `sum(unit_price × quantity)` with no reference to the discount, so returning a cart sold for 600 off a 700 shelf price asked for 700 back. The refundable ceiling in `request_refund` kept the money in — by refusing the return outright (*"only 600 remains refundable"*), which is a refusal a manager cannot act on rather than a correct figure.

## The online advance

Separate from the discount, and worth stating plainly because it reverses an earlier decision.

The advance **used to be** `fee × BOOKING_ADVANCE_RATIO`, computed server-side and refused from the client. The clinic asked for it to be the desk's call, so it is now sent by the manager; the ratio survives only as the figure the form suggests.

What is still the server's is the **fee**. A request may say how much to collect today, never what the session costs. That was always the half that mattered — a client-chosen advance is a manager undercollecting, which they may always do; a client-chosen fee is a changed price.

`Booking` became idempotent on its own key as a direct consequence: an advance of nothing takes no payment, and the replay guard was reading the payment, so a retry would have found nothing and minted a second `booking_code` for the same slot. Same reason `MonthlyEnrollment` carries the key rather than its admission payment.

**The public website path (`create_public_booking`) keeps the fixed ratio and takes no discount.** There is no staff account behind that request and nothing to hold accountable for the figure, and a discount needs someone to give it and a reason to record.

## The daily endpoint

`POST /api/enrollments/daily/` exists because the browser used to post its own `amount` straight to `/api/payments/`. The money came out right as long as the form was honest — but a discount was given by typing a smaller number, and *nothing anywhere recorded that one had been given*, how much, or why. It was missing from the receipt, the branch report and the audit trail alike.

The fee now comes from the catalogue, as it does for every other package.

A plain `APIView`, not a ViewSet: a daily session has no row of its own to list or retrieve.

## Tests

- `apps/common/` — the shared rule itself, through its callers
- `apps/enrollments/tests/test_daily_charge.py` — 13: the fee is the catalogue's, the discount is recorded, the whole fee cannot go
- `apps/enrollments/tests/test_bookings.py` — `TestTheAdvanceIsTheDesksDecision` and `TestTheOnlineDiscount`: the manager sets the advance, the fee is not accepted from the client, a zero advance is replay-safe
- `apps/materials/tests/test_sale_discount.py` — 12: cart-level recording, the lines keep their shelf price, and a discounted cart comes back at what was collected
