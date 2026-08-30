# 04 — Payments Core

Depends on `01-auth-and-branches.md`, `02-patients.md`, `03-services-catalog.md`.

Read `docs/00-OVERVIEW.md` first — **every money rule there applies here in full**. This is the most correctness-critical module in the system.

Frontend source: `src/lib/api/payments.ts`, `src/components/payments/Receipt.tsx`, `PaymentMethodSelector.tsx`, `src/utils/paymentMethod.ts`.

---

## Payment is the single source of truth for all money movement

Every rupee that enters the system becomes a `Payment` row, regardless of which flow created it:

| Flow | Creates a Payment with `category` |
|---|---|
| Daily service enrollment | `daily` |
| Monthly service bill collection | `monthly` |
| Installment payment | `installment` |
| Online booking advance | `online` |
| Materials POS sale | `material_sale` |
| Due payment collection | `monthly` or `installment` (whichever was owed) |

**All reporting reads from this one table** — dashboard metrics, revenue trends, by-method/by-category breakdowns, daily closing system totals, branch overviews. Do not add parallel money tables or per-module revenue counters; that duplication is how reporting figures drift apart from each other.

---

## Model: Payment

| Field | Type | Notes |
|---|---|---|
| `transaction_id` | CharField, unique | e.g. `TXN-...` |
| `receipt_number` | CharField, unique | e.g. `RCPT-2026-00001` — printed on receipts |
| `patient` | FK → Patient | |
| `amount` | **DecimalField** | Never float |
| `method` | choices (7) | `cash`, `bkash`, `nagad`, `rocket`, `bank_transfer`, `online_payment`, `card` |
| `status` | choices | `paid`, `due`, `upcoming`, `partial`, `cancelled`, `refunded`, `void` |
| `category` | choices, optional | `daily`\|`monthly`\|`installment`\|`online`\|`material_sale` |
| `collected_by` | FK → User | Printed on the receipt as the collecting staff member |
| `branch` | FK → Branch | Branch-scoped |
| `created_at` | DateTimeField, indexed | Every report filters/sorts on this |
| `idempotency_key` | CharField, unique, nullable | See below |
| `is_deleted` | bool | Soft delete only — financial records are never hard-deleted |

The 7 payment methods and their display labels are already centralized in the frontend at `src/utils/paymentMethod.ts` (`PAYMENT_METHOD_OPTIONS` / `PAYMENT_METHOD_LABELS`). Keep the backend's stored values identical to those keys (`bank_transfer`, not `Bank Transfer`) — the frontend maps keys to labels itself.

### ✅ CONFIRMED: receipt numbers are per-branch series

Format: **`RCPT-{BRANCH_CODE}-{year}-{5-digit}`** — e.g. `RCPT-DHK-2026-00001`, `RCPT-CTG-2026-00001`.

Each branch owns an independent sequence. This was chosen specifically to make **offline-first** workable (see `00-OVERVIEW.md`): a branch never has to coordinate numbers with any other branch, so a device that's offline can keep issuing receipts from its own series without risking a collision. It also makes reconciliation easier — the branch is readable straight off the receipt.

**Still race-safe within a branch.** Use a per-branch Postgres sequence or a `select_for_update()`-locked counter row per branch. The mock's in-memory `sequence += 1` collides under multiple Gunicorn workers, and a duplicate number on a financial document is a serious defect.

