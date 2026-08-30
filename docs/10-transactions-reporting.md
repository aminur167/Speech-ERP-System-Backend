# 10 — Transactions & Reporting

Depends on everything before it — build **last**.

Read `docs/00-OVERVIEW.md` first for the cross-cutting rules.

Frontend source: `src/lib/api/transactions.ts`, `src/components/transactions/*`, `src/components/reports/AdminReportsView.tsx`, both dashboard pages.

---

## What this module is

**No models of its own.** Every endpoint here is a read-only projection over `Payment` (plus joins to Patient/Branch, and Expense for net-revenue figures). It powers the transaction history screens, both dashboards, and the Admin Reports page.

### Two rules that govern everything in this module

1. **Only `status: "paid"` counts as revenue.** Refunded and void payments must be excluded from every total, trend, and breakdown. The mock is explicit about this (`getTransactionsSummary` filters to paid before summing, with the comment *"Refunded/void payments aren't real revenue"*). Getting this wrong inflates every figure on every screen — and it's the kind of error nobody notices until the client reconciles against their bank.
   - Exception: `/transactions/` (the history list) and `/transactions/refunds-voids/` deliberately **show** them; they just don't sum into revenue.

### ✅ CONFIRMED: a refund belongs to the month it was approved

If ৳5,000 was collected in August and refunded in September, **August's reported revenue stays ৳5,000** and the ৳5,000 refund lands in **September**.

This is standard accounting practice (a refund is *contra-revenue* in the current period, not a retroactive edit). Two reasons it matters here:

- **Closed periods stay closed.** August's daily closings were already reconciled and signed off; September activity must not silently rewrite them. If it could, a manager who reviewed and reported August's numbers would find them different a month later, with nothing to explain the change.
- **Reports become stable.** A figure someone screenshots or reports to the owner doesn't change underneath them.

**Implementation:** filter revenue by the payment's `created_at`, but exclude it only from the period in which its refund was **approved** — i.e. a payment counts as revenue in its own month, and the refund is a separate negative event dated to the approval. Practically this means reporting queries need both the payment date and the refund-approval date, not just the payment's current `status`.

> ⚠️ **This conflicts with the simple `status == "paid"` filter above.** A payment refunded in September has `status = "refunded"` today, so a naive status filter would remove it from *August* as well — retroactively changing a closed month, which is exactly what this rule forbids. Reporting must therefore be driven by **dated events**, not by the payment's current status alone. Resolve this properly when building `10`; getting it wrong silently rewrites history.

Expose refunds as their own reported figure (`refundedTotal` for the period) so Net Revenue reads honestly: *gross collected − refunds − expenses*.

**Void is different:** a voided transaction never happened at all, so it's removed from its **original** day/month entirely. Voids are same-day-only precisely so this never disturbs a closed period.

2. **`date` always overrides `period`** when both are supplied (see `isWithinPeriod` in the mock). Same convention as `08` and `09`.

