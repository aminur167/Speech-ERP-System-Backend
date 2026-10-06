# Performance log

A record of every change made for speed: what it was, why, what it measured,
and how to undo it on its own. If the live site misbehaves after one of these,
start here.

## Rollback points

Both repositories carry a tag at the exact state that was live before the
performance work:

| Repository | Tag | Commit |
|---|---|---|
| Backend  | `live-before-perf-2026-09-21` | `d8986ad` |
| Frontend | `live-before-perf-2026-09-21` | `395897a` |

Undo one change — preferred, keeps everything else:

```bash
git revert <commit-of-that-change>
git push origin main
```

Go back to the tagged state entirely (creates a new commit, history is kept):

```bash
git revert --no-edit live-before-perf-2026-09-21..HEAD
git push origin main
```

Compare what changed since then: `git diff live-before-perf-2026-09-21 -- <path>`.

---

## 2026-09-21 — Pass 1

Symptom reported: on the live site every page change and button click waited
on a loading state.

Stack at the time: frontend on Vercel (static pages, calls the API from the
browser), backend on Render **free** plan in Singapore (0.1 CPU, 512 MB,
sleeps after 15 minutes idle), Postgres on Supabase free plan.

### 1. Due-payment queries no longer grow with the number of patients

- **Where:** `apps/duepayments/services.py` (`collect_due_items`)
- **Problem:** each row read its enrollment's bills (`outstanding_total()`) and
  its plan's installments (`installments.count()`) separately — one query per
  patient. `due_summary` and the branch summary go through the same function.
- **Fix:** `prefetch_related("enrollment__bills")` and
  `prefetch_related("plan__installments")`, so the related rows arrive in one
  query each. The figures are computed by the same code as before; only the
  number of queries changed.
- **Measured** (queries per request, test database):

  | Endpoint | 10 patients before | 40 patients before | after (any number) |
  |---|---|---|---|
  | `/api/due-payments/` | 18 | 63 | 5 |
  | `/api/due-payments/summary/` | 19 | 64 | 6 |
  | `/api/transactions/branch-summary/` | 32 | 77 | 19 |

  On the live site each query is also a network round trip to Supabase, so
  with a few hundred patients this was seconds per page.
- **Guard:** `apps/duepayments/tests/test_query_budget.py` fails if the count
  starts depending on the number of patients again (verified: it fails on the
  old code).
- Every other list endpoint was measured the same way and stayed constant
  (2–7 queries).

### 2. Database connections are reused

- **Where:** `config/settings/production.py`
- **Problem:** `CONN_MAX_AGE = 60` was written as a top-level setting. Django
  only reads it inside `DATABASES`, so it had no effect: every request opened
  a new TLS connection to Supabase and authenticated before its first query.
- **Fix:** `DATABASES["default"]["CONN_MAX_AGE"]` (60 s, overridable with the
  `DB_CONN_MAX_AGE` environment variable) and `CONN_HEALTH_CHECKS = True`, so
  a connection the pooler has closed is replaced instead of failing a request.
- **If the database reports too many connections:** set `DB_CONN_MAX_AGE=0`
  on Render (restores the old behaviour) — no code change needed.

### 3. The server handles requests in parallel

- **Where:** `render.yaml` start command
- **Problem:** gunicorn ran its default of one synchronous worker, so every
  request — including the sidebar badges polling every 10 s — waited in one
  line.
- **Fix:** `--workers 2 --threads 4 --timeout 60`.
- **Note:** `render.yaml` is read when the Blueprint syncs. If the service was
  created by hand, set the same start command in the Render dashboard
  (Settings → Start Command).
- **If memory runs out on the 512 MB plan:** drop to `--workers 1 --threads 4`.

### 4. API responses are compressed

- **Where:** `apps/common/middleware.py`, registered in `config/settings/base.py`
- **Fix:** gzip for API responses when the browser accepts it — JSON lists
  shrink to roughly a quarter. Responses under `/api/auth/` are never
  compressed (they carry tokens; see the BREACH note in the module).
- **Guard:** `apps/common/tests/test_compression.py`.

### 5. Keep-warm ping

- **Where:** `.github/workflows/keep-warm.yml`
- **Problem:** the Render free plan stops the service after 15 idle minutes;
  the next visitor waits 30–60 s for it to start.
- **Fix:** GitHub Actions requests `/api/health/` every 10 minutes from 08:00
  to 22:59 Bangladesh time.