*(A related bug already occurred in the frontend: every seeded payment shared the literal `RCPT-2026-00000`. Don't recreate that class of problem server-side.)*

**✅ Confirmed: one reception device per branch.** With a single device drawing from its branch's series, offline issuance can't collide — no per-device suffix or pre-allocated ranges needed.

> If the clinic ever adds a second concurrent device at one branch, this assumption breaks and two offline devices will issue the same number. Note it in the handover documentation so nobody adds a second terminal without addressing it.

Apply the same per-branch pattern to `transaction_id`, `patient_code` (see `02`), and `booking_code` — anything that can be created offline. `expense_code` can stay org-wide (expenses aren't taken at the counter under time pressure).

### Idempotency (mandatory here)

Per the overview: the client sends a unique key per user-initiated payment action; the server returns the original Payment if that key was already processed instead of creating a second one. This protects against ordinary network retries **and** the offline-queue replay scenario. Without it, a flaky connection during a payment can double-charge a patient.

Apply to every payment-creating endpoint, including the composite flows in `06` (materials sale) and `07` (due collection).

### ✅ CONFIRMED: Void and Refund are two different things

They're often conflated, but they behave differently and carry different risk, so they get different permissions:

| | **Void** | **Refund** |
|---|---|---|
| Meaning | The transaction never really happened — caught before the money settled | Money genuinely goes back to the patient |
| Typical cause | Wrong amount typed, duplicate entry, caught immediately | Returned material, cancelled booking, service stopped |
| Money movement | None | Real cash/bKash leaves the clinic |
| Who | **Manager**, same day only | **Manager requests → Admin approves** |

Never mutate the original payment's `amount` or delete it. Status transitions to `void`/`refunded` and the original row stays intact.

---

### Void — Manager, same day only

Manager can void a payment they took **on the same calendar day**, with a required reason.

**Hard cutoff: voiding is blocked once that day's Daily Closing has been submitted.** Before closing, the day's cash is still being counted and a correction is just bookkeeping. After closing, the day has been reconciled and signed off — silently changing a settled day would invalidate the reconciliation. After that point the correction path is a refund (or an Admin closing amendment per `09`).

- Payment from a previous day → Manager gets 403 with a message pointing to the refund flow.
- Reason required; recorded in the audit log with the acting user.
- Admin can void any payment regardless of day (they own the amendment path anyway).

---

### Refund — Manager requests, Admin approves

This is **separation of duties**, the most basic financial internal control: the person who takes money must not also be able to send it back on their own authority. Otherwise pocketing cash and recording a "refund" is undetectable.

**Model: `RefundRequest`**

| Field | Type | Notes |
|---|---|---|
| `payment` | FK → Payment | |
| `amount` | **Decimal** | See partial-refund note below |
| `reason` | TextField | **Required** from the manager |
| `requested_by` / `requested_at` | FK → User / DateTime | |
| `status` | `pending` \| `approved` \| `rejected` | |
| `reviewed_by` / `reviewed_at` | FK → User / DateTime | The Admin |
| `review_note` | TextField | **Required when rejecting** |
| `bill_action` | `reopen` \| `write_off` | Admin's choice — see below |
| `refund_method` | choices | How the money went back (may differ from how it came in) |

Flow: Manager submits a request against a payment → it appears in an Admin queue (alongside the existing pending-expenses pattern, so staff learn one mental model) → Admin approves or rejects with a note. **Only on approval** does the payment become `refunded` and the side effects below fire — all inside one `transaction.atomic()`.

A payment can have at most one `pending` or `approved` refund request. Requesting a refund on an already-refunded or void payment → rejected.

### ✅ Effect on the underlying bill — Admin chooses

When the refunded payment settled a monthly bill or installment, Admin picks at approval time:

- **`reopen`** (the default, and correct in most cases) — the bill/installment returns to `due`, `paid_at` clears, and the amount **reappears in Outstanding Due**. The patient got their money back, so they owe it again.
- **`write_off`** — the bill is cancelled and does not return to Outstanding Due. For "patient is leaving, we're refunding and closing the account" situations.

Default to `reopen` in the UI. `write_off` is a deliberate decision to forgive money and must be logged as such.

This also supplies the missing write-off path noted in `05-enrollments.md` (termination is blocked while dues are outstanding — a write-off is how a genuinely uncollectable enrollment gets closed).

### ✅ Effect on material sales — stock always returns

When the refunded payment was a `material_sale`, **the sold quantities go back into stock automatically**, with a `MaterialMovement` of type `in` noting the refund's receipt number.

Consequence to be aware of: a **damaged** returned item also lands back in sellable stock. The clinic's workflow for that is a separate stock-out adjustment with a note like "damaged return" — the existing `adjust-stock` endpoint already handles it. Mention this to the client so they know the second step is required; otherwise damaged goods quietly inflate inventory value.

### ✅ CONFIRMED: partial refunds are supported

**For material sales — refund specific lines/quantities.**

`RefundRequest` carries line items:

**`RefundRequestItem`:** `refund_request` FK, `material` FK, `quantity`, `unit_price` (copied from the original sale)

- Manager picks which items and how many to return (e.g. 1 of the 3 Flashcard Sets).
- **The refund amount is computed server-side** from the original sale's recorded prices — never from a client-supplied total. Same rule as the sale itself in `06`.
- Only the returned quantities go back into stock.
- Returning more than was sold on that receipt → rejected.
- Cumulative check: two partial refunds against one sale can't exceed the original quantities or amount.
- When every line is fully returned, the payment becomes `refunded`; while some remains, it becomes **`partial`** (already in the `PaymentStatus` enum).

**For bill/installment payments — the bill reopens for the refunded portion.**

This requires a model change: add **`amount_paid`** (Decimal, default 0) to `MonthlyBill` and `Installment`, alongside the existing `amount`.

- Paying a bill sets `amount_paid = amount`, status `paid`.
- Partially refunding ৳2,000 of a ৳5,000 bill sets `amount_paid = 3,000` and status back to `due`.
- **Outstanding for that bill is `amount − amount_paid`** — ৳2,000, not the full ৳5,000.
- `07-due-payments.md`'s Outstanding Due totals must sum `amount − amount_paid` for unpaid bills, **not** `amount`. Getting this wrong overstates what the patient owes.
- A bill is `paid` when `amount_paid >= amount`; `due`/`overdue` otherwise.

> ⚠️ **Knock-on effect to be aware of:** introducing `amount_paid` means bills can now hold a partial balance, which touches the oldest-first rule (`05`), overdue detection (`05`), and every Outstanding Due calculation (`07`). Those all need to reason about the remaining balance rather than a binary paid/unpaid flag. This is unavoidable given partial refunds are required — but it's why the field must go in from the start rather than being bolted on later.
>
> It also incidentally makes **partial payments** representable (a patient paying ৳3,000 of a ৳5,000 bill). **Do not enable that yet** — the confirmed rule is full payment by the 5th, and allowing partial collection is a separate business decision. Keep the collection endpoints requiring the full remaining balance until the client asks otherwise.

**Common to both:** partial refunds still go through the Manager-requests → Admin-approves flow, and the `bill_action` choice (`reopen` / `write_off`) applies to the refunded portion.

---

### Revenue treatment

- **Refunded and void payments are excluded from revenue totals.** The frontend's `getTransactionsSummary` filters to `status === "paid"` before summing; reporting in `10` must do the same. Getting this wrong inflates every revenue figure in the system.
- **A refund is recorded in the month it was approved, not the month of the original payment** — see `10-transactions-reporting.md` for the full rule and rationale.
- Every void, refund request, approval, and rejection lands in the audit log (who, when, why).

---

## Endpoints

Payments are mostly created *through* other flows rather than posted directly, so this module's public surface is small — but the model and creation logic it owns are used everywhere.

| Method | Path | Notes |
|---|---|---|
| POST | `/payments/` | Direct creation (daily service enrollment, online booking advance). Requires idempotency key |
| GET | `/payments/{id}/` | Branch-scoped |
| POST | `/payments/{id}/void/` | **Manager** (same day, before closing) or Admin. `{ reason }` |
| POST | `/payments/{id}/refund-requests/` | **Manager** — `{ amount, reason }` |
| GET | `/refund-requests/` | `?status=pending` — the Admin approval queue |
| POST | `/refund-requests/{id}/approve/` | **Admin only** — `{ billAction, refundMethod, reviewNote? }` |
| POST | `/refund-requests/{id}/reject/` | **Admin only** — `{ reviewNote }` (required) |

Listing/reporting of payments lives in `10-transactions-reporting.md` (the frontend reads them through the denormalized "transactions" view, not a raw payments list).

### Receipt data

Receipts are rendered client-side (`Receipt.tsx`) from the Payment plus a few joined fields — no PDF generation needed server-side. The response must supply: `receiptNumber`, `transactionId`, `createdAt`, `amount`, `method`, `status`, patient name, service/item description, `collectedBy` name, and **branch name**.

Branch name specifically: it must be the branch where the transaction actually happened, resolved from the FK. A hardcoded `"Main Branch"` string was a real bug in the frontend, fixed by deriving it properly — don't reintroduce it server-side.

---

## Required Tests

This module deserves the most thorough suite in the project.

**Creation & codes**
- Payment created with correct `receipt_number` / `transaction_id` format.
- **Concurrent payment creation produces unique receipt numbers** (the race-condition test — run genuinely concurrent requests, not sequential ones).
- Receipt numbers increment without gaps or duplicates across many creates.
- `collected_by` records the authenticated user, not a client-supplied value.
- `branch` is the authenticated manager's branch, even if a different branch is posted in the body.

**Decimal correctness (guards the classic float bug)**
- `amount` of `1234.56` round-trips exactly.
- Summing many decimal amounts produces exact totals with no floating-point drift.
- Negative or zero amount → validation error.

**Idempotency**
- Two requests with the **same** idempotency key → one Payment created, both responses identical (same receipt number).
- Two requests with **different** keys → two distinct Payments.
- Replaying a key after a delay (the offline-queue case) still returns the original, doesn't create a duplicate.

**Void**
- Manager voiding a **same-day** payment before closing → succeeds, status `void`, original `amount` unchanged.
- Manager voiding a **previous-day** payment → 403.
- Manager voiding a same-day payment **after Daily Closing was submitted** → 403 (the reconciliation cutoff — test explicitly).
- Voiding without a reason → validation error.
- Admin can void regardless of day.
- Voided payment excluded from revenue totals and from that day's closing `system_total`.

**Refund request flow (separation of duties — the highest-value tests here)**
- Manager creates a refund request → `pending`; the payment is **still `paid`** (nothing changes until approval).
- **Manager attempting to approve their own request → 403.** This is the control that makes the whole thing meaningful.
- Manager attempting to approve *any* request → 403.
- Admin approves → payment becomes `refunded`, side effects fire.
- Admin rejects without a `reviewNote` → validation error.
- Admin rejects with a note → payment stays `paid`, request `rejected`.
- Requesting a refund on an already-`refunded` payment → rejected.
- Requesting a second refund while one is `pending` → rejected.
- Requesting without a reason → validation error.
- Branch isolation: Manager A cannot request a refund on Branch B's payment; Admin sees all pending requests.

**Partial refunds**
- Refunding 1 of 3 material lines → **only that quantity returns to stock**; the other two stay sold.
- The refund amount is computed from the **stored** sale price — post a tampered amount and assert the stored refund matches the database.
- Returning more than was sold → rejected.
- Two partial refunds together exceeding the original quantity/amount → the second is rejected.
- Payment status becomes `partial` after a partial refund, `refunded` only once fully returned.
- Partially refunding ৳2,000 of a ৳5,000 bill → `amount_paid` becomes 3,000, status `due`, and **Outstanding Due for that bill is ৳2,000** (not ৳5,000 — the overstatement bug this guards against).
- Paying the remaining ৳2,000 → bill returns to `paid` with `amount_paid = 5,000`.
- A fully-paid bill has `amount_paid == amount`.

**Refund side effects**
- `billAction: reopen` → the monthly bill/installment returns to `due`, `paid_at` is cleared, and **the amount reappears in Outstanding Due** (assert against the `07` summary).
- `billAction: write_off` → the bill is cancelled and does **not** reappear in Outstanding Due.
- Refunding a `material_sale` → **the sold quantities return to stock**, with a `MaterialMovement` of type `in` referencing the refund.
- Multi-line material sale → every line's quantity returns.
- **Atomicity:** force a failure after the status change but before stock restoration — assert full rollback (payment still `paid`, stock unchanged, request still `pending`).
- Refund of a bill payment makes that bill payable again, and the oldest-first rule still applies afterwards.

**Audit**
- Void, request, approve, and reject each write an audit-log entry naming the acting user and the reason.
- Approving records `reviewed_by`/`reviewed_at`; a pending request has both null.

**Branch isolation**
- Manager A `GET /payments/{branch_B_payment_id}/` → 404.
- Manager A cannot refund/void another branch's payment (403 or 404 — assert whichever, consistently).

**Atomicity**
- If a composite flow fails partway (e.g. stock deduction raises after the payment row is written), the whole transaction rolls back — no orphan Payment left behind. Test by forcing a failure mid-transaction.

**Receipt data completeness**
- Every field the receipt renders is present and correct.
- **Branch name resolves to the real branch of the transaction** — explicitly test a non-Dhaka branch payment (the exact bug that occurred in the frontend).
- Patient name, service description, and collecting user's name are all correct.
