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
