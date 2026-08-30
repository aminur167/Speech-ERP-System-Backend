# Speech Therapy Lab — Backend Architecture Overview

**Status:** Planning only. No backend code exists yet. Frontend (Next.js, currently mock-data-only) lives at `E:\Speech ERP System` and is functionally complete — this backend must serve it without requiring frontend changes beyond swapping `NEXT_PUBLIC_API_BASE_URL` and deleting the mock modules under `src/lib/api/`.

## The frontend is a demo, not a specification

The frontend was built to show the client what the product will feel like. Its `src/lib/api/*.ts` mock modules are a good starting point for the API contract — response shapes, field names, what each screen needs — and this doc set (`01`–`10`) is largely derived from reading them.

**But the mock is not authoritative on correctness.** It has no database, no concurrency, no auth boundary, and no security model, so it does several things a production backend must not. Expect gaps and expect to find more.

**The backend is the system of record.** Design it properly on its own terms:
- Where the mock is unsafe or wrong → build it correctly, and note the required frontend change in that module's doc.
- Where the mock is missing something the system genuinely needs → add it; a gap in a demo is not a reason to ship an incomplete backend.
- Never weaken backend design to avoid changing the frontend. The frontend can be updated afterwards; that's the cheaper side to change.

Known deviations are called out inline in the module docs (marked ⚠️). The main ones: plaintext manager passwords on Branch (`01`), client-supplied pricing in the materials sale (`06`), the non-atomic two-call payment flow (`05`), and in-memory sequence generation for receipt/patient/expense codes (`04`, `02`, `08`). Each has an industry-standard replacement documented — hashed credentials, server-side price lookup, a single atomic transaction, and DB-level sequences respectively.

**Quality bar:** this must be built to industry best-practice standard, able to comfortably support the business for 10 years of accumulated data, and stay fast. Not a shortcut-taking MVP. See "Engineering checklist" below — it applies to every module, not just the ones that mention it explicitly.

**Definition of Done for any module:** code written → its own automated tests written and passing (happy path + edge cases + failure cases + branch isolation + role permissions) → only then move to the next module. A module without passing tests is not finished, regardless of whether it appears to work manually.

---

## Confirmed Stack

| Concern | Choice | Why |
|---|---|---|
| Framework | Django + Django REST Framework | Frontend already assumes DRF's exact conventions (see below) |
| Database | PostgreSQL | ACID guarantees matter — this system tracks real money |
| Auth | SimpleJWT, access+refresh, **short-lived access token** | Frontend sends `Authorization: Bearer <token>` (see `src/lib/api/client.ts`) |
| Real-time | **None (no Django Channels)** | See "Real-time strategy" below |
| Filtering | django-filter + DRF SearchFilter | Covers almost all `?search=&status=&category=&page=` patterns with minimal custom code |
| CORS | django-cors-headers | Frontend is a separate origin (Next.js dev server / deployed domain) |

### Why the frontend dictates DRF conventions exactly
`src/lib/api/client.ts` is already wired for:
- Base URL `http://localhost:8000/api` (override via `NEXT_PUBLIC_API_BASE_URL` in production)
- `Authorization: Bearer <token>` on every request
- Paginated list responses shaped exactly like DRF's default pagination: `{ count, next, previous, results }` (see `src/types/api.ts` — literally commented *"Mirrors Django REST Framework's default paginated list response."*)
- Validation errors shaped like DRF's default: `{ field_name: ["message"] }` (see `ApiFieldErrors` in the same file)
- A generic error fallback reading `error.response.data.detail` or `.message`

None of this needs custom work on the Django side — it's what DRF gives you by default. Don't build a custom response envelope; the frontend doesn't expect one.

---

## Real-Time Strategy: No Channels

**Decision:** Do not use Django Channels / WebSockets for this project.

"Real-time" here means Admin sees a Manager's newly-collected payment reflected within seconds, not milliseconds. That's covered by:
- TanStack Query's `refetchOnWindowFocus` (default behavior, already active) — switching back to a dashboard tab refetches automatically.
- Add `refetchInterval` polling (~15–30s) on dashboard-only queries (transactions summary, due-payments summary, dashboard-metrics).

Channels would require Redis (channel layer) + running Django under ASGI — real operational complexity (another service to deploy, secure, and keep alive) for a benefit that's mostly cosmetic here. This is a one-time sale to a client who will likely have a small/no dedicated ops team — keep the deployment surface as small as it can be while still being correct. Revisit only if a specific feature genuinely needs push (e.g., an instant "new payment" toast) — and even then, scope it to that one feature, not the whole architecture.

