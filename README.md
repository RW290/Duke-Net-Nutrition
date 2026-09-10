# Duke NetNutrition API

A personal REST backend that scrapes Duke's CBORD **NetNutrition** system so a
separate frontend (built elsewhere, e.g. Replit) can log dining-hall food and
get accurate macros — including fractional portions.

**Live frontend:** https://duke-nutrition-tracker.replit.app

Surfaced macros: **Calories, Protein, Fat, Carbs** (the full label is parsed
internally but responses project down to these four).

## Confirmed CBORD API (from live capture)

All under base `https://netnutrition.cbord.com/nn-prod/Duke`. Endpoint names,
payloads, and behaviors below were verified live, not guessed.

| Step | Request | Body | Returns |
|------|---------|------|---------|
| Establish session | `GET /` | — | HTML; sets `ASP.NET_SessionId`. Units are **server-rendered** here as `unitsSelectUnit(<unitOid>)` links. |
| Select unit | `POST /Unit/SelectUnitFromUnitsList` | `unitOid=<n>` | JSON envelope. Multi-period units → meal links in **menuPanel** (`menuListSelectMenu(<menuOid>)`); single-period units → items directly in **itemPanel**. |
| Select menu | `POST /Menu/SelectMenu` | `menuOid=<n>` | JSON envelope; **itemPanel** holds the full item table. |
| Nutrition label | `POST /NutritionDetail/ShowItemNutritionLabel` | `detailOid=<n>` | **raw HTML** fragment (`#nutritionLabel`), or an error panel. |

**Response envelope** (for the two SelectX calls): `{"success": true, "panels":
[{"id","html"}, ...]}`. Panel ids: `itemPanel, staticPanel1, childUnitsPanel,
menuPanel, coursesPanel, navBarResults, disclaimerPanel`.

### Behaviors that shape the design

- **No CSRF token.** Just the session cookie + `X-Requested-With: XMLHttpRequest`.
- **The session is stateful, and the whole chain is ordered.** The server tracks
  the currently-loaded unit and menu, and each step requires the previous one:
  - `SelectMenu(menuOid)` fails with a non-success envelope unless that menu's
    **unit is already selected** in the session. (An earlier note here claimed
    menuOids were self-contained — that was wrong; the test that produced it ran
    in a browser session that already had a unit selected.)
  - `ShowItemNutritionLabel(detailOid)` only works if that item's **menu is the
    one currently loaded**, otherwise it returns an error panel.

  So the app remembers each menu's owning unit (`menu_unit:{menuOid}` in the
  cache) and each item's context. `select_menu()` optimistically tries the menu
  first and, on failure, selects the unit and retries — self-healing after a
  restart, with no wasted request in the common case.
- **The whole menu comes back at once.** Category expand/collapse on the site is
  purely client-side; one `SelectMenu` yields every category and item.
- **Nutrition values have edge cases:** `NA` (kept as `null`, never 0), a `<`
  qualifier (`"< 5mg"` → value 5 + qualifier), and Potassium living in a
  `NoBorder` row. All handled in `app/parsers.py`.

## Our REST API

Interactive docs at **`/docs`** (Swagger UI) once running.

```
GET    /health
GET    /units                          live dining units (never hardcoded)
GET    /units/lookup?name=             resolve a place; found=false → manual entry
GET    /units/{unitId}/menus           meal periods (`periods`) or `directItems`
GET    /units/{unitId}/dishes          dish grouping (single-period units)
GET    /menus/{menuOid}/items          full menu: categories + items
GET    /menus/{menuOid}/dishes         categories clustered into buildable dishes
GET    /dish-overrides                 the manual grouping overrides in effect
GET    /items/{detailOid}/nutrition    base macros; ?menuOid= or ?unitId=
POST   /items/{detailOid}/scale        {quantity, menuOid|unitId} → scaled macros
POST   /meals/compute                  scale N components + sum → running total
POST   /log                            save a LogEntry (components + frozen total)
GET    /log?date=                      past entries + dayTotal
DELETE /log/{entryId}
POST   /admin/refresh?audit=           re-pull the lineup; ?audit=true re-sorts dishes
POST   /cache/clear?prefix=            force a live refetch
```

