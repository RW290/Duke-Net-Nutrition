"""Duke NetNutrition REST API.

A clean REST wrapper over Duke's CBORD NetNutrition system, for a personal
food-logging app. This file wires HTTP routes to the CBORD session wrapper, the
HTML parsers, and SQLite storage. A separate frontend (built elsewhere) consumes
these endpoints — no UI is served here beyond FastAPI's own /docs.

Endpoints:
  GET    /health
  GET    /units                          live dining units (never hardcoded)
  GET    /units/lookup?name=             resolve a place by name; found=false ->
                                         frontend falls back to manual entry
  GET    /units/{unitId}/menus           meal periods (or direct items)
  GET    /menus/{menuOid}/items          full menu: categories + items
  GET    /items/{detailOid}/nutrition    base macros (?menuOid= or ?unitId=)
  POST   /items/{detailOid}/scale        macros * quantity
  POST   /meals/compute                  scale N components + sum -> total
  POST   /log                            save a LogEntry (components + total)
  GET    /log?date=                      past entries (stored totals, as logged)
  DELETE /log/{entryId}
  POST   /cache/clear                    force-refresh cached menus

Runs entirely on free infrastructure — no API keys or paid services required.

The four macros surfaced are Calories, Protein, Fat, Carbs. The full label is
parsed internally but responses project down to those.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import re
import sqlite3
import tempfile
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import dishes, parsers
from .cbord import CbordClient, CbordError
from .db import (
    ANONYMOUS_USER,
    DEFAULT_DB_PATH,
    DEFAULT_TTL_SECONDS,
    FRESH_ITEM_SECONDS,
    MENU_TTL_SECONDS,
    Store,
)
from .scaling import scale_macros, sum_macros

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

# --- weekly refresh ------------------------------------------------------------

# Duke changes its dining lineup between weeks far more often than mid-week —
# locations open, close for a break, or get renamed over a weekend. Refreshing
# early Monday means the first person logging breakfast sees the new lineup.
REFRESH_WEEKDAY = 0   # Monday
REFRESH_HOUR = 4      # 04:00 campus time; nobody is logging food
_CAMPUS_TZ = ZoneInfo("America/New_York")


def _seconds_until_next_refresh(now: Optional[dt.datetime] = None) -> float:
    """Seconds from `now` until the next Monday at REFRESH_HOUR, campus time.

    Computed in the campus timezone rather than UTC so the job stays at 4am
    local across daylight-saving changes instead of drifting an hour twice a year.
    """
    now = now or dt.datetime.now(_CAMPUS_TZ)
    target = now.replace(hour=REFRESH_HOUR, minute=0, second=0, microsecond=0)
    target += dt.timedelta(days=(REFRESH_WEEKDAY - now.weekday()) % 7)
    if target <= now:
        target += dt.timedelta(days=7)
    return (target - now).total_seconds()


def refresh_menu_data() -> dict:
    """Drop cached menu data, re-pull the live unit list, report what moved.

    Every response is already sourced live, so this isn't about the correctness
    of one request — it's about the caches. Unit ids are POSITIONAL: Duke adding
    a location shifts every id above it, so anything still cached under the old
    numbering points at the wrong venue. Doing this on a schedule bounds that to
    a week even in the cases the automatic detection can't see.
    """
    before = store.get_known_units()
    for prefix in ("units", "unit_menus:", "menu_unit:"):
        store.cache_clear(prefix)
    store.prune_menu_items(0)          # detailOids are re-indexed on next fetch

    units = _fetch_units()
    after = {u["id"]: u["name"] for u in units}
    renumbered = sorted(f"{uid}: {before[uid]} -> {after[uid]}"
                        for uid in before.keys() & after.keys()
                        if before[uid] != after[uid])
    report = {
        "refreshedAt": dt.datetime.now(_CAMPUS_TZ).isoformat(timespec="seconds"),
        "unitCount": len(units),
        "added": sorted(set(after.values()) - set(before.values())),
        "removed": sorted(set(before.values()) - set(after.values())),
        "renumbered": renumbered,
    }
    if report["added"] or renumbered:
        logger.warning(
            "dining lineup changed (%d added, %d ids reassigned) — re-audit "
            "app/dish_overrides.json: categoryToDish is keyed by categoryId, and "
            "a new venue's stations have no overrides until someone adds them.",
            len(report["added"]), len(renumbered),
        )
    return report


def audit_dish_grouping() -> dict:
    """Re-run dish sorting across the whole lineup and report what needs a human.

    The sorting itself is not something to "re-run" for correctness — it happens
    live on every /dishes request. What this catches is the state around it:

    - **Venues that self-corrected.** A venue-wide prefix now gets rejected
      automatically instead of collapsing the menu into one pseudo-dish. That
      recovers most of what a hand-written override would give, not all of it,
      so these are the venues most worth a human's eye.
    - **Overrides that have gone stale.** categoryToDish is keyed by categoryId;
      when a station is renamed or a venue closes, its entry silently stops
      matching anything and just accumulates in the file.

    Samples one menu per unit rather than every meal period — enough to see
    which categories and overrides are live, at roughly two CBORD calls per
    unit instead of a few hundred for the whole lineup.
    """
    overrides = dishes.load_overrides()
    seen_ids: set[str] = set()
    autohealed: list[str] = []
    failed: list[str] = []

    for unit in _fetch_units():
        try:
            payload = unit_menus(unit["id"])
            if payload.get("directItems"):
                cats = payload["directItems"]["categories"]
            elif payload.get("periods"):
                cats = menu_items(payload["periods"][0]["menuOid"])["categories"]
            else:
                continue
            seen_ids.update(str(c.get("categoryId")) for c in cats)
            if _dishes_for(cats, unit_id=unit["id"]).get("venueSweepsRejected"):
                autohealed.append(unit["name"])
        except Exception as exc:
            # One unreachable venue must not abort the audit of the rest.
            failed.append(f"{unit['name']}: {exc}")

    return {
        "unitsAudited": len(_fetch_units()) - len(failed),
        "venuesAutoCorrected": sorted(autohealed),
        "staleOverrideIds": sorted(cid for cid in overrides if cid not in seen_ids),
        "unreachable": failed,
    }


async def _weekly_refresh_loop() -> None:
    """Run refresh_menu_data() every Monday for as long as the process lives."""
    while True:
        await asyncio.sleep(_seconds_until_next_refresh())
        try:
            # Blocking: hits CBORD over the network and writes SQLite.
            report = await asyncio.to_thread(refresh_menu_data)
            logger.info("weekly refresh complete: %s", report)
            audit = await asyncio.to_thread(audit_dish_grouping)
            logger.info("weekly dish audit: %s", audit)
            if audit["venuesAutoCorrected"] or audit["staleOverrideIds"]:
                logger.warning(
                    "dish grouping needs review — auto-corrected venues: %s; "
                    "stale override ids: %s. Both are in app/dish_overrides.json.",
                    audit["venuesAutoCorrected"] or "none",
                    audit["staleOverrideIds"] or "none")
        except Exception:
            # Never let one bad week (CBORD down, network blip) kill the loop.
            logger.exception("weekly refresh failed; retrying next Monday")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_weekly_refresh_loop())
    logger.info("weekly refresh scheduled in %.1f hours",
                _seconds_until_next_refresh() / 3600)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    lifespan=lifespan,
    title="Duke NetNutrition API",
    version="0.2.0",
    description="Personal backend for logging Duke dining macros. "
                "Backend only — the frontend is built separately.",
)
client = CbordClient()


def _open_store() -> tuple[Store, Optional[str]]:
    """Open the store, falling back to ephemeral storage if the configured path
    is unusable.

    DUKE_NUTRITION_DB normally points at a mounted volume (/data/data.sqlite3).
    When that mount is missing or read-only, a bare `Store()` raises here at
    import time — which fails the whole ASGI app, so *every* route including
    /health returns 500 and the only evidence is a traceback in the deploy log.
    Booting degraded keeps the failure legible: menus still work, and /health
    reports the storage problem directly.
    """
    try:
        return Store(), None
    except (sqlite3.Error, OSError) as exc:
        fallback = os.path.join(tempfile.gettempdir(), "duke-nutrition-fallback.sqlite3")
        logger.error(
            "cannot open the database at %s (%s) — falling back to %s. "
            "Attach persistent storage and set DUKE_NUTRITION_DB to a path on it; "
            "until then the food log will NOT survive a restart.",
            DEFAULT_DB_PATH, exc, fallback,
        )
        return Store(fallback), f"{type(exc).__name__}: {exc}"


store, _storage_error = _open_store()

# The frontend is built and hosted separately (Replit), so browser requests come
# from a different origin and would be blocked without CORS. Set
# ALLOWED_ORIGINS to a comma-separated list to restrict it; the default is open,
# which is acceptable only because this API holds no secrets and no auth —
# revisit if you ever add either.
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Nutrition labels are per-item and effectively static for a menu's lifetime;
# cache them a bit longer than menus.
_LABEL_TTL = 12 * 60 * 60


@app.get("/health")
def health():
    """Liveness, plus whether storage landed on the configured path.

    Always 200 when the process is up. `status` is "degraded" rather than "ok"
    if the app fell back to ephemeral storage, so a misconfigured volume is
    visible here instead of only in the deploy logs.
    """
    if _storage_error is None:
        return {"status": "ok", "database": store.path}
    return {
        "status": "degraded",
        "database": store.path,
        "configuredDatabase": DEFAULT_DB_PATH,
        "storageError": _storage_error,
        "warning": "using ephemeral storage — the food log will be lost on restart",
    }


@app.get("/")
def root():
    """Deployment readiness endpoint and pointer to the API documentation."""
    return {
        "status": "ok",
        "service": "Duke NetNutrition API",
        "docs": "/docs",
    }


# --- units ---------------------------------------------------------------------

def _fetch_units() -> list[dict]:
    """Units, cached. Always sourced live from NetNutrition's landing page —
    never a hardcoded list — so added/renamed/removed locations track upstream."""
    cached = store.cache_get("units")
    if cached is not None:
        return cached
    units = parsers.parse_units(client.get_landing_html())
    _drop_caches_if_unit_ids_shifted(units)
    store.cache_set("units", units, ttl=DEFAULT_TTL_SECONDS)
    store.upsert_units(units)
    return units


def _drop_caches_if_unit_ids_shifted(units: list[dict]) -> None:
    """Clear id-keyed caches when Duke renumbers its dining units.

    Unit ids are positional, so adding a location shifts every id above it.
    Cached `unit_menus:{id}` entries then serve another venue's menu for up to
    90 minutes, and `menu_unit:{menuOid}` mappings stay wrong for a day. A name
    changing under an id we already knew is the tell.
    """
    known = store.get_known_units()
    shifted = [u for u in units if u["id"] in known and known[u["id"]] != u["name"]]
    if not shifted:
        return
    logger.warning(
        "dining unit ids shifted (%d reassigned, e.g. id %s: %r -> %r) — "
        "clearing id-keyed menu caches",
        len(shifted), shifted[0]["id"], known[shifted[0]["id"]], shifted[0]["name"],
    )
    store.cache_clear("unit_menus:")
    store.cache_clear("menu_unit:")
    store.prune_menu_items(0)


@app.get("/units")
def list_units(refresh: bool = Query(False, description="bypass cache")):
    if refresh:
        store.cache_clear("units")
    return {"units": _fetch_units()}


@app.get("/units/lookup")
def lookup_unit(name: str = Query(..., description="place name to resolve")):
    """Resolve a place name against the live NetNutrition unit list.

    Not every Duke dining location is covered by NetNutrition. A miss is a normal
    path, not an error: this returns 200 with `found: false` and
    `fallback: "manual_entry"` so the frontend can route to manual entry the same
    way it does for off-campus food.
    """
    needle = name.strip().lower()
    units = _fetch_units()
    exact = [u for u in units if u["name"].lower() == needle]
    partial = [u for u in units if needle and needle in u["name"].lower()]
    match = (exact or partial or [None])[0]
    if match is None:
        return {"found": False, "query": name, "fallback": "manual_entry",
                "message": f"'{name}' is not listed in NetNutrition; log it manually.",
                "candidates": units}
    return {"found": True, "query": name, "unit": match}


def _remember_menu_units(unit_id: str, periods: list[dict]) -> None:
    """Record which unit each menuOid belongs to.

    CBORD requires a menu's unit to be selected before SelectMenu will work, and
    /menus/{menuOid}/... has no other way to know the owner. Kept longer than the
    menu cache so the mapping outlives the menu payload.
    """
    for period in periods:
        if period.get("menuOid"):
            store.cache_set(f"menu_unit:{period['menuOid']}", unit_id,
                            ttl=DEFAULT_TTL_SECONDS * 4)


@app.get("/units/{unit_id}/menus")
def unit_menus(unit_id: str, refresh: bool = Query(False)):
    """Meal periods for a unit.

    Multi-period units return `periods` (each with a menuOid). Single-period
    units have no period links; their items load on unit selection, so they come
    back under `directItems` with an empty `periods` list.
    """
    key = f"unit_menus:{unit_id}"
    if refresh:
        store.cache_clear(key)
    cached = store.cache_get(key)
    if cached is not None:
        # Re-assert the menu->unit mapping even on a cache hit; it may be
        # missing (older cache entries) and /menus/{menuOid}/... depends on it.
        _remember_menu_units(unit_id, cached.get("periods") or [])
        return cached

    try:
        panels = client.select_unit(unit_id)
    except CbordError as exc:
        raise HTTPException(status_code=502, detail={
            "error": "unit_select_failed", "unitId": unit_id, "message": str(exc)})
    periods = parsers.parse_menu_periods(panels["menuPanelHtml"])
    _remember_menu_units(unit_id, periods)
    direct = None
    if not periods and panels["itemPanelHtml"].strip():
        direct = parsers.parse_menu(panels["itemPanelHtml"])
        _index_menu_items(direct, unit_id=unit_id, menu_oid=None)
    if not periods and direct is None:
        raise HTTPException(status_code=404, detail={
            "found": False,
            "message": f"unit {unit_id} returned no menu periods or items",
        })
    payload = {"unitId": unit_id, "periods": periods, "directItems": direct}
    store.cache_set(key, payload, ttl=MENU_TTL_SECONDS)
    return payload


# --- menus ---------------------------------------------------------------------

def _index_menu_items(menu: dict, unit_id: Optional[str], menu_oid: Optional[str],
                      date: Optional[str] = None, meal: Optional[str] = None) -> None:
    """Record each item's identity + how to reach it again (the stateful
    nutrition call needs a menuOid or unitId), for later lookup and for receipt
    fuzzy matching."""
    rows = []
    for cat in menu.get("categories", []):
        for item in cat.get("items", []):
            rows.append({
                "detailOid": item["detailOid"],
                "unitId": unit_id,
                "menuOid": menu_oid,
                "name": item["name"],
                "categoryId": item.get("categoryId"),
                "categoryHeader": cat.get("header"),
                "servingSizeText": item.get("servingSizeText"),
                "date": date,
                "mealPeriod": meal,
            })
    if rows:
        store.upsert_menu_items(rows)


def _resolve_menu_unit(menu_oid: str) -> Optional[str]:
    """Which unit owns this menuOid, discovering it if not already known.

    Normally the mapping was recorded when the frontend listed the unit's menus.
    If it wasn't (cold cache, or the frontend jumped straight to a menu), walk
    the units once to find the owner and record every mapping found along the
    way. Bounded by the unit count (~6) and only runs on a miss.
    """
    known = store.cache_get(f"menu_unit:{menu_oid}")
    if known:
        return known
    for unit in _fetch_units():
        try:
            panels = client.select_unit(unit["id"])
        except CbordError:
            continue
        periods = parsers.parse_menu_periods(panels["menuPanelHtml"])
        _remember_menu_units(unit["id"], periods)
        if any(p.get("menuOid") == str(menu_oid) for p in periods):
            return unit["id"]
    return None


@app.get("/menus/{menu_oid}/items")
def menu_items(menu_oid: str, refresh: bool = Query(False)):
    """Full menu for a date+meal (menuOid): categories and their items."""
    key = f"menu_items:{menu_oid}"
    if refresh:
        store.cache_clear(key)
    cached = store.cache_get(key)
    if cached is not None:
        return cached

    owning_unit = _resolve_menu_unit(menu_oid)
    try:
        html = client.select_menu(menu_oid, unit_oid=owning_unit)
    except CbordError as exc:
        # Most likely cause: this menuOid's unit was never fetched in this
        # session, so we can't select it first. Tell the caller how to fix it.
        raise HTTPException(status_code=502, detail={
            "error": "menu_select_failed",
            "menuOid": menu_oid,
            "message": str(exc),
            "hint": "call GET /units/{unitId}/menus for the owning unit first — "
                    "CBORD requires the unit selected before a menu.",
        })
    menu = parsers.parse_menu(html)
    if not menu["categories"]:
        raise HTTPException(status_code=404, detail={
            "found": False,
            "message": f"menu {menu_oid} returned no items",
        })
    _index_menu_items(menu, unit_id=owning_unit, menu_oid=menu_oid)
    payload = {"menuOid": menu_oid, **menu}
    store.cache_set(key, payload, ttl=MENU_TTL_SECONDS)
    return payload


# --- dish grouping -------------------------------------------------------------

def _unit_name(unit_id: Optional[str]) -> Optional[str]:
    """Look up a unit's display name from the live list (ids shift; names don't)."""
    if not unit_id:
        return None
    for unit in _fetch_units():
        if str(unit["id"]) == str(unit_id):
            return unit["name"]
    return None


def _dishes_for(categories: list[dict], unit_id: Optional[str] = None) -> dict:
    return dishes.group_categories(categories, dishes.load_overrides(),
                                   unit_name=_unit_name(unit_id),
                                   standalone_units=dishes.load_standalone_units())


@app.get("/menus/{menu_oid}/dishes")
def menu_dishes(menu_oid: str, refresh: bool = Query(False)):
    """Menu categories clustered into buildable multi-part dishes.

    e.g. the four 'Sashimi Bowl (...)' categories become one dish with Base /
    Fish / Toppings / Dressing sections. Categories that belong to no
    multi-part dish are returned under `standalone`.

    `selectionHint` ('pick_one'/'pick_any') is inferred from header text and is
    a DISPLAY HINT ONLY — CBORD enforces no such constraint.
    """
    menu = menu_items(menu_oid, refresh=refresh)
    return {"menuOid": menu_oid, **_dishes_for(menu["categories"],
                                              unit_id=_resolve_menu_unit(menu_oid))}


@app.get("/units/{unit_id}/dishes")
def unit_dishes(unit_id: str, refresh: bool = Query(False)):
    """Dish grouping for a single-period unit (items load on unit selection)."""
    payload = unit_menus(unit_id, refresh=refresh)
    if not payload.get("directItems"):
        raise HTTPException(status_code=400, detail={
            "message": f"unit {unit_id} is multi-period; call "
                       f"/menus/{{menuOid}}/dishes with a menuOid from /units/{unit_id}/menus",
            "periods": payload.get("periods", []),
        })
    return {"unitId": unit_id,
            **_dishes_for(payload["directItems"]["categories"], unit_id=unit_id)}


@app.get("/dish-overrides")
def get_dish_overrides():
    """The manual categoryId -> dish name map currently in effect.

    Edit app/dish_overrides.json to correct groupings the matcher gets wrong;
    changes take effect on the next request (no restart needed).
    """
    return {"categoryToDish": dishes.load_overrides(), "path": dishes.OVERRIDES_PATH}


# --- item nutrition ------------------------------------------------------------

def _looks_stale(detail_oid: str) -> bool:
    """True only when we've indexed this item AND every sighting is old.

    Deliberately conservative: an id we've never seen (empty cache, or a menu the
    frontend fetched but we didn't index) is NOT called stale — we let CBORD be
    the judge rather than rejecting a possibly-valid request.
    """
    if store.find_item_context(detail_oid, max_age_seconds=FRESH_ITEM_SECONDS):
        return False                      # seen recently — fine
    return store.find_item_context(detail_oid) is not None   # seen, but only long ago


def _invalidate_menu_caches(menu_oid: Optional[str], unit_id: Optional[str]) -> None:
    """Drop cached menu payloads whose item ids are evidently no longer valid."""
    if menu_oid:
        store.cache_clear(f"menu_items:{menu_oid}")
    if unit_id:
        store.cache_clear(f"unit_menus:{unit_id}")
    logger.info("invalidated stale menu cache (menuOid=%s unitId=%s)", menu_oid, unit_id)


def _item_macros(detail_oid: str, menu_oid: Optional[str],
                 unit_id: Optional[str]) -> dict:
    """Base ('1x') macros for one item, cached.

    If no context is supplied, fall back to the last-seen context for this item
    from the menu index — so a frontend holding only a detailOid still works.
    """
    if not menu_oid and not unit_id:
        known = store.find_item_context(detail_oid)
        if known:
            menu_oid, unit_id = known.get("menuOid"), known.get("unitId")
    if not menu_oid and not unit_id:
        raise HTTPException(status_code=422, detail=(
            "provide the item's session context: menuOid (multi-period unit) or "
            "unitId (single-period unit). No cached context found for this item."))
    if _looks_stale(detail_oid):
        raise HTTPException(status_code=410, detail={
            "error": "stale_detail_oid",
            "detailOid": detail_oid,
            "message": "this detailOid is not on any currently-cached menu. CBORD "
                       "reissues item ids daily — re-fetch the menu and use "
                       "today's id.",
        })

    key = f"label:{detail_oid}"
    cached = store.cache_get(key)
    if cached is not None:
        return cached
    # Selecting a menu needs its unit first; fill it in when the caller only
    # gave us a menuOid.
    if menu_oid and not unit_id:
        unit_id = store.cache_get(f"menu_unit:{menu_oid}")
    try:
        html = client.nutrition_label_html(detail_oid, menu_oid=menu_oid, unit_oid=unit_id)
    except CbordError as exc:
        # An error panel here almost always means the id no longer exists: CBORD
        # reissued every detailOid when the menu date rolled over, while our
        # cached menu still lists the old ones. Drop those stale cache entries so
        # the next menu fetch is fresh, and tell the client to refetch rather
        # than reporting an opaque upstream failure.
        _invalidate_menu_caches(menu_oid, unit_id)
        raise HTTPException(status_code=410, detail={
            "error": "stale_detail_oid",
            "detailOid": detail_oid,
            "message": "CBORD no longer recognizes this item id — ids are "
                       "reissued when the menu rolls over. The cached menu has "
                       "been dropped; re-fetch the menu and use today's ids.",
            "upstream": str(exc),
        })
    macros = parsers.to_macros(parsers.parse_nutrition_label(html))
    store.cache_set(key, macros, ttl=_LABEL_TTL)
    return macros


@app.get("/items/{detail_oid}/nutrition")
def item_nutrition(detail_oid: str,
                   menu_oid: Optional[str] = Query(None, alias="menuOid"),
                   unit_id: Optional[str] = Query(None, alias="unitId")):
    """Base ('1x') macros for one item. Pass `menuOid` (multi-period unit) or
    `unitId` (single-period unit); omitted, a cached context is used if known."""
    return _item_macros(detail_oid, menu_oid, unit_id)


class ScaleRequest(BaseModel):
    quantity: float = Field(..., ge=0, description="any decimal, e.g. 0.5, 1.25")
    menuOid: Optional[str] = None
    unitId: Optional[str] = None


@app.post("/items/{detail_oid}/scale")
def item_scale(detail_oid: str, req: ScaleRequest):
    """Macros for one item scaled by an arbitrary decimal quantity.
    NA macros stay null after scaling; they never become 0."""
    base = _item_macros(detail_oid, req.menuOid, req.unitId)
    return scale_macros(base, req.quantity)


# --- custom bowls (multi-component meals) --------------------------------------

class Component(BaseModel):
    """One part of a meal. Either a NetNutrition item (detailOid) or a manual
    entry (manualName + macros) for food NetNutrition doesn't cover."""
    detailOid: Optional[str] = None
    manualName: Optional[str] = None
    quantity: float = Field(1.0, ge=0)
    menuOid: Optional[str] = None
    unitId: Optional[str] = None
    # Manual-entry macros, used only when detailOid is absent.
    calories: Optional[float] = None
    proteinG: Optional[float] = None
    totalFatG: Optional[float] = None
    totalCarbG: Optional[float] = None


