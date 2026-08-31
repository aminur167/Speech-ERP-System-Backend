# Deployment Guide

Everything needed to take this from a laptop to a running production server:
environment separation, the reverse proxy + SSL, process management, error
tracking, and backups. Written for a single Ubuntu 22.04/24.04 LTS VPS (the
same target CI already tests against — see `.github/workflows/ci.yml`),
because that is the simplest thing to operate correctly for a single-clinic
deployment of this size (docs/00's Phase 10 rationale). Everything here also
transfers directly to a second VPS later if a client needs true blue-green
deploys.

Config files referenced below live in `deploy/` in this repo:

```
deploy/
├── nginx/
│   ├── api.speech-erp.conf     # backend reverse proxy + TLS
│   └── app.speech-erp.conf     # frontend reverse proxy + TLS
├── systemd/
│   ├── speech-erp-backend.service
│   └── speech-erp-frontend.service
└── scripts/
    ├── backup_db.sh            # nightly pg_dump, off-server copy, retention
    ├── restore_db.sh           # restore a dump (real recovery or the drill below)
    └── verify_backup_restore.sh # weekly automated restore drill
```

---

## 1. Architecture

Two subdomains, not one origin with path-based routing:

- **`app.yourdomain.com`** → Next.js (`next start`), the frontend.
- **`api.yourdomain.com`** → Django/DRF via gunicorn, the backend.

This isn't arbitrary — Django serves its admin panel at `/admin/`, and the
Next.js app has its own `/admin/*` route segment. One origin would collide.
Two subdomains sidesteps it entirely and matches how `CORS_ALLOWED_ORIGINS`
is already set up for cross-origin calls between them.

```
                    ┌─────────────────────┐
 Browser ─────────► │  nginx (443, certs)  │
                    └──────────┬───────────┘
                 app.yourdomain.com │ api.yourdomain.com
                               ▼             ▼
                    ┌─────────────────┐  ┌──────────────────────┐
                    │ Next.js :3000   │  │ gunicorn (unix sock)  │
                    │ (systemd)       │  │ (systemd)             │
                    └─────────────────┘  └──────────┬────────────┘
                                                       ▼
                                              PostgreSQL 18
```

---

## 2. Server prep (one-time)

```bash
sudo adduser --system --group --home /opt/speech-erp speecherp
sudo mkdir -p /opt/speech-erp/backend /opt/speech-erp/frontend
sudo mkdir -p /etc/speech-erp /var/log/speech-erp /var/backups/speech-erp
sudo chown -R speecherp:speecherp /opt/speech-erp /var/log/speech-erp /var/backups/speech-erp

sudo apt update
sudo apt install -y python3.14 python3.14-venv postgresql-18 nginx certbot python3-certbot-nginx nodejs npm
```

Install Node 22 LTS via [NodeSource](https://github.com/nodesource/distributions)
rather than the Ubuntu-packaged one if it's older than the frontend needs —
check `engines` in `package.json` before trusting `apt`'s version.

### PostgreSQL

```bash
sudo -u postgres createuser speecherp --pwprompt
sudo -u postgres createdb speech_erp --owner speecherp
```

Use the password you set here as `POSTGRES_PASSWORD` in step 4.

---

## 3. Environment files

Two files, root-owned, mode `600`, readable only by the `speecherp` user's
systemd services (via `EnvironmentFile=`) — never committed:

**`/etc/speech-erp/backend.env`** — copy `.env.example` from this repo, fill
in real values. Critically:
- `DJANGO_SECRET_KEY` — generate fresh, don't reuse the dev one.
- `DJANGO_ALLOWED_HOSTS=api.yourdomain.com`
- `CORS_ALLOWED_ORIGINS=https://app.yourdomain.com`
- `POSTGRES_*` — from step 2.
- `SENTRY_DSN`, `SENTRY_ENVIRONMENT` — see §7.

**`/etc/speech-erp/frontend.env`** — copy `.env.example` from the frontend
repo:
- `NEXT_PUBLIC_API_BASE_URL=https://api.yourdomain.com/api`
- `NEXT_PUBLIC_SENTRY_DSN`, `SENTRY_DSN` — see §7.

```bash
sudo chmod 600 /etc/speech-erp/backend.env /etc/speech-erp/frontend.env
sudo chown speecherp:speecherp /etc/speech-erp/backend.env /etc/speech-erp/frontend.env
```

---

## 4. Backend deploy

```bash
sudo -u speecherp git clone <backend-repo-url> /opt/speech-erp/backend
cd /opt/speech-erp/backend
sudo -u speecherp python3.14 -m venv .venv
sudo -u speecherp .venv/bin/pip install -r requirements/prod.txt

export DJANGO_SETTINGS_MODULE=config.settings.production
sudo -u speecherp -E .venv/bin/python manage.py migrate
sudo -u speecherp -E .venv/bin/python manage.py collectstatic --noinput
sudo -u speecherp -E .venv/bin/python manage.py createsuperuser  # first deploy only

sudo cp deploy/systemd/speech-erp-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now speech-erp-backend
```

## 5. Frontend deploy

```bash
sudo -u speecherp git clone <frontend-repo-url> /opt/speech-erp/frontend
cd /opt/speech-erp/frontend
sudo -u speecherp npm ci
sudo -u speecherp env $(cat /etc/speech-erp/frontend.env | xargs) npm run build

sudo cp /opt/speech-erp/backend/deploy/systemd/speech-erp-frontend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now speech-erp-frontend
```

`next build` reads `NEXT_PUBLIC_*` vars at build time and inlines them into
the client bundle — the env must be present during `npm run build`, not just
at runtime. Any change to a `NEXT_PUBLIC_*` value requires a rebuild.

## 6. Nginx + SSL (Certbot, auto-renewing)

```bash
sudo cp /opt/speech-erp/backend/deploy/nginx/*.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/api.speech-erp.conf /etc/nginx/sites-enabled/
sudo ln -s /etc/nginx/sites-available/app.speech-erp.conf /etc/nginx/sites-enabled/
# Edit both files first: replace yourdomain.com with the real domain.

sudo mkdir -p /var/www/certbot
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d api.yourdomain.com
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
```

Certbot rewrites the server blocks with the real cert paths and installs its
own systemd timer (`certbot.timer`) that renews automatically before
expiry — nothing further to schedule. Confirm it's active:

```bash
systemctl status certbot.timer
sudo certbot renew --dry-run
```

---

## 7. Error tracking (Sentry)

Both apps are already wired to initialize Sentry the moment a DSN is
present, and to no-op cleanly if it isn't — a first deploy is never blocked
on Sentry being set up:

- Backend: `config/settings/production.py`, gated on `SENTRY_DSN`.
- Frontend: `instrumentation.ts` / `instrumentation-client.ts` / `sentry.server.config.ts`, gated on `SENTRY_DSN` / `NEXT_PUBLIC_SENTRY_DSN`.

Both share the same non-negotiable: `send_default_pii` / `sendDefaultPii` is
`false`. Patient names, phone numbers, and payment details pass through this
app constantly — Sentry must never capture request bodies, headers, or PII
by default.

To enable: create one Sentry project per app (or two environments in one
project), set the DSN + `SENTRY_ENVIRONMENT=production` in both env files,
redeploy. Trace sample rate defaults to `0.1` (10%) — a low-traffic clinic
backend doesn't need full tracing.

## 8. Uptime monitoring

Point an external checker — [UptimeRobot](https://uptimerobot.com) or
equivalent — at:

```
GET https://api.yourdomain.com/api/health/
```

This endpoint (`apps/common/views.py::HealthCheckView`) is unauthenticated,
does zero DB queries, and exists specifically so external monitoring never
needs credentials or touches real load. It's also what the frontend's own
connectivity detection polls (`src/lib/offline/connectivity.ts`) — the same
endpoint proves both "is the backend up" and "can the app tell the
difference between offline and a real outage."

Set the check interval to 5 minutes and alert on 2 consecutive failures
(avoids paging on a single dropped packet).

## 9. Backups

```bash
# Nightly full backup, off-server copy, 14-day local retention.
sudo crontab -u speecherp -e
# add:
15 2 * * * SPEECH_ERP_ENV_FILE=/etc/speech-erp/backend.env /opt/speech-erp/backend/deploy/scripts/backup_db.sh >> /var/log/speech-erp/backup.log 2>&1

# Weekly automated restore drill -- proves the backups are actually
# restorable, not just that pg_dump exited 0.
0 3 * * 0 SPEECH_ERP_ENV_FILE=/etc/speech-erp/backend.env /opt/speech-erp/backend/deploy/scripts/verify_backup_restore.sh >> /var/log/speech-erp/restore-drill.log 2>&1
```

`backup_db.sh` needs **one** of these set in `backend.env` to actually go
off-server (it warns loudly if neither is set):

- `BACKUP_S3_BUCKET` — uploads via `aws s3 cp` (requires `awscli` installed
  and IAM credentials configured for the `speecherp` user).
- `BACKUP_REMOTE_HOST` (+ `BACKUP_REMOTE_USER`, `BACKUP_REMOTE_DIR`,
  `BACKUP_REMOTE_SSH_KEY`) — rsyncs to a second server over SSH.

A real disaster-recovery restore (not the drill) uses the same script
directly: `deploy/scripts/restore_db.sh <dump-file>` — restoring into the
live database name requires typing `CONFIRM`, since it drops and replaces
its contents.

---

## 10. Staging

Staging is **the same `config.settings.production` module**, not a forked
settings file — the whole point of a staging environment is to run what
production actually runs. It's distinguished purely by environment
variables: a second VPS (or a second set of systemd services + nginx server
blocks on the same box), its own `backend.env` / `frontend.env` pointing at
a separate database and a separate subdomain (`api-staging.yourdomain.com`,
`app-staging.yourdomain.com`), and `SENTRY_ENVIRONMENT=staging` so errors
there don't page anyone as if they were real production incidents. Deploy to
staging first, verify, then repeat the same steps against production.

---

## 11. Release checklist

For every deploy after the first:

```bash
# Backend
cd /opt/speech-erp/backend
sudo -u speecherp git pull
sudo -u speecherp .venv/bin/pip install -r requirements/prod.txt
sudo -u speecherp -E .venv/bin/python manage.py migrate
sudo -u speecherp -E .venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart speech-erp-backend

# Frontend
cd /opt/speech-erp/frontend
sudo -u speecherp git pull
sudo -u speecherp npm ci
sudo -u speecherp env $(cat /etc/speech-erp/frontend.env | xargs) npm run build
sudo systemctl restart speech-erp-frontend
```

Before restarting either service: confirm CI is green on the commit being
deployed (`.github/workflows/ci.yml` already gates this on every push to
`main`), and check the uptime monitor + Sentry right after restart, not the
next morning.