---

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/transactions/` | Paginated list, newest first |
| GET | `/transactions/summary/` | `{ totalCollected, transactionCount, todayCollected, monthCollected, byMethod[] }` |
| GET | `/transactions/refunds-voids/` | All refunded/void payments, newest first |
| GET | `/transactions/trend/` | `?days=7` → `[{ date, label, amount }]`, oldest first |
| GET | `/transactions/by-method/` | This month by payment method, descending |
| GET | `/transactions/by-category/` | This month by service category |
| GET | `/transactions/dashboard-metrics/` | `{ todayPatientsSeen, todayDueCollected }` |
| GET | `/transactions/collection-for-date/` | Single total for one date |

All accept `?branch=` (Admin) and are branch-scoped for Managers. `/transactions/` also accepts `search`, `method`, `status`, `patientId`, `period`, `date`, `page`, `pageSize`.

### Notes on individual endpoints

**`/transactions/`** — denormalized: each row carries `patientName` and `patientCode` alongside the payment fields, so the table renders without a second round trip. `search` matches patient name, patient code, receipt number, or transaction ID. Sorted by `created_at` descending.

**`/transactions/summary/`** — takes `?date=` for the dashboard date picker: `todayCollected` becomes *that date's* total and `monthCollected` *that month's* total. `totalCollected` is all-time. `byMethod` sorted by amount descending.

**`/transactions/trend/`** — one bucket per calendar day for the last `days` days, **including days with zero collection** (the chart needs a continuous axis — don't return a sparse list). Oldest first. `label` is a short display date (`"Aug 22"`).

**`/transactions/by-category/`** — excludes `material_sale`, since it charts *service* categories. (`getRevenueByCategory` filters it out explicitly.)

**`/transactions/dashboard-metrics/`** — `todayPatientsSeen` is the count of **distinct patients** with a paid payment that day (not payment count). `todayDueCollected` sums only `monthly` + `installment` category payments — i.e. money collected against outstanding dues, not new sales.
> Note: the Manager dashboard no longer displays `todayDueCollected` (that card was replaced with "Today's Revenue"), but the Admin dashboard still uses `todayPatientsSeen`. Keep both fields — dropping the unused one is a false economy if the client asks for a due-collection metric later.

**Net revenue** (Admin Reports) = total collected − total expenses. This is the one place `Payment` and `Expense` are combined; both must use the same status-inclusion rules decided in `04` and `08` or the figure won't reconcile.

---

## Performance — this is where a 10-year system dies

Every endpoint here scans payments. On day one that's 9 rows; after a decade of multi-branch operation it's easily hundreds of thousands. Design for that now:

- **Aggregate in the database** (`Sum`, `Count`, `TruncDate`, `values().annotate()`), never by pulling rows into Python and reducing. The mock fetches everything and reduces in JS because it has no database — do not port that shape.
- **Indexes** on `payment(branch_id, created_at)`, `payment(status)`, `payment(category)`, `payment(method)` — every query here filters on some combination of these.
- **Avoid the N+1 join** for patient name/code on the transaction list — `select_related("patient")`.
- **Consider caching** the expensive all-time aggregates (`totalCollected`) with short TTLs if they prove slow — but measure first; premature caching hides correctness bugs.
- **Benchmark against a realistically-sized seeded dataset** (e.g. 500k payments across 4+ branches), not against demo data. An endpoint that's fine at 9 rows and 8 seconds at 500k is a failure you want to discover now, not after handover.

---

## Required Tests

**Revenue exclusion rules (highest-value tests in this module)**
- A `void` payment is **excluded** from `totalCollected`, `todayCollected`, `monthCollected`, `trend`, `by-method`, and `by-category` — removed from its original period entirely.
- Both refunded and void payments still **appear** in `/transactions/` and `/transactions/refunds-voids/`.
- Seed a mix of paid/refunded/void and assert the exact expected revenue figure.

**Refund period attribution (the rule that's easy to get wrong)**
- Payment collected in August, refunded in September → **August's `monthCollected` still includes it**. Assert directly; a naive `status == "paid"` filter fails this test.
- The same refund appears in **September's** `refundedTotal`.
- Net Revenue for September = collected − refunds − expenses, including that refund.
- Net Revenue for August is **unchanged** by the September refund.
- A payment collected and refunded **within the same month** nets to zero for that month.
- A **voided** payment (same-day only) is removed from its own day and month — contrast this with refunds in the same test file so the distinction is pinned down.
- Re-running August's report after the September refund returns the identical figure as before — the closed-period stability guarantee.

**Summary**
- `todayCollected` counts only today's paid payments; `monthCollected` only this month's.
- `?date=<past date>` shifts both to that date and that date's month.
- `byMethod` amounts sum to the paid total; sorted descending.
- Decimal-exact totals across many payments.
- Branch-scoped; another branch's payments never contribute.
- Empty branch → zeros/empty arrays, not errors.

**Trend**
- Returns exactly `days` buckets, oldest first.
- **Days with no collection appear with `amount: 0`** rather than being omitted.
- Bucket boundaries are correct around midnight.
- Refunded/void excluded from bucket amounts.

**By-category**
- `material_sale` is excluded.
- Each service category totals correctly.
- Only the current month's payments included.

**Dashboard metrics**
- `todayPatientsSeen` counts **distinct patients**, not payments — a patient with 3 payments today counts once.
- `todayDueCollected` sums only `monthly` + `installment` categories, excluding `daily`, `online`, `material_sale`.
- `?date=` shifts both to the given date.

**Transaction list**
- Sorted newest first.
- `search` matches patient name, patient code, receipt number, and transaction ID.
- `method` / `status` / `patientId` filters each narrow correctly and combine.
- `date` overrides `period` when both supplied.
- `patientName`/`patientCode` correctly joined; a payment whose patient was soft-deleted still renders sensibly rather than erroring.
- Pagination `count` reflects the filtered total, not the whole table.

**Branch isolation**
- Manager A's transactions/summary/trend/metrics exclude Branch B entirely — assert on **every** endpoint in this module, not just the list.
- Admin sees all; `?branch=X` narrows correctly on every endpoint.

**Net revenue**
- Equals total collected − total expenses, using the same status rules as `04`/`08`.
- Negative net (expenses exceed collection) is returned correctly rather than clamped to zero.

**Performance**
- `assertNumQueries` bounds on the transaction list and each dashboard endpoint, so an N+1 regression fails the suite.
- A seeded large-dataset benchmark for the dashboard endpoints, with an agreed response-time budget.