class ComputeRequest(BaseModel):
    components: list[Component] = Field(..., min_length=1)


def _resolve_components(components: list[Component]) -> list[dict]:
    """Scale each component independently, then return them for summing.

    Duke's NetNutrition has no build-your-own endpoint and no modifier groups —
    every part of a bowl is an independent item. So a custom dish is exactly
    this: each component scaled by its own quantity, summed.
    """
    resolved = []
    for comp in components:
        if comp.detailOid:
            base = _item_macros(comp.detailOid, comp.menuOid, comp.unitId)
        elif comp.manualName:
            base = {
                "name": comp.manualName, "servingSizeText": None,
                "servingSizeGrams": None, "calories": comp.calories,
                "proteinG": comp.proteinG, "totalFatG": comp.totalFatG,
                "totalCarbG": comp.totalCarbG, "qualifiers": {},
            }
        else:
            raise HTTPException(status_code=422,
                                detail="each component needs a detailOid or a manualName")
        scaled = scale_macros(base, comp.quantity)
        scaled["detailOid"] = comp.detailOid
        scaled["manualName"] = comp.manualName
        scaled["itemName"] = base.get("name") or comp.manualName
        resolved.append(scaled)
    return resolved


@app.post("/meals/compute")
def compute_meal(req: ComputeRequest):
    """Scale each component by its own quantity and sum them into a total.

    Preview endpoint — use this to show a running total while the user builds a
    bowl, then POST the same components to /log to save it.
    """
    components = _resolve_components(req.components)
    return {"components": components, "totalNutrition": sum_macros(components)}


