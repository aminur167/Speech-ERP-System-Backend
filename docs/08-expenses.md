# 08 — Expenses

Depends on `01-auth-and-branches.md`.

Read `docs/00-OVERVIEW.md` first for the cross-cutting rules.

Frontend source: `src/lib/api/expenses.ts`, `src/components/expenses/ExpenseListView.tsx`, `ExpenseForm.tsx`.

---

## Model: Expense

| Field | Type | Notes |
|---|---|---|
| `expense_code` | CharField, unique | `EXP-2026-00042` — race-safe generation |
| `category` | choices (8) | `rent`, `utilities`, `salaries`, `supplies`, `equipment`, `maintenance`, `marketing`, `other` |
| `amount` | **Decimal** | |
| `description` | CharField | |
| `paid_to` | CharField | Vendor/payee name |
| `payment_method` | choices | Same 7 methods as Payment (`src/utils/paymentMethod.ts`) |
| `remarks` | TextField, optional | The **manager's** own note when submitting — context for why this expense was needed |
| `is_recurring` | bool | Display flag only — nothing auto-generates recurring expenses |
| `branch` | FK → Branch | |
| `submitted_by` | FK → User | |
| `status` | choices | `pending` \| `approved` \| `rejected` |
| `review_note` | TextField | 🆕 The **Admin's** note when approving/rejecting. **Required when rejecting**, optional when approving |
| `reviewed_by` | FK → User, nullable | 🆕 Which Admin decided |
| `reviewed_at` | DateTimeField, nullable | 🆕 When |
| `created_at` | DateTimeField, indexed | |