---

## Branch Data Isolation — Non-Negotiable

Every branch's data must be invisible to every other branch's Manager. Admin sees everything. This is standard multi-tenant row-level isolation and it must be enforced **server-side**, never trusted from the client.

**Rules:**
1. **Never read `branch_id` from the request for a Manager.** Derive it from `request.user.branch` (the authenticated user's own branch) and force that filter onto every queryset. If a Manager's request includes `?branch=branch-2` in the URL, ignore it — always use their own token-derived branch.
2. **Admin has no branch filter** by default, and may optionally pass `?branch=<id>` to scope into one branch (mirrors the already-built Admin branch drill-down UI).
3. **Scope detail/edit endpoints too, not just list endpoints.** A Manager must not be able to `GET /patients/{id}/` for another branch's patient by guessing/enumerating the ID, even though the list endpoint correctly filters. Return 404 (don't leak existence via 403 vs 404 distinction) for out-of-scope objects.
4. **Implement once, reuse everywhere.** Build a single permission class / queryset mixin (e.g. `BranchScopedQuerySetMixin`) and apply it to every ViewSet that touches branch-scoped data: Patients, Payments, Expenses, Materials, MaterialMovements, MonthlyEnrollments, InstallmentPlans, Bookings, DailyClosing, DuePayments. Reimplementing this per-module is exactly how real isolation bugs happen (correct on 8 of 9 endpoints, forgotten on the 9th).
5. **Write an explicit automated test for this.** Log in as Branch A's manager, attempt to access Branch B's patient/payment/expense by ID, assert rejection. This is the one thing worth a dedicated test regardless of overall test coverage.

---

## Money-Handling Rules (apply to every module that touches currency)

- **`DecimalField` only for money — never `FloatField`.** Floating point causes real rounding errors in accounting; this is non-negotiable.
- **`transaction.atomic()` around every multi-step money operation** — e.g. payment creation + bill status update, material sale + stock deduction. Partial failure must never leave the system in a half-done state (money deducted but nothing recorded, or vice versa).
- **`select_for_update()` on rows being paid/adjusted** to prevent race conditions — e.g. two requests trying to collect the same due bill simultaneously.
- **Idempotency keys on write endpoints, especially payment creation.** The client generates a unique key per user-initiated action (UUID); the server checks "have I already processed this key?" and returns the original result if so, instead of creating a duplicate. This matters for both normal network retries and the offline-sync scenario below. This is the same pattern Stripe uses for payment API calls — copy it, don't reinvent it.
- **Refund/void actions are Admin-only**, not Manager — enforce via permission class, not just UI hiding.
- **Auto-generated sequential codes** (`receiptNumber`, `patientCode`, `expenseCode`, `bookingCode`, material `code`) must be race-safe under concurrent requests. Use a Postgres sequence or a counter row locked with `select_for_update()` — the mock's in-memory `counter += 1` does not translate safely to a multi-worker production server.

---

## ✅ CONFIRMED: Full Offline-First

The client's branch internet is unreliable enough that work must continue without it. **Build the full offline-first path**, modeled on how Square/Shopify POS handle offline sales — a clinic cannot turn away a paying patient because the WiFi dropped.

### Frontend behavior (the target UX)

1. **Real connectivity detection** — not just `navigator.onLine` (which only reports whether a network interface exists, not whether the server is reachable). Use an actual reachability check.
2. **Draft autosave** — form input persists locally as it's typed, so a drop mid-form loses nothing.
3. **Optimistic UI** — the action appears to succeed immediately (patient appears in the list, payment shows in history) with a small "Syncing…" indicator. Never a blocking error dialog.
4. **Auto-retry with exponential backoff** — TanStack Query does this by default; keep it.
5. **Persistent queue** — unsent mutations go to an IndexedDB-backed outbox that survives refresh and browser restart (like an email Outbox).
6. **Auto-flush on reconnect** — the queue drains by itself; staff do nothing.
7. **Visible pending count** — a small badge showing how many items haven't synced, so nobody closes the laptop mid-queue unaware.

Mechanism: Service Worker + TanStack Query `onlineManager` / `networkMode: 'offlineFirst'` + an IndexedDB persister.

### Backend requirements this creates

Offline-first is mostly a frontend build, but it imposes hard constraints on the API. These are **not optional** if the queue is to be safe:

- **Idempotency keys on every write endpoint, not just payments.** Any queued mutation can replay — a patient registration, a stock adjustment, an expense. The key is generated client-side when the user acts, and the server returns the original result on replay. This is the single most important requirement here.
- **Dual timestamps.** The device supplies when the action happened; the server records when it arrived. Store both (`client_created_at` and `server_received_at`) and treat the client's clock as untrusted — an offline device's clock can be wrong or tampered with. Reports should use the server timestamp unless there's a specific reason not to.
- **Sync-time rejection must be recoverable.** A queued sale may fail on arrival because stock ran out, or a queued bill payment may violate the oldest-first rule because another payment landed first. The API must return a clear, specific reason so the UI can show *"3 sales failed to sync — Flashcards Set was out of stock"*, not a silent drop. Never discard a rejected queued item; surface it for staff to resolve.
- **Receipt numbers are the hard problem.** They're server-assigned and sequential, but a patient paying cash offline needs a receipt *now*. Options, in order of preference:
  1. **Pre-allocated blocks per branch** — each branch draws a reserved range it can assign offline. This is what real offline-capable POS terminals do.
  2. A branch-prefixed provisional number reconciled on sync (receipt reprints with the final number).
  
  Decide this before building the payment module — it affects the receipt-number scheme in `04-payments-core.md`. Don't leave it to integration time.
- **Daily closing can't be computed offline** — `system_total` is derived server-side from Payments. If a branch closes the day while offline, the closing must wait for sync, and the UI must say so rather than showing a wrong total. Flag any unsynced payments on the closing screen.

### Scope note

This is a substantial piece of work — larger than any single module in `01`–`10`, and it touches both codebases. Treat it as its own phase with its own testing (see `ROADMAP.md` Phase 9), not as a detail folded into another module. Its correctness matters as much as the money logic does: a queue that silently loses a payment is worse than no queue at all.

---

## Engineering Checklist (cross-cutting, applies everywhere)

- **Audit trail:** a real audit log table for who approved/rejected/terminated/edited what — not just `created_at`. Soft-delete patients and payments; never hard-delete financial or medical records.
- **Backups:** automated daily backups, stored off-server, and *periodically test-restored*. An untested backup is not a real backup.
- **Performance for 10-year data growth:** indexes on `branch_id`, `created_at`, and patient search fields from day one. `select_related`/`prefetch_related` discipline on join-heavy endpoints (Patient Directory is the worst offender — see `docs/02-patients.md`) to avoid N+1 queries as data scales.
- **Security:** re-validate everything server-side that the frontend already validates — never trust client input. Short-lived access tokens with refresh rotation. Rate-limit the login endpoint. Prefer an httpOnly cookie for token storage over the frontend's current JS-accessible store, to reduce XSS token-theft risk (a frontend change to make when wiring up real auth).
- **Monitoring:** error tracking (e.g. Sentry) and uptime monitoring (e.g. UptimeRobot) from the first day of deployment, not added later.
- **Testing — mandatory per module, not deferred to the end:** every module gets its own automated test suite covering happy path, edge cases, and failure cases *before moving to the next module*, not bundled into one big testing pass at the end of the project. Each module doc (`docs/02` through `docs/10`) lists the concrete test cases expected for that module — treat that list as a gate, not a suggestion. At minimum, every module's tests must include: (1) the normal successful flow, (2) invalid/missing input rejected correctly, (3) branch-isolation is enforced (a Manager from another branch cannot read or write this module's data), (4) role permission is enforced (Manager vs Admin-only actions), (5) the specific business-logic edge cases called out in that module's doc.
- **Deployment discipline:** separate dev/staging/production environments, never test against the live client database, auto-renewing SSL (Certbot).

---

## Document Index

| File | Covers |
|---|---|
| `01-auth-and-branches.md` | User model, JWT auth, Branch CRUD + overview |
| `02-patients.md` | Patient model, CRUD, Patient Directory (denormalized listing), active-services |
| `03-services-catalog.md` | Service model (shared, not branch-scoped), CRUD, enrollment counts |
| `04-payments-core.md` | Payment model — the single source of truth for all money movement |
| `05-enrollments.md` | MonthlyEnrollment/Bill, InstallmentPlan/Installment, Booking |
| `06-materials.md` | Material + MaterialMovement, stock adjustment, POS-style sell flow |
| `07-due-payments.md` | Aggregated due-items view, collection action, historical "due as of date" reconstruction |
| `08-expenses.md` | Expense model, auto-approve threshold rule |
| `09-daily-closing.md` | DailyClosing model, system-vs-actual reconciliation |
| `10-transactions-reporting.md` | All reporting/analytics/dashboard endpoints |
| `ROADMAP.md` | Build order across all of the above |
