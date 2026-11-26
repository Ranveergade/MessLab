# MessLab deployment runbook

This is the production checklist for a small single-campus MessLab instance. It
assumes Django is served by Gunicorn behind a TLS-terminating reverse proxy and
that the mess office enters real data; no sample corpus ships.

## 1. Topology

```text
browser ──HTTPS──> reverse proxy ──HTTP──> Gunicorn (gthread)
                                             │
                                             ├── PostgreSQL (or SQLite on one node)
                                             ├── local cache table (OTP throttles)
                                             └── ml_store/ (joblib model artifacts)
```

- One web process group is enough for a campus of a few thousand members.
- SQLite is supported only for a single node. Use PostgreSQL when you run more
  than one Gunicorn worker host or need concurrent writers.
- `ml_store/` must be writable by the web process. If you run multiple hosts,
  put it on shared storage or run retraining on one host.

## 2. Prerequisites

- Python 3.11+ (CI tests Python 3.12).
- `pip install -r requirements.txt`.
- A reverse proxy that sets `X-Forwarded-Proto` and strips client-supplied
  `X-Forwarded-For`.
- SMTP credentials, or an explicit decision to keep OTP codes terminal-only.

## 3. Required environment

`messlab.settings_prod` fails closed. It refuses to start with the development
key, an empty `ALLOWED_HOSTS`, no database choice, or an accidental console mail
backend.

| Variable | Required | Purpose |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | yes | `messlab.settings_prod` |
| `SECRET_KEY` | yes | Long random value; must not start with `dev-only` |
| `ALLOWED_HOSTS` | yes | Comma-separated public host names |
| `CSRF_TRUSTED_ORIGINS` | yes for HTTPS forms | e.g. `https://mess.example.edu` |
| `DATABASE_URL` | yes unless SQLite | `postgres://user:pass@host:5432/messlab` |
| `MESSLAB_ALLOW_SQLITE` | only for SQLite | Set `1` to permit the single-node database |
| `EMAIL_BACKEND` | production | SMTP backend; console is rejected unless explicitly allowed |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | with SMTP | OTP delivery |
| `DEFAULT_FROM_EMAIL` | recommended | Sender shown in OTP mail |
| `BOOTSTRAP_ADMIN_EMAIL` | first run | Administrator created by `bootstrap` |
| `MESSLAB_ALLOW_CONSOLE_EMAIL` | only for terminal-only | Set `1` to accept console OTP delivery |
| `MESSLAB_ALLOW_SELF_REGISTRATION` | optional | `0` closes public registration |
| `MESSLAB_ALLOW_PASSWORD_LOGIN` | optional | `0` by default; keep off in production |
| `MESSLAB_SSL_REDIRECT`, `MESSLAB_SECURE_COOKIES` | optional | Leave on behind TLS |
| `MESSLAB_HSTS_SECONDS` | optional | Default `31536000` |
| `MESSLAB_TRUST_X_FORWARDED_FOR` | optional | Default `1` behind a proxy; `0` if direct |
| `MESSLAB_QR_TTL_SECONDS` | optional | Pass lifetime, default `90` |
| `MESSLAB_CACHE_BACKEND` | optional | `db` (default in production) or `locmem` |
| `JWT_SIGNING_KEY` | recommended | Separate random value from `SECRET_KEY` |
| `GUNICORN_*` | optional | Workers, threads, timeout, bind, forwarded IPs |
| `SENTRY_DSN` | optional | Error reporting; PII disabled |

Copy `.env.example` as a starting point, but do not commit the real file.

## 4. First deploy

```bash
export DJANGO_SETTINGS_MODULE=messlab.settings_prod
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export ALLOWED_HOSTS=mess.example.edu
export CSRF_TRUSTED_ORIGINS=https://mess.example.edu
export DATABASE_URL=postgres://messlab:...@db:5432/messlab
export EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
export BOOTSTRAP_ADMIN_EMAIL=admin@mess.example.edu
export DEFAULT_FROM_EMAIL='MessLab <no-reply@mess.example.edu>'

python manage.py migrate
python manage.py createcachetable
python manage.py collectstatic --noinput
python manage.py bootstrap --email "$BOOTSTRAP_ADMIN_EMAIL"
python manage.py check --deploy
gunicorn messlab.wsgi:application --config gunicorn.conf.py
```

`bootstrap` creates only four standard meal slots and the first administrator.
It creates no students, menus, check-ins or history. If the email already exists
and should become an administrator, use `--promote-existing` instead of creating
a second account.

`collectstatic` is required once per release because production uses
`CompressedManifestStaticFilesStorage`.

## 5. Health checks and probes

| Path | Meaning |
|---|---|
| `/healthz` | Process is alive; does not touch the database. |
| `/readyz` | Database connection succeeds; returns `503` when it does not. |
| `/health/` | Legacy JSON liveness used by the API docs. |

