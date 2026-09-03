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

1. `GET /health` returns `{"status": "ok"}`.
2. `GET /units` returns a non-empty `units` array. This one hits Duke live — if
   it returns an empty list or errors, the deployment is not working, even
   though `/health` passes.
3. `/docs` loads the interactive API docs.
4. `pytest` passes (40 tests). Tests run against fixtures, not the network.

Then give me the public URL of this backend. That URL is the frontend's
`API_BASE_URL` — it is a different Repl with a different URL, so do not assume
it is the same host as the frontend.

## Do not

- Do not scrape Duke yourself or hardcode menu/dining-hall data — the app
  fetches it live so renamed and added locations track upstream.
- Do not add auth. The API holds no secrets; CORS is open by design.
- Do not serve a UI. `/docs` is the only HTML this backend serves.