# --- food log ------------------------------------------------------------------

# A NetID is a short lowercase alphanumeric handle (e.g. "rw290"). Validated so
# a malformed or injected value can't silently become someone's log key.
_NETID_RE = re.compile(r"^[a-z][a-z0-9]{1,19}$")


def caller_id(x_duke_netid: Optional[str] = Header(
        None, alias="X-Duke-NetID",
        description="Duke NetID, e.g. 'rw290'. Omit to use the shared anonymous log.")
        ) -> str:
    """Whose food log this request reads or writes.

    IMPORTANT: this identifies, it does not authenticate. The NetID arrives as a
    plain header that the client fills in, so anyone can send anyone else's. And
    NetIDs are short and guessable, which makes that easier here than it would
    be with a random id — someone who types a friend's NetID sees their food log
    and can delete from it.

    That is an acceptable trade for a 20-person beta among friends, and it buys
    the thing a random per-device id can't: the same log on your phone and your
    laptop, with nothing to copy between them. It is NOT acceptable once this is
    open to campus.

    To make it real, verify the NetID instead of trusting it: register an app
    with Duke OIT for Shibboleth/OIDC single sign-on, and set the user key from
    the verified token subject rather than from this header. Identities are
    stored prefixed ("netid:rw290") precisely so verified subjects and any
    future device-scoped ids can coexist without colliding with these.

    A request with no header falls back to the shared anonymous log, which is
    the pre-NetID behavior — an older frontend build keeps working while
    clients roll over.
    """
    raw = (x_duke_netid or "").strip().lower()
    if not raw:
        return ANONYMOUS_USER
    if not _NETID_RE.match(raw):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_netid",
            "message": "X-Duke-NetID must be a NetID like 'rw290' "
                       "(letter first, then letters/digits).",
        })
    return f"netid:{raw}"


