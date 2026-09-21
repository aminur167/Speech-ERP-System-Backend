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

## How to measure again

Count queries per endpoint with realistic data: write a throwaway test that
seeds patients, wraps `client.get(url)` in
`django.test.utils.CaptureQueriesContext`, and compares the count at two data
sizes. A count that grows with the data is an N+1. The query-budget test above
is the permanent version of this for the due-payment endpoints.
