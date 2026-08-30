# Build Roadmap

Order matters — each phase depends on the ones before it. Read `00-OVERVIEW.md` before starting anything.

**Definition of Done for every module:** code written → its own tests written and passing (happy path + edge cases + failure cases + branch isolation + role permissions) → *then* move on. A module without passing tests isn't finished, no matter how well it works manually.

---

## Phase 0 — Project setup
- Django + DRF + PostgreSQL, dev/staging/prod settings split
- `django-cors-headers`, `django-filter`, SimpleJWT
- Base test infrastructure + factories/fixtures
- CI running the test suite on every commit (cheap now, painful to retrofit)

## Phase 1 — `01-auth-and-branches.md` 🔑
Custom User (email login, role, branch FK), JWT auth, Branch CRUD + overview, and **the shared branch-scoping mixin every later module reuses**.

Get the isolation layer right here — everything downstream inherits it, including the holes.

## Phase 2 — `02-patients.md` (partial) + `03-services-catalog.md`
Patient CRUD with the confirmed registration fields; Service catalog (org-wide, Admin-only writes).

Skip the Patient Directory for now — it joins payments and enrollments that don't exist yet.

## Phase 3 — `04-payments-core.md` 💰
The Payment model, per-branch race-safe receipt numbering, idempotency, void (Manager/same-day), and the refund request→approval flow.

Everything financial flows through here. Slow down and get it exactly right — the tests in that doc are the most valuable in the project.

Includes **partial refunds** and the `amount_paid` field on bills/installments — build both in from the start, not later.

## Phase 4 — `05-enrollments.md`
Monthly enrollments + bills, installment plans + installments, bookings, and the **shared atomic pay-transition** (lock → create Payment → mark paid + stamp `paid_at` → promote next).

Write that transition once; `07` reuses the same function.

## Phase 5 — `06-materials.md`
Materials (branch-scoped), stock movements, and the atomic POS sell flow with server-side pricing.

