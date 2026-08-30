# 09 — Daily Closing

Depends on `01`, `04`.

Read `docs/00-OVERVIEW.md` first for the cross-cutting rules.

Frontend source: `src/lib/api/dailyClosings.ts`, `src/components/dailyClosing/DailyClosingView.tsx`.

---

## What this module does

End-of-day cash reconciliation. The system knows what it *should* have collected (sum of the day's Payments); the manager counts the actual cash/collection on hand and submits it. The difference is recorded and flagged.

This is a real internal-control mechanism — it's how a clinic detects miscounts, unrecorded transactions, or theft. Accuracy matters more than convenience here.

---

## Model: DailyClosing

| Field | Type | Notes |
|---|---|---|
| `branch` | FK → Branch | |
| `date` | DateField | The business day being closed |
| `system_total` | **Decimal** | Computed server-side — never client-supplied |
| `actual_total` | **Decimal** | The only value the manager provides |
| `difference` | **Decimal** | Computed: `actual − system` |
| `status` | choices | `matched` \| `over` \| `short` |
| `submitted_by` | FK → User | |
| `submitted_at` | DateTimeField | |

**Unique constraint on `(branch, date)`** — one closing per branch per day. The mock has no such constraint and would happily record duplicates; enforce it at the DB level.

Status rule (from `submitDailyClosing`): `difference == 0` → `matched`; `> 0` → `over` (more cash than expected); `< 0` → `short`.

### `system_total` and `difference` must be computed server-side

The client sends only `actual_total`. If the browser could supply `system_total` or `difference`, the entire control is defeated — a manager could submit a "matched" closing for any figure. Recompute both from the Payment table inside the same request.

### ✅ CONFIRMED: closings are append-only, corrected by Admin amendment

A submitted closing is **never edited in place and never deleted**. The manager cannot change their own submission at all. Only an **Admin** can correct one, and correcting means recording an amendment on top of the original — both figures survive.

This is standard accounting practice (the same reason ledgers use reversing entries rather than erasers): a manager who could quietly rewrite the number whenever cash came up short would defeat the entire purpose of daily closing as an internal control.

**Model: `DailyClosingAmendment`**

| Field | Type | Notes |
|---|---|---|
| `closing` | FK → DailyClosing | |
| `previous_actual_total` | **Decimal** | Copied from the closing before the change |
| `corrected_actual_total` | **Decimal** | The new figure |
| `reason` | TextField | **Required** — a correction with no explanation is not a correction |
| `amended_by` | FK → User | The Admin |
| `amended_at` | DateTimeField | |

On amendment: write the amendment row, then update the closing's `actual_total`, `difference`, and `status` (recomputed server-side, exactly as on original submission — never client-supplied). `system_total` is **not** amendable; it's derived from Payments and changing it would mean rewriting history.

The closing response carries its amendment history and an `isAmended` flag, so the UI can mark a corrected day rather than presenting the new figure as if it were the original.

Multiple amendments to the same closing are allowed and each is recorded — every one links back through the chain.

**Frontend follow-up:** an Admin-only "Correct closing" action requiring a reason, plus an "Amended" marker with history on the closing history table. Not built yet.

Every amendment also writes to the general audit log (`00-OVERVIEW.md`) — the amendment table is the domain record, the audit log is the tamper-evident trail.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/daily-closing/today-summary/` | System-side collection for a date: `{ total, transactionCount, byMethod[] }` — `?branch=`, `?date=` |
| GET | `/daily-closing/history/` | Past closings, newest first — `?branch=`. Includes amendment history |
| POST | `/daily-closing/` | `{ actualTotal }` → computes and stores the closing |
| POST | `/daily-closing/{id}/amend/` | **Admin only** — `{ correctedActualTotal, reason }` |

`today-summary` is used in two places, so it must be correct for arbitrary dates, not just today: the Daily Closing screen, and both dashboards' "Today's Collection" card (which feeds "Today's Revenue" = collection − expenses). The `?date=` param backs the dashboard date picker.

`byMethod` breaks the day's collection down per payment method (Cash ৳1,300, bKash ৳1,000, …), shown as chips on the closing screen so the manager can reconcile per method.

### ✅ CONFIRMED: `system_total` counts only `paid`, and refunds/voids are itemized alongside

`system_total` sums **only `status: "paid"`** payments for the day. Refunded and void payments are excluded — matching the revenue rule in `10-transactions-reporting.md`, so the closing screen and the reports never disagree.

But excluding them silently isn't enough. If ৳3,000 was refunded today, the manager counting the drawer will find less cash than they expect and have no idea why. So the endpoint **also returns the day's refunds and voids as a separate itemized list**, which the closing screen displays next to the expected total.

Response shape for `today-summary`:
```
{
  total,                  // paid only — the figure to reconcile against
  transactionCount,       // count of paid transactions
  byMethod: [...],        // paid only, per method
  adjustments: {
    refundedTotal,
    voidTotal,
    items: [ { receiptNumber, patientName, method, amount, status, createdAt }, ... ]
  }
}
```

This is standard cash-reconciliation practice: **reconcile against one clean number, but show the manager every event that moved money**, so a discrepancy is explainable rather than mysterious. A closing screen that shows only a total and a difference forces staff to guess — and guessing is how real discrepancies get waved through.

Note the cash nuance worth mentioning to the client: a refund paid out in cash physically reduces the drawer, while a refund of a bKash payment doesn't. The itemized list includes each adjustment's `method` precisely so the manager can reason about that. The system does **not** try to auto-adjust the expected cash figure for refund method — that's a judgement call that varies by how the clinic actually handles refunds, and encoding a wrong assumption would be worse than showing the facts plainly.

**Frontend follow-up:** an "Adjustments today" section on the Daily Closing screen listing refunds/voids with amount, method, and patient. Not built yet.

---

## Required Tests

**System total correctness**
- Sums only the target date's Payments — a payment from the previous or next day must not leak in (test around midnight boundaries explicitly).
- Branch-scoped: another branch's payments never contribute.
- `transactionCount` matches the number of payments summed.
- `byMethod` groups correctly and its amounts sum to `total`.
- No payments that day → `total: 0`, empty `byMethod`, not an error.
- Decimal-exact totals (no float drift across many payments).

**Refunds/voids exclusion + itemization**
- A `refunded` payment is **excluded** from `total`, `transactionCount`, and `byMethod`.
- A `void` payment is likewise excluded from all three.
- Both **appear** in `adjustments.items` with their receipt number, patient, method, amount, and status.
- `adjustments.refundedTotal` and `voidTotal` sum correctly and independently.
- No refunds/voids that day → `adjustments.items` is empty and both totals are `0`, not an error.
- The closing `system_total` for a day matches that same day's figure from `/transactions/summary/` — assert against shared seeded data so the two screens can never drift apart.
- Adjustments are branch-scoped like everything else.

**Status calculation**
- `actual == system` → `matched`, `difference: 0`.
- `actual > system` → `over`, positive difference.
- `actual < system` → `short`, negative difference.
- Decimal difference exact (e.g. system 2300.50, actual 2300.00 → −0.50, not −0.4999…).

**Server-side computation (security-relevant)**
- Posting a fake `systemTotal` or `difference` in the body is ignored — server recomputes from Payments.
- Submitting `actualTotal: 0` is valid (a genuinely zero-collection day) and yields `short` when the system total is positive.
- Missing/non-numeric `actualTotal` → validation error.

**One-per-day constraint**
- Second submission for the same branch+date → rejected.
- Same date, **different** branch → allowed (both branches close their own day).
- Same branch, different date → allowed.

**Branch isolation & permissions**
- Manager A's history excludes Branch B's closings.
- Manager A submitting a closing for Branch B → rejected (branch always taken from the token, never the body).
- **Admin cannot submit a closing** — the frontend passes `readOnly` for Admin viewing a branch; enforce server-side.
- Admin can *view* any branch's history; `?branch=X` narrows.

**History**
- Newest first.
- Includes all stored fields the UI renders (date, system, actual, difference, status).
- Empty history → empty list, not an error.

**Immutability & amendment**
- `PUT`/`PATCH`/`DELETE` on a submitted closing → rejected for **everyone**, including Admin (corrections go through `/amend/` only).
- **Manager attempting `/amend/` → 403** (this is the control that makes daily closing meaningful — test it explicitly).
- Admin amending → new `actual_total`, and `difference`/`status` **recomputed server-side**, not taken from the request.
- Amending without a `reason` → validation error.
- **The original `actual_total` survives** in `previous_actual_total` on the amendment row.
- `system_total` is unchanged by an amendment, and a request trying to modify it is ignored.
- Amending a `short` closing to a matching figure correctly flips `status` to `matched`.
- **Two successive amendments** both recorded, in order, each with its own reason and acting Admin; the closing reflects the latest figure.
- `isAmended` is false on a fresh closing, true after amendment.
- Amendment history is returned with the closing and included in `/history/`.
- Each amendment writes an audit-log entry naming the Admin and the reason.
- Admin cannot amend a closing for a branch via a mismatched id — branch scoping still applies.