Add `?refresh=true` to any menu/unit GET to bypass the cache.

### Custom bowls are summed, not relayed

Duke's NetNutrition has **no build-your-own endpoint and no modifier-group
concept**. Every component of a bowl — base, protein, sauce, topping — is an
independent item with its own `detailOid` and its own add button. The
`(Choose One)` / `(Choose up to Two)` text in category headers is a
human-readable label that CBORD **does not enforce**.

So there is no CBORD-computed total to relay. A custom dish is exactly: each
component scaled by its own quantity, then summed. `POST /meals/compute`:

```jsonc
{"components": [
  {"detailOid": "292785246", "quantity": 0.5, "unitId": "7"},   // ½ sushi rice
  {"detailOid": "292785249", "quantity": 1,   "unitId": "7"},   // 1× tuna
  {"detailOid": "292785256", "quantity": 1.5, "unitId": "7"}    // 1½ eel sauce
]}
```

A component may instead carry `manualName` + macros, for food NetNutrition
doesn't cover (off-campus, or an on-campus spot not in the unit list).

**NA handling in totals:** a component whose field is `NA` contributes nothing
but is listed in the total's `incomplete` array, so a total is never silently
understated as though the NA were a real 0. A field that is NA in *every*
component stays `null`.

### ⚠️ detailOids are date-specific — don't persist them

**CBORD reissues item ids daily.** "Sushi Rice" was `292785246` on Aug 18 and
`292786260` on Aug 20 — same item, different id. Confirmed against live data.

For the frontend this means: **fetch the menu, use the ids from that response,
and don't store a detailOid to reuse tomorrow.** Store the item *name* if you
need a durable reference (e.g. for favorites), then re-resolve it against a
fresh menu.

Requesting a known-stale id returns **`410`** with `error: "stale_detail_oid"`
so this is easy to distinguish from a real failure. An id the server has never
seen is passed through to CBORD rather than pre-rejected.

This is also why logged entries freeze their totals — a past entry's detailOid
generally won't resolve later, so nutrition could never be recomputed from it.

### Whose log: the `X-Duke-NetID` header

Log endpoints are scoped to one person. Send the NetID on every `/log` request:

```
X-Duke-NetID: rw290
```

Each NetID gets its own entries and its own `dayTotal`. A request without the
header falls back to a shared `anonymous` log — the behavior from before this
existed, so an older client keeps working while it updates.

**This identifies; it does not authenticate.** The header is whatever the client
sends, and NetIDs are short and guessable, so anyone who types a friend's NetID
sees that person's log and can delete from it. That is a deliberate trade for a
small beta: it buys the same log on phone and laptop with nothing to copy
between them, which a random per-device id can't. It is not good enough for
campus-wide use.

To make it real, verify the NetID instead of trusting it — register an app with
Duke OIT for Shibboleth/OIDC and take the user key from the verified token
subject rather than the header. Ids are stored namespaced (`netid:rw290`) so
verified subjects can coexist with these without collision.

Deletes are scoped too: `DELETE /log/{entryId}` only removes an entry belonging
to the calling NetID, and returns 404 otherwise — entry ids are handed out in
log reads, so an unscoped delete would accept any id anyone had seen.

### Logging freezes the total

`POST /log` stores the full per-component breakdown *and* the total computed at
log time. Reads return stored JSON verbatim — a past entry never changes if
CBORD's menu data changes later. (Verified: after tampering with a cached item's
calories, the logged entry still read 337.5 while a fresh compute picked up the
new value.)

### Dish grouping

`/menus/{menuOid}/dishes` clusters category headers into buildable dishes by
longest shared word-prefix, so the four `Sashimi Bowl (...)` categories come back
as one dish with Base / Fish / Toppings / Dressing sections. `&` normalizes to
`and` (real case: `Salmon & Tuna Tower` + `Salmon and Tuna Tower Toppings`), and
prefixes are ranked by how many categories they cover so a station like
*Leaf and Ladle* isn't split by the longer `Leaf and Ladle Salad` sub-prefix.

