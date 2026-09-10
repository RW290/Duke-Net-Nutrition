# Replit prompt — backend

Copy everything below the line into Replit, after importing
`https://github.com/RW290/Duke-Net-Nutrition.git` via **Create Repl → Import
from GitHub**.

---

Deploy this existing FastAPI backend. The code is complete and tested — do NOT
rewrite it, restructure it, add a database server, or build a frontend. Your job
is only to get it running and publicly reachable.

It is a REST wrapper over Duke's CBORD NetNutrition portal. It scrapes menus and
nutrition labels and serves them as JSON to a separate frontend Repl. No API
keys, no auth, no paid services.

## Run it

Python 3.11+ (the Dockerfile targets 3.13). Install from `requirements.txt`:
fastapi, uvicorn[standard], requests, beautifulsoup4, pytest.

Run command — use exactly this:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Do not change the host or port. It must bind `0.0.0.0`, not `127.0.0.1`, or
Replit cannot route to it.

There is a `Dockerfile` if you deploy as a Docker container; otherwise ignore it
and use the run command above.

## Persistent storage — required

SQLite holds two things: a response cache and **the food log**. The log is user
data and must survive redeploys.

- Deploy as a **Reserved VM**, not Autoscale. Autoscale has an ephemeral disk
  and the food log will be silently destroyed on every redeploy.
- Attach persistent storage and set `DUKE_NUTRITION_DB` to a path on it, e.g.
  `/data/data.sqlite3`.

Do not commit or seed a database file. `data.sqlite3` is gitignored on purpose —
the schema is `CREATE TABLE IF NOT EXISTS` and builds itself on first boot. An
empty database on first run is correct, not a bug.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DUKE_NUTRITION_DB` | yes | SQLite path on persistent storage |
| `ALLOWED_ORIGINS` | no | Comma-separated CORS allowlist; defaults to `*` |

Leave `ALLOWED_ORIGINS` unset until the frontend URL is known, then set it to
the frontend's origin.

## Verify before you report success

1. `GET /health` returns `{"status": "ok", "database": "/data/data.sqlite3"}`.

   If it returns `"status": "degraded"`, the app booted but could NOT open the
   database on persistent storage — the response includes `storageError` saying
   why. Menus will work and the food log will be destroyed on every restart.
   Fix the storage before reporting success; do not treat degraded as passing.

2. `GET /units` returns a non-empty `units` array. This one hits Duke live — if
   it returns an empty list or errors, the deployment is not working, even
   though `/health` passes.
3. `/docs` loads the interactive API docs.
4. `pytest` passes (54 tests). Tests run against fixtures, not the network.
5. Per-person logs work. These two must NOT see each other's entries:

   ```bash
   curl -X POST $URL/log -H 'X-Duke-NetID: aaa111' -H 'Content-Type: application/json' \
     -d '{"label":"test","components":[{"manualName":"test","quantity":1,"calories":100}]}'
   curl $URL/log -H 'X-Duke-NetID: bbb222'     # entries must be []
   ```

Then give me the public URL of this backend. That URL is the frontend's
`API_BASE_URL` — it is a different Repl with a different URL, so do not assume
it is the same host as the frontend.

## If every endpoint returns 500

The public URL responding while *every* route fails — `/health` included — means
the app failed to import, not that a route is broken. The proxy is up; the
process behind it is not. Read the deployment log for the Python traceback
instead of testing more routes. The usual cause is `DUKE_NUTRITION_DB` pointing
at storage that isn't mounted.

## A background job runs weekly

The app schedules its own refresh every Monday at 4am US/Eastern to re-pull
Duke's dining lineup. This needs the process to stay alive between requests,
which is another reason it must be a **Reserved VM** — do not "optimize" it to
Autoscale or add a scale-to-zero setting. Do not add an external cron service or
a scheduled deployment; the job is in-process already.

## Do not

- Do not scrape Duke yourself or hardcode menu/dining-hall data — the app
  fetches it live so renamed and added locations track upstream.
- Do not add auth, accounts, or a login screen. Requests carry an
  `X-Duke-NetID` header to separate people's food logs; the backend already
  handles it. CORS stays open so the frontend Repl can send that header.
- Do not serve a UI. `/docs` is the only HTML this backend serves.