- **Needs one setting:** repository variable `BACKEND_HEALTH_URL`
  (Settings → Secrets and variables → Actions → Variables). Until it is set
  the job does nothing.
- The permanent fix is a paid Render instance, which never sleeps.

### 6. Hidden tabs stop polling (frontend)

- **Where:** the five badge/notification hooks that use
  `LIVE_POLL_INTERVAL_MS` (`src/lib/livePolling.ts`)
- **Fix:** `refetchIntervalInBackground: false`. They already refetch when the
  tab regains focus, so a badge is current as soon as someone looks at it.
- **Why:** a clinic PC keeps several tabs open all day; each was sending
  requests every 10 s to a single small server.

### Not changed in code — needs a decision

- **Supabase region.** Render runs in Singapore. If the Supabase project is in
  another region, every query crosses continents (~200–300 ms each). Check
  Supabase → Project Settings → General → Region. Moving means creating a
  project in Southeast Asia (Singapore) and restoring a dump into it.
- **Old staff photos.** New uploads are resized in the browser (frontend
  `resizeImage.ts`); photos uploaded before that are still full size and are
  sent with every staff list.

## 2026-09-21 — Pass 2: instant clicks

Goal: data on screen as soon as a page is opened or a button pressed.
Supabase and Render are both in Singapore, so region was ruled out.
The rollback tags above still mark the state before both passes.

| # | Change | Repo / commit | Undo with |
|---|---|---|---|
| 7 | Revisits show the last data at once (gcTime 7 days, matching the IndexedDB cache); branches, packages, staff, settings trusted for 10 min. **Cache tied to the signed-in user** — cleared on sign-out and when someone else signs in. | frontend `b329489` | `git revert b329489` (removes both; they depend on each other) |
| 8 | Audit log and package requests keep the current page visible while the next loads | frontend `fd524ba` | `git revert fd524ba` |
| 9 | Skeleton instead of spinner (`LoadingState`, 32 places) | frontend `70a5a54` | `git revert 70a5a54` |
| 10 | Dashboard: one request instead of ten — `POST /api/batch/` (apps/common/batch.py) + `src/lib/api/batch.ts` | backend `1977ea6`, frontend `d04c0d3` | revert the frontend commit first; the endpoint alone is harmless |
| 11 | Sidebar link hover/focus/touch prefetches that page's data (`src/lib/routePrefetch.ts`) | frontend `c9e8bfd` | `git revert c9e8bfd` |
| 12 | Due Payments: remaining balance summed in SQL instead of loading every bill of every patient | backend `3edc349` | `git revert 3edc349` |
| 13 | Staff photos stored as 160 px avatars; data migration shrank existing ones | backend `3a86863` | code: `git revert 3a86863`. The **shrunk photos cannot be restored** (not kept) |
| 14 | Attendance marks show at once, roll back if refused (optimistic) | frontend `200b730` | `git revert 200b730` |

### Notes that matter later

- **Found along the way — security:** before #7, signing out did not clear
  cached data, so on a shared PC the next user could briefly see the previous
  user's patients and payments. Fixed in the same commit.
- **Found along the way — offline:** the 5-minute gcTime had silently cut the
  offline cache (meant to be 7 days) down to 5 minutes. Fixed by #7.