class LogRequest(BaseModel):
    components: list[Component] = Field(..., min_length=1)
    label: Optional[str] = Field(None, description="e.g. 'Sashimi bowl, lunch'")
    timestamp: Optional[str] = Field(None, description="ISO 8601; defaults to now")


@app.post("/log")
def create_log_entry(req: LogRequest, user_id: str = Depends(caller_id)):
    """Save a logged meal.

    The full per-component breakdown is preserved (each detailOid with its own
    quantity and scaled macros), alongside the total computed AT LOG TIME. Reads
    return the stored values verbatim — a past entry never silently changes if
    CBORD's menu data changes later.
    """
    components = _resolve_components(req.components)
    total = sum_macros(components)
    timestamp = req.timestamp or dt.datetime.now().isoformat(timespec="seconds")
    log_date = timestamp[:10]
    return store.add_log_entry(timestamp, log_date, components, total, req.label,
                               user_id=user_id)


@app.get("/log")
def read_log(date: Optional[str] = Query(None, description="YYYY-MM-DD; omit for all"),
             user_id: str = Depends(caller_id)):
    """Past entries, exactly as they were logged (never recomputed).

    `dayTotal` sums the stored entry totals for convenience.
    """
    entries = store.get_log_entries(date, user_id=user_id)
    day_total = sum_macros([e["totalNutrition"] for e in entries]) if entries else None
    return {"date": date, "userId": user_id, "entries": entries, "dayTotal": day_total}


@app.delete("/log/{entry_id}")
def delete_log_entry(entry_id: str, user_id: str = Depends(caller_id)):
    if not store.delete_log_entry(entry_id, user_id=user_id):
        raise HTTPException(status_code=404, detail=f"no log entry {entry_id}")
    return {"deleted": entry_id}


@app.post("/admin/refresh")
def admin_refresh(audit: bool = Query(
        False, description="also re-sort dishes across every unit (slow)")):
    """Run the weekly refresh now, and report what changed in the lineup.

    Same work the Monday job does — useful right after Duke opens a location
    rather than waiting for the schedule. `?audit=true` adds the dish-grouping
    pass, which walks every unit and takes a while; the Monday job always
    includes it.
    """
    report = refresh_menu_data()
    if audit:
        report["dishAudit"] = audit_dish_grouping()
    return report


# --- cache control -------------------------------------------------------------

@app.post("/cache/clear")
def clear_cache(prefix: Optional[str] = Query(None, description="e.g. 'menu_items:'")):
    """Drop cached menus/labels to force a live refetch."""
    return {"cleared": store.cache_clear(prefix)}
