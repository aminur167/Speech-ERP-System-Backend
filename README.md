# Speech Therapy Lab — Backend

Django + DRF API for a multi-branch speech therapy clinic ERP. Serves the
Next.js frontend at `E:\Speech ERP System`.

## Status

Phase 0 (project setup) in progress. See [`docs/ROADMAP.md`](docs/ROADMAP.md)
for the build order and [`docs/00-OVERVIEW.md`](docs/00-OVERVIEW.md) for
architecture and the rules that apply across every module.

## Stack

| | |
|---|---|
| Python | 3.14 |
| Django | 5.2 LTS (security support to April 2028) |
| API | Django REST Framework |
| Database | PostgreSQL 18 |
| Auth | SimpleJWT — short-lived access tokens, refresh rotation |
| Tests | pytest + pytest-django + factory-boy |

PostgreSQL is required, including for tests. The money logic depends on real
row-level locking (`select_for_update`) and sequences; SQLite silently no-ops
the former, which would make the concurrency tests pass while proving nothing.

## Local setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements/dev.txt
cp .env.example .env      # then fill in POSTGRES_PASSWORD
createdb speech_erp       # or via pgAdmin
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py runserver
```

`.env` is gitignored and must never be committed.

## Tests

```bash
.venv/Scripts/pytest                          # everything
.venv/Scripts/pytest -m isolation             # branch data-isolation only
.venv/Scripts/pytest -m money                 # currency / atomicity / races
.venv/Scripts/pytest --cov=apps --cov-report=term-missing
```

Markers: `isolation` guards the tenant boundary, `money` covers currency
correctness and race conditions, `slow` for the heavier concurrency tests.

## Layout

```
config/settings/     base / development / production / test
apps/common/         branch scoping, audit log, soft delete, code sequences
apps/accounts/       User (email login, role, branch)
apps/branches/       Branch — the tenant boundary
docs/                architecture and per-module specs
```

## Non-negotiables

Detail and rationale in `docs/00-OVERVIEW.md`; the short version:

- **Branch isolation** — a Manager's branch comes from their token, never from
  the request. Enforced once in `apps/common/mixins.py` and reused everywhere.
- **Money** — `DecimalField` only, `transaction.atomic()` around every
  multi-step operation, `select_for_update()` where rows are contended.
- **Idempotency keys** on write endpoints, so a retry (or a queued offline
  mutation) can never double-charge.
- **Audit trail** — append-only; soft delete on financial and medical records.