## Phase 6 — `07-due-payments.md`
The aggregated dues view, collection (reusing Phase 4's transition), and **historical "due as of date" reconstruction** — the trickiest logic in the system. Re-read that doc's two traps before writing it.

## Phase 7 — `08-expenses.md` + `09-daily-closing.md`
Expenses with the server-side auto-approve threshold and Admin-only approval; daily closing with server-computed system totals and the one-per-branch-per-day constraint.

## Phase 8 — `02-patients.md` (Directory) + `10-transactions-reporting.md` 📊
Now that all the data exists: the Patient Directory's derived fields, and every reporting/dashboard/analytics endpoint.

Build with database-level aggregation from the start, and benchmark against a realistically-sized seeded dataset — not demo data.

## Phase 9 — Integration

The frontend was a client demo, so expect this phase to involve real frontend changes rather than a clean drop-in swap. That's expected and fine — the backend is the system of record, and the frontend adapts to it.

- Point the frontend's `NEXT_PUBLIC_API_BASE_URL` at the real backend, delete the mock modules under `src/lib/api/`
- Apply the frontend changes these docs flag (full list in "Frontend follow-ups" below)
- Expect additional gaps to surface here that the demo never exercised — treat each as "fix it properly", not "patch the backend to match the demo"
- Add dashboard polling (`refetchInterval`) per the no-Channels decision in `00`
- End-to-end pass over every screen against real data

## Phase 9b — Offline-first ⚠️ (own phase, not a detail)

Confirmed as required (`00-OVERVIEW.md`). Service Worker + TanStack Query offline mode + IndexedDB mutation queue, with optimistic UI and auto-flush on reconnect.

Backend prerequisites must already be in place from earlier phases: idempotency keys on **every** write endpoint, dual timestamps, and specific sync-rejection errors. **Settle the offline receipt-number scheme before Phase 3** (pre-allocated per-branch blocks recommended) — retrofitting it into the payment module later is painful.

Test it as its own thing: queue while offline, kill the browser, reopen, reconnect, and assert everything arrives exactly once. A queue that silently loses a payment is worse than no queue.

## Frontend follow-ups (accumulated from the module docs)

The frontend is a demo, so these are expected. Each is recorded in context in its module doc:

| Change | Doc |
|---|---|
| Stop displaying the manager password on the branch detail panel | `01` |
| Patient form: new fields, new required fields, age-conditional guardian section | `02` |
| Remove `registrationFee` from the Service type, form, card, and seeds | `03` |
| Package Active/Inactive toggle, "Inactive" badge, hide inactive from wizards, delete-blocked dialog | `03` |
| Replace the two-call payment flow with the single atomic endpoint | `05` |
| "Overdue — সেবা বন্ধ" badge on patient list, profile, and due rows | `05` |
| Material image upload switched to Cloudinary | `06` |
| Expense reject: reason input (modal), and show both manager/admin notes | `08` |
| Refund request flow: Manager "Request refund" action + Admin approval queue | `04` |
| Partial refund UI: pick which material lines/quantities to return | `04` |
| Patient code display/search updated for the longer per-branch format | `02` |
| Due rows show the **remaining** balance on partially-refunded bills | `07` |
| Void action (Manager, same-day) with required reason | `04` |
| Termination blocked message showing the outstanding amount + "Collect payment" action | `05` |
| Show pending portion alongside expense totals | `08` |
| Daily closing: "Adjustments today" (refunds/voids) section | `09` |
| Daily closing: Admin "Correct closing" action + "Amended" marker and history | `09` |
| Offline: sync indicator, pending count, failed-sync surfacing | `00` |

## Phase 10 — Production readiness
- Automated off-server backups **plus a tested restore**
- Sentry + uptime monitoring
- SSL with auto-renewal
- Seeded performance benchmark on production-like hardware
- Client handover: documentation and training

---

## ✅ Decisions made (all 10 original questions answered)

Each is documented in full, with rationale, in its module doc:

| # | Decision | Doc |
|---|---|---|
| 1 | Monthly bills auto-generate via a **scheduled job**; due by the **5th**; unpaid after that → `overdue`, flagged clearly but **not** auto-blocking | `05` |
| 2 | New required fields apply to **new registrations only**; guardian fields are **required for minors, optional for adults** | `02` |
| 3 | **Registration is free** — `registration_fee` dropped entirely | `03` |
| 4 | Expense totals = **Approved + Pending**, Rejected excluded; `pendingAmount` surfaced separately; **Admin must give a reason when rejecting** | `08` |
| 5 | Closing `system_total` counts **paid only**, with the day's refunds/voids **itemized alongside** | `09` |
| 6 | Closings are **append-only**; only Admin can correct, via a reasoned **amendment** that preserves the original | `09` |
| 7 | Package delete **blocked while enrollments are active** (error names the count); **Deactivate** is the alternative | `03` |
| 8 | Material images on **Cloudinary** (URL + `public_id` stored; validate against injection) | `06` |
| 9 | **Oldest unpaid first** — can't pay September while August is outstanding; error names what to pay first | `05` |
| 10 | **Full offline-first** required — see `00` for the backend constraints this imposes | `00` |

## ✅ Second round of decisions

| Decision | Doc |
|---|---|
| Receipt numbers are **per-branch series** (`RCPT-DHK-2026-00001`) — makes offline issuance safe | `04` |
| **Termination blocked while dues are outstanding** — must pay before stopping service | `05` |
| Expense decisions are **reversible with a required reason**, full history kept | `08` |
| **Void** (same-day, pre-closing) = Manager; **Refund** (real money out) = Manager requests → **Admin approves** | `04` |
| Refund's effect on the bill: **Admin chooses** `reopen` (back to Outstanding Due, the default) or `write_off` | `04` |
| Material refund **always returns stock**; damaged goods handled by a separate stock-out adjustment | `04`, `06` |
| A refund is reported in the **month it was approved** — closed months are never rewritten | `10` |

## ✅ Third round of decisions

| Decision | Doc |
|---|---|
| **One reception device per branch** — per-branch series is collision-free, no device suffix needed | `04` |
| **Partial refunds supported** — per-line for material sales, `amount_paid` on bills for the rest | `04` |
| **`patient_code` is per-branch too** (`PT-DHK-2026-00001`) — offline registration safe | `02` |

⚠️ **`amount_paid` is the ripple from partial refunds.** Bills and installments now carry a partial balance, so outstanding is `amount − amount_paid` everywhere — oldest-first (`05`), overdue detection (`05`), and all Outstanding Due totals (`07`). Build it in from the start. It also makes partial *payments* representable, but that stays **disabled** — collection still requires the full remaining balance until the client asks otherwise.

## Still open (none blocking Phase 0)

1. **Age cutoff for "minor"** — 18 assumed for the guardian requirement. (`02`)
2. **New patient fields** — `referred_by`, `chief_complaint`, `national_id`, `status`, `notes` added as defensible clinic fields; trim what nobody will fill. (`02`)
3. **Adult without guardian** — should `emergency_contact` become required so every patient has a second contact? (`02`)
4. **Monthly-bill job runner** — a management command run by system cron is recommended for a single-VPS deployment (simplest to operate, easy to trigger manually for catch-up). Celery beat only if async work is needed elsewhere. Engineering call, no client input needed. (`05`, Phase 10)
5. **Second device at a branch** — not planned, but if the clinic ever adds one the receipt/patient-code scheme breaks. Put it in the handover documentation. (`04`)