Point the platform liveness probe at `/healthz` and the readiness probe at
`/readyz`. Neither returns personal data.

## 6. Static files and the scanner

- WhiteNoise serves fingerprinted static files from Gunicorn. No CDN is needed.
- `static/vendor/jsQR.js` is vendored locally; the scanner never uploads frames.
- Camera access requires HTTPS or `localhost`. Manual student-code entry is the
  fallback when the browser or camera cannot scan.
- The CSP allows only same-origin scripts plus the vendored decoder. Django admin
  and the API docs use a relaxed policy.

## 7. Database and cache

- Run all migrations before switching traffic: `python manage.py migrate`.
- The OTP throttle cache is a database table in production. Run
  `python manage.py createcachetable` once. Without it, OTP request throttling
  raises an error.
- Schedule `python manage.py purge_personal_data --days 180 --apply` daily or
  weekly. Without `--apply` it only reports.
- Back up the database and `ml_store/` together. A model artifact without its
  training rows is still usable, but retraining and metrics need the rows.

### Backup and restore

```bash
pg_dump "$DATABASE_URL" --format=custom --file=messlab-$(date +%F).dump
tar czf ml_store-$(date +%F).tgz ml_store/

# restore into an empty database
pg_restore --clean --if-exists --dbname "$DATABASE_URL" messlab-2026-11-03.dump
```

For SQLite, stop writes, copy `db.sqlite3` and `ml_store/`, then restart.

## 8. Privacy operations

- `/privacy/` is available to every signed-in member. It exports JSON and deletes
  the account after the member types `DELETE`.
- Deletion removes the account and cascading personal rows. Aggregate
  `MealService` rows stay because they contain counts only.
- Self-registration stores the privacy-notice version and timestamp on
  `Profile.data_consent_at` / `data_consent_version`.
- `purge_personal_data` removes expired `LoginEvent`, `OTPCode` and
  `AccountAudit` rows. Audit rows that must be retained for policy reasons should
  have a longer `--days` value or be exported first.

## 9. Security checklist

- TLS terminates at the proxy; `X-Forwarded-Proto: https` reaches Gunicorn.
- `SECRET_KEY` and `JWT_SIGNING_KEY` are random, unique and stored in a secret
  manager.
- `MESSLAB_ALLOW_PASSWORD_LOGIN=0`; staff use email OTP.
- `MESSLAB_ALLOW_SELF_REGISTRATION=0` if the mess office creates accounts.
- The proxy overwrites `X-Forwarded-For`; otherwise set
  `MESSLAB_TRUST_X_FORWARDED_FOR=0`.
- SMTP credentials are present and OTP mail is tested with a real mailbox.
- `collectstatic` ran and the manifest is current.
- `/healthz` and `/readyz` are monitored; `/readyz` alerts on database loss.
- Backups are restorable and `ml_store/` is included.
- Run `python manage.py check --deploy` on every release.

## 10. Load and query smoke test

Start a staging server with a temporary manager account and password login:

```bash
MESSLAB_ALLOW_PASSWORD_LOGIN=1 python manage.py runserver 127.0.0.1:8011 --noreload
python scripts/load_test.py \
  --base-url http://127.0.0.1:8011 \
  --username loadtest --password 'local-only-pass' \
  --requests 300 --concurrency 10
```

The script logs in once and replays `/healthz`, `/manager/`, `/manager/plan/`,
`/manager/live/`, `/manager/waste/` and `/manager/menu/day/`. It prints
throughput, mean/p50/p95/p99 latency and status counts, and exits non-zero on a
5xx. Delete the temporary account afterwards.

Use `scripts/query_audit.py` against a copy of real data to catch N+1s:

```bash
python scripts/query_audit.py --user admin@mess.example.edu --all
```

Reference run on the development machine (SQLite, 4 meals, a few published
menus, 300 requests, concurrency 10): **35.1 req/s, p50 263 ms, p95 755 ms, p99
844 ms, 300/300 HTTP 200**. Query counts after the student-page batching fix:
17 for `/student/`, 22 for `/student/checkin/`, 3 for `/manager/menu/day/`, 4 for
`/manager/data/`, and 74 for the aggregate-heavy manager dashboard.

## 11. Rollback

1. Keep the previous release directory and its `staticfiles/` manifest.
2. Restore the database only if a migration is not backward-compatible; all
   MessLab migrations are reversible, but restoring is faster for a bad import.
3. Stop Gunicorn, point the symlink or platform release back, run `migrate`
   only if rolling forward the schema, and restart.
4. Confirm `/readyz`, sign in with an OTP, and open `/manager/menu/day/`.

## 12. Owner-only supplies

These cannot be created by the repository and must come from the mess owner:
real menu/dish names, planned kilograms, staff accounts, SMTP credentials, the
public host name and TLS certificate, and any retention period required by the
institution.