**Expenses are NOT Payments.** They're money going out; `Payment` is money coming in. Keep them in separate tables — they're only combined at the reporting layer (e.g. dashboard "Today's Revenue" = collection − expenses, and Reports' "Net Revenue").

### Auto-approval threshold — the one real business rule

From `EXPENSE_AUTO_APPROVE_THRESHOLD = 5000` in the mock:

- `amount >= 5000` → `pending` (needs Admin approval)
- `amount < 5000` → `approved` immediately

**The threshold must be a server-side constant/setting, and status must be computed server-side** — never accepted from the request body. A client could otherwise post `status: "approved"` on a ৳50,000 expense and bypass approval entirely.

Make the threshold configurable (settings value or a DB-backed config row) rather than hardcoded in a function — a clinic will eventually want to change it, and a code deploy for that is poor design over a 10-year life.

### Approval is Admin-only, and rejection requires a reason

Managers submit; only Admin approves/rejects. `ExpenseListView` renders approve/reject controls only for Admin — enforce this server-side with a permission class.

**✅ CONFIRMED — notes on both sides:**
- **Manager** may add an optional `remarks` when submitting (already in the model) explaining the expense.
- **Admin must provide a `review_note` when rejecting.** Reject with a blank/missing reason → validation error. The manager has already spent this money, so being told "rejected" with no explanation is unworkable; it also gives the clinic a written record if the amount is later disputed or recovered from the manager.
- `review_note` is optional when approving.
- Store `reviewed_by` and `reviewed_at` on every decision.

**Frontend follow-up:** the reject action needs a reason input (a small modal, not a bare button), and both notes should be visible on the expense row/detail. Not built yet.

Every approve/reject also writes an **audit-log entry** (who, when, which expense, old → new status, the reason). The `review_note` fields are for display; the audit log is the tamper-evident history — keep both.

### ✅ CONFIRMED: decisions are reversible, with a reason

Valid transitions: `pending → approved`, `pending → rejected`, and **`approved ↔ rejected`** (Admin changed their mind).

Reversing requires a `review_note` explaining why — **including when reversing to `approved`**, which is the one case where a note isn't otherwise required. A silent flip on a money record is exactly what an audit needs to be able to explain later.

Each reversal appends a new audit-log entry (old status → new status, reason, acting Admin) — it never overwrites the previous one. `reviewed_by`/`reviewed_at` update to the latest decision, but the full history lives in the audit log.

Because reversal changes whether the expense counts toward totals (approved/pending count, rejected doesn't — see below), reversing an expense **changes reported figures for a past period**. That's correct and intended, but worth knowing when a monthly total shifts after the fact.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/expenses/` | `?search=&status=&category=&period=&date=&page=&pageSize=` |
| POST | `/expenses/` | Manager submits; server sets `status` and `submitted_by` |
| PATCH | `/expenses/{id}/` | **Admin only** — `{ status }` approve/reject |
| GET | `/expenses/summary/` | `{ total, todayTotal, monthTotal, pendingCount, voucherCount }` — `?branch=`, `?date=` |

`search` matches expense code, description, or payee. `period` is `today`/`month`; **`date` overrides `period`** when both are present (same convention as elsewhere — see `isWithinPeriod` in the mock).

`summary`'s `date` param serves the dashboard date picker: passing a past date returns *that* date's `todayTotal` and *that* month's `monthTotal`. `pendingCount` drives the Admin dashboard's approval banner and the Manager's Action Center card.

### ✅ CONFIRMED: totals count Approved + Pending, exclude Rejected

`total`, `todayTotal`, and `monthTotal` sum expenses with status `approved` **or** `pending`. `rejected` is excluded.

**Why (this is standard accrual practice):** this system records expenses *after* the manager has already spent the money — the form captures `paid_to` and `payment_method`, i.e. past tense, and sub-৳5,000 items are auto-approved with no request step at all. So a `pending` expense is money that has genuinely left the clinic; it's awaiting *verification*, not permission. Excluding it would understate spending and overstate profit. (This differs from a pre-purchase requisition workflow, where pending means no money has moved yet and should not be counted.)

`rejected` is excluded because the clinic has declined to own that expense — it becomes the manager's liability or a corrected entry, not a clinic cost.

**Also return `pendingAmount` separately** in the summary response, so the UI can show an honest total with its unfinalized portion visible:

> Monthly Expenses: ৳97,200 *(৳20,000 awaiting approval)*

Apply the identical rule to **Net Revenue** in `10-transactions-reporting.md` (collected − expenses) — the two figures must reconcile.

**Frontend follow-up:** show the pending portion alongside the expense totals on both dashboards and Reports. Not built yet.

---

## Required Tests

**Auto-approval rule (highest-value tests here)**
- Expense of ৳4,999 → `approved` automatically.
- Expense of exactly ৳5,000 → `pending` (boundary is `>=`).
- Expense of ৳5,001 → `pending`.
- **Posting `status: "approved"` on a ৳50,000 expense is ignored** — server still marks it `pending` (the privilege-escalation test).
- Threshold read from configuration, not hardcoded — changing the setting changes the behavior.

**Permissions**
- Manager `POST` → allowed; `submitted_by` set from the token, not the body.
- Manager `PATCH` to approve their own expense → 403.
- Manager `PATCH` to reject → 403.
- Admin `PATCH` approve → `approved`; reject → `rejected`.
- Approve/reject writes an audit entry naming the acting Admin.

**Review notes**
- **Rejecting without a `review_note` → validation error** (reason is mandatory).
- Rejecting with a reason → stored, and returned on the expense.
- Approving without a note → allowed.
- Approving with a note → stored.
- `reviewed_by` set from the authenticated Admin (not the body) and `reviewed_at` stamped on every decision.
- A `pending` expense has null `reviewed_by`/`reviewed_at`.
- Manager's `remarks` and Admin's `review_note` are stored and returned as **separate** fields — one never overwrites the other.

**Reversing a decision**
- `approved → rejected` succeeds when a reason is given.
- `rejected → approved` succeeds when a reason is given.
- **Reversing without a reason → validation error, in both directions** (including to `approved`).
- Each reversal appends a new audit entry; earlier entries survive (assert the audit chain length grows).
- `reviewed_by`/`reviewed_at` reflect the latest decision.
- Reversing correctly moves the amount in/out of the reported totals — reject an approved expense and assert `monthTotal` drops by exactly that amount.
- Manager attempting any reversal → 403.

**Branch isolation**
- Manager A's list excludes Branch B's expenses.
- Manager A `GET /expenses/{branch_B_id}/` → 404.
- Created expense always carries the manager's own branch, ignoring any posted branch.
- Admin sees all; `?branch=X` narrows.

**Validation & codes**
- `expense_code` format correct; concurrent creates produce unique codes.
- `amount` Decimal-exact; negative/zero → validation error.
- Invalid `category` or `payment_method` → validation error.
- Missing required fields (description, paid_to) → validation error.

**Filters**
- `status`, `category`, and `search` each narrow correctly and combine.
- `period=today` returns only today's; `period=month` only this month's.
- `date` **overrides** `period` when both are supplied.
- Pagination `count` reflects filtered totals.

**Summary — status inclusion (the rule that reporting depends on)**
- An `approved` expense **is** counted in `total`/`todayTotal`/`monthTotal`.
- A `pending` expense **is** counted.
- A `rejected` expense is **not** counted — assert directly by rejecting one and confirming the total drops.
- `pendingAmount` returns only the pending portion, and `total` minus `pendingAmount` equals the approved-only figure.
- Net Revenue in `10` uses the identical rule — the two figures reconcile against the same seeded data.

**Summary — general**
- `todayTotal` / `monthTotal` / `pendingCount` / `voucherCount` correct and branch-scoped.
- `?date=<past date>` returns that date's day-total and that month's month-total, not today's.
- Empty branch → zeros, not an error.