This is fuzzy text matching, so **`app/dish_overrides.json` is a hand-editable
`categoryId → dish name` map that always wins.** It's seeded with Marketplace's
abbreviated `LL Salad Cheese` / `LL Sal Veg Etc` / `LL Crouton and Bread`, which
share no prefix with "Leaf and Ladle". Edits take effect on the next request; no
restart. Find current ids via `/menus/{menuOid}/dishes` (each section lists one).

### Locations not on NetNutrition

`/units` is always live, never hardcoded. `/units/lookup?name=` returns HTTP 200
with `found: false` and `fallback: "manual_entry"` for a place NetNutrition
doesn't cover — a normal path, not an error state.

## Run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8799
pytest                       # parser tests run against real captured fixtures
```

## Caching

SQLite-backed, 6h TTL for menus/units and 12h for nutrition labels. **Why SQLite
over an in-memory dict:** the cache survives restarts, which matters because this
runs on a small host that sleeps — an in-memory cache would be cold after every
wake, putting the slow CBORD chain in front of the first request each time. Cost
is a disk round trip per lookup (microseconds, irrelevant next to a ~1s CBORD
call). Measured: `/units` 1.85s cold → 0.017s warm.

*Deployment note:* on hosts with ephemeral disks, attach persistent storage or
the log database is lost on redeploy.

## New and changed dining locations

`/units` is parsed from the live landing page on every cache miss, so a new
restaurant appears on its own — within the 6h unit-list TTL, or immediately with
`/units?refresh=true`. Nothing about locations is hardcoded.

The subtle part is that **unit ids are positional**: Duke adding a location
shifts every id above it (observed live — `Red Mango` and `Sazon` appearing
mid-week renumbered everything after them). Anything cached under the old
numbering then points at the wrong venue: `unit_menus:{id}` for up to 90
minutes, `menu_unit:{menuOid}` for a day. Two things guard against it:

- **Detection.** Every refresh of the unit list compares it against the last one
  seen. A name changing under an id we already knew means a renumber, and the
  id-keyed caches are dropped immediately.
- **A weekly floor.** A background job runs every **Monday at 4am campus time**
  (`REFRESH_WEEKDAY` / `REFRESH_HOUR` in `app/main.py`), clearing cached menu
  data and re-pulling the lineup, so staleness is bounded to a week even in
  cases detection can't see. Duke changes its lineup between weeks far more
  often than mid-week. `POST /admin/refresh` runs the same thing on demand and
  returns what changed:

```jsonc
{"refreshedAt": "2026-09-14T04:00:00-04:00", "unitCount": 32,
 "added": ["Red Mango"], "removed": [], "renumbered": ["18: Sazon -> Trinity Cafe"]}
```

### A new venue sorts its own dishes

The worst grouping failure used to need a human. When every category at a venue
starts with the venue's own name — `Freeman Café Soups`, `Freeman Café Desserts`
— the matcher gets handed a prefix meaning "everything here" and collapses the
whole cafe into one meaningless 11-section dish. Fixing it took a hand-written
`null` override per category, so a newly added venue of that shape stayed broken
until someone noticed.

That case is now detected from its shape: a prefix that is *exactly the venue's
name* and swallows more sections than a real build-your-own has
(`MAX_DISH_SECTIONS`) is rejected, and the more specific prefixes below it group
instead. Both conditions are required — without the size test, an ordinary
station named after its venue (`1892 Grille` + `1892 Grille Toppings`) would
stop grouping.

It recovers most of what hand-curation gives, not all of it. On the Freeman
menu, `Freeman Café Salads` still lands standalone rather than joining the salad
builder, because prefix matching is literal and `Salads` shares no third word
with `Salad Add Ons`. The point is the floor, not the ceiling: an unattended new
venue now degrades to a slightly-split menu instead of one bogus dish.

### The weekly dish audit

The Monday job re-sorts dishes across every unit and reports what still wants a
human, since `categoryToDish` is keyed by categoryId and cannot self-heal:

```jsonc
{"unitsAudited": 31,
 "venuesAutoCorrected": ["Freeman Café"],   // sweep rejected — worth a look
 "staleOverrideIds": ["1470"],              // matches no live category any more
 "unreachable": []}