- **Batch endpoint guarantees** (#10): each part runs through the same view as
  the direct call, as the same user; `apps/common/tests/test_batch.py` compares
  every allowed path both ways. Only allowlisted read-only paths, GET only,
  at most 12. To batch a new endpoint, add it to `ALLOWED_PATHS` and to that
  test's comparison.
- **Optimistic updates are for attendance only.** Money (payments,
  collections, refunds, advances) must never be shown before the server
  confirms it.
- **Hover prefetch** (#11) matches each page's *opening* filters. If a page's
  defaults change and `routePrefetch.ts` is not updated, nothing breaks — the
  prefetch just stops helping.

## 2026-09-30 — Pass 3: approvals within seconds, faster money actions

Asked for: approval requests always on top of Admin's lists and updating
"instantly" (agreed: within 3–5 s, by polling — no push channel on this
plan); money actions and page changes that respond as soon as clicked.

Measured first. From the clinic's own connection, a warm request that
touches no database takes 0.13–0.24 s end to end on the live backend, so
the network is not the problem. Locally every endpoint finishes in 20–70 ms;
the live server has 0.1 CPU, which stretches that roughly tenfold, and each
database statement is a round trip to Supabase. So the lever in code is the
number of statements per action.

| # | Change | Repo / commit | Undo with |
|---|---|---|---|
| 15 | Code sequences drawn with one `INSERT … ON CONFLICT … RETURNING` instead of three statements (every payment draws two). Still race-safe — the real-thread test in `test_foundations.py` passes. | backend `147cbcb` | `git revert 147cbcb` (also undoes #16–#18) |
| 16 | Pay endpoints reuse the enrollment/plan they already loaded; response rebuilt with `select_related`. | backend `147cbcb` | as above |
| 17 | Approval lists sort pending first in the database (`apps/common/ordering.py`), before pagination, under any filter. | backend `147cbcb` | as above |
| 18 | `GET /api/approvals/pulse/` — per-branch counter bumped after commit by `post_save`/`post_delete` on the five approval models (`apps/common/approvals.py`). | backend `147cbcb` | revert the frontend commit first; the endpoint alone is harmless |
| 19 | Frontend polls the pulse every 4 s (visible tab only) and refetches queues/badges only when it moves; the three badge timers (10 s each) are gone. | frontend `5b62a97` | `git revert 5b62a97` (also undoes #20) |
| 20 | After sign-in, every sidebar page's first data loads in the background, one page at a time while the browser is idle, so the first click on any page is instant. | frontend `5b62a97` | as above |

Statements per action (local, before → after): collect monthly bill 27 → 18,
collect installment 36 → 27, enroll + admit fee 30 → 26, daily payment
13 → 9, material sale 20 → 16, expense 14 → 9.

### Notes that matter later

- **The pulse only sees saves that fire signals.** Nothing updates the five
  approval models with `QuerySet.update()`. If something ever does, call
  `apps.common.approvals.bump_on_commit(branch_id)` there too, or that change
  will reach other screens only on focus/reload.
- **Load:** with nothing changing, each open, visible tab costs one small
  request every 4 s — fewer requests than the three 10 s badge timers it
  replaced. The warm-up is a one-off trickle of ~10–15 GETs per sign-in.
- **What code cannot fix:** on 0.1 CPU the server itself is the bottleneck.
  A paid Render instance (more CPU, never sleeps) or the VPS plan below is
  the change that makes every action several times faster.

## 2026-10-06 — Pass 4: fewer database round trips per request

Asked for: the same behaviour with fewer queries — one or two where several
were used for one job. Every statement is a network round trip to the hosted
database, so this is most of what a request costs on the clinic's server.

**Measured first, then changed.** 69 endpoints, realistic data (60 patients
with bills, payments, refunds, expenses, staff, attendance), writes counted
in real transactions (BEGIN/COMMIT included). **454 → 305 queries** across
the set, and — more important than the total — most reads no longer grow with
the data. The numbers below are per request, after the login lookup.

| Endpoint | Before | After |
|---|---|---|
| Admin branches overview (7 branches) | 26 | 4 |
| Dashboard batch (8 summaries) | 27 | 12 |
| Transactions summary | 9 | 4 |
| Branch summary | 15 | 8 |
| Expenses summary | 7 | 2 |
| Patient directory summary | 5 | 2 |
| Staff summary | 6 | 4 |
| Patient directory (page) | 10 | 6 |
| Pay an installment | 29 | 10 |
| Enroll monthly + admit fee | 26 | 14 |
| Pay a monthly bill | 18 | 12 |
| Sell materials (2 items) | 19 | 12 — and flat however big the cart |
| Book a session | 16 | 11 |
| Register a patient | 9 | 6 |
| Mark a patient's attendance | 10 | 6 |
| Staff check-in | 9 | 5 |
| Request / approve a refund | 12 / 13 | 10 / 11 |

What was done, grouped by the kind of waste (rollback: `git revert` the commit
named; each is independent of the others unless noted):

1. **Same row read twice.** The branch is joined to the user at login
   (`apps/accounts/authentication.py`) so views stop re-fetching it; refund and
   adjust-stock reuse the branch already loaded; pay-bill / pay-installment read
   the enrollment or plan once and re-read only what they changed.
2. **Many figures, many queries → one conditional aggregate.** Transactions,
   dashboard metrics, directory summary, branch summary, expenses, staff,
   materials, and the due-payments summary each compute all their figures with
   `Sum(..., filter=...)` / `Count(..., filter=...)` in one query.
3. **N+1 across branches / rows.** Admin overview is grouped by branch
   (`branch_headline_figures`); outstanding dues are one UNION query instead of
   one per enrollment; the directory's three payment reads, two service reads
   and two overdue reads are one each; the activity feed reads the audit log
   once and joins the actor; refund requests join `collected_by` and their lines'
   materials; the attendance roster joins `marked_by`.
4. **Nested transactions.** `@transactional` (`apps/common/transactions.py`):
   same behaviour called alone, but `.in_transaction` variants for flows already
   inside one skip the SAVEPOINT/RELEASE pair.
5. **Several writes → one.** A payment's receipt and transaction numbers are one
   statement; a POS sale writes stock, sale lines and movements in one statement
   each; an installment collection locks the plan's installments once, works out
   every change in memory and writes them in one `UPDATE`; attendance marking and
   staff check-in/out are single `INSERT ... ON CONFLICT DO UPDATE`s; admin
   notifications are one `INSERT ... SELECT`; a booking is written once, already
   linked to its payment.

### The one that was a scaling problem, not just a count

The dashboard calls `GET /due-payments/summary/?date=<today>`, so it always took
the *historical* reconstruction path — which loaded **every bill and installment
ever created** into Python and looped over them. With ten years of data that is
the heaviest request in the app, on its most-visited page. It is now two SQL
aggregates (`apps/duepayments/services.py`). Because it is money, equivalence is
proven, not assumed: `test_summary_matches_reference.py` runs the original loops
(kept as test code) against the SQL on random bills of every status, paid before
or after the date asked about, advances, forgiven months, partial balances,
across dates and branches.

The same discipline for installment collection:
`test_installment_collection_matches_reference.py` runs the step-by-step original
beside the in-memory version through random payments (scheduled, short, over,
zero, out of order, on forgiven schedules, clearing the plan so later installments
are dropped) and requires identical installments, payments and refusals — and
asserts the random runs actually reached each hard case.

### Behaviour that changed (deliberately, and only these)

* A POS cart holding the **same material twice** is now checked against its
  *total* quantity and becomes one sale line. Before, each line was checked
  against the same starting stock, so 3 + 3 against 5 in stock was sold. This
  was a bug, not a rule.
* Locking order: a sale locks its materials in id order, and an installment
  collection locks the whole plan's installments rather than only the one being
  paid. Concurrent collections on one plan serialise (they already had to, by the
  oldest-first rule); the effect is only that they wait at the lock instead of at
  the next check.
* A branch that was soft-deleted under a logged-in manager now gets a clean
  404 on writes instead of a server error.

### Guard

`apps/common/tests/test_query_budget.py` measures every list and summary with a few
rows and again with five times as many, and fails if any endpoint's count differs
(an N+1), and gives each read and write a budget it may not exceed. Verified it
catches what it should: removing one `select_related` fails it with
`query count grew with the data`.

### What is not reduced, on purpose

The floor for a write is its own inserts and updates, the **audit entry**, a
sequence draw where it issues a number, BEGIN/COMMIT, and the **locks** that stop
two managers doing the same thing at once. Those are correctness (docs/00), not
overhead, so "everything in one or two queries" is not possible for a payment —
a bill payment is ~12 because it must lock, check oldest-first, number, insert the
payment, audit, update the bill, and read back the result. Not done: batching
several audit entries into one insert (they must stay inside the transaction),
and pushing the due-payments list and attendance roster pagination into the
database (they assemble in Python; fine now, the next thing to move at scale).

## Planned: when moving to a VPS

1. **Postgres on the same machine** as Django (or the same private network).
   Each query drops from a network round trip to well under a millisecond.
   Keep `CONN_MAX_AGE` (#2). Set up daily `pg_dump` backups off the machine
   *before* moving real data — Supabase free has no backups either, so this
   is not optional.
2. **Redis as Django's cache** for data that rarely changes: the package
   catalog, branch list, system settings, and the dashboard's month-level
   figures (by-method, by-category). Invalidate on the writes that change
   them (package/branch/settings saves, payments for the revenue figures), not
   by waiting for a timer — stale money figures are worse than slow ones.
   Configure through `CACHES` from an env var (`REDIS_URL`) so local and test
   runs keep the in-memory cache.
3. **gunicorn workers ≈ 2 × CPU cores + 1**, threads 2–4; no keep-warm ping
   needed (#5 can be deleted).
4. Region: Singapore (closest good option to Bangladesh).

## How to measure again

Count queries per endpoint with realistic data: write a throwaway test that
seeds patients, wraps `client.get(url)` in
`django.test.utils.CaptureQueriesContext`, and compares the count at two data
sizes. A count that grows with the data is an N+1. The query-budget test above
is the permanent version of this for the due-payment endpoints.