```

`venuesAutoCorrected` names venues that saved themselves; `staleOverrideIds` are
entries left behind by renamed stations or closed venues. Run it on demand with
`POST /admin/refresh?audit=true` — it samples one menu per unit, so it takes a
while. One unreachable venue is reported, never fatal to the rest.

(`standaloneUnits` is keyed by *name* rather than id for the same
renumbering reason described above.)

## Layout

```
app/
  cbord.py            session wrapper: stateful chain, expiry recovery
  parsers.py          HTML → normalized macros (units, menus, periods, labels)
  scaling.py          fractional scaling + multi-component summing
  dishes.py           category-header clustering into buildable dishes
  dish_overrides.json hand-editable grouping overrides
  db.py               SQLite cache + food log
  main.py             FastAPI routes
tests/
  fixtures/           real captured NetNutrition responses (HTML + JSON envelopes)
  test_parsers.py  test_cbord_session.py
  test_store_and_sum.py  test_dishes.py
```

## Deploying on Replit

Use the included `Dockerfile`, or configure the Replit run command as:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Attach persistent storage and set `DUKE_NUTRITION_DB` to its SQLite path so
food logs survive restarts. This needs a **Reserved VM** deployment — Autoscale
has no persistent disk, so the log resets on every restart. Replit's public URL
is the API base URL for the frontend; the interactive docs are at `/docs`.

### Troubleshooting: every endpoint returns 500

If the public URL is reachable but *all* routes fail — `/health` included — the
ASGI app failed to import; the proxy is up, the process behind it is not. Check
the deploy log for the traceback rather than the routes.

The usual cause is storage: `DUKE_NUTRITION_DB` points at a volume that isn't
mounted, so opening the database raises during import and takes the whole app
down. The app now creates a missing directory, and falls back to ephemeral
storage if it can't, so this degrades instead of blacking out. `/health` names
the problem directly:

```jsonc
// healthy
{"status": "ok", "database": "/data/data.sqlite3"}

// booted, but the volume is missing — menus work, the log won't survive a restart
{"status": "degraded", "database": "/tmp/duke-nutrition-fallback.sqlite3",
 "configuredDatabase": "/data/data.sqlite3",
 "storageError": "OSError: [Errno 30] Read-only file system: '/data'"}
```

A `degraded` response means the deployment is serving but storage is
misconfigured: confirm the deployment is a Reserved VM, that storage is attached
and mounted, and that `DUKE_NUTRITION_DB` points at a path on it.

**CORS** is enabled (default `*`) so the Replit frontend can call this API.
Restrict it to the deployed frontend:

```bash
ALLOWED_ORIGINS=https://duke-nutrition-tracker.replit.app
```

## Status

Complete: session wrapper, HTML parsing, dish grouping, fractional scaling,
multi-component meals, caching, SQLite log, and manual-entry fallback.
**58 tests pass**, verified end-to-end against live NetNutrition in production.

**No API keys, no paid services** — the whole thing runs on free infrastructure.

Receipt photo parsing was built and then deliberately removed: it was the only
feature requiring a paid API, and it wasn't worth the cost. Manual entry covers
the same ground (log any item by name with macros).

**The code is not recoverable from this repository.** It lived in
`app/receipts.py` and `tests/test_receipts.py`, but was deleted before the
first commit, so no git object here contains it — an earlier version of this
section said to look in git history, which was wrong. What it did, for anyone
rebuilding it: a vision-API extractor behind a swappable `Extractor` protocol,
feeding fuzzy name matching against the cached `menu_item` index. The index it
matched against still exists (`db.py`), so a replacement only needs the
extractor and the matcher.
