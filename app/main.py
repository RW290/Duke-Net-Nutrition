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

import datetime as dt
import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import dishes, parsers
from .cbord import CbordClient, CbordError
from .db import DEFAULT_TTL_SECONDS, FRESH_ITEM_SECONDS, MENU_TTL_SECONDS, Store
from .scaling import scale_macros, sum_macros

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

app = FastAPI(
    title="Duke NetNutrition API",
    version="0.2.0",
    description="Personal backend for logging Duke dining macros. "
                "Backend only — the frontend is built separately.",
)
client = CbordClient()
store = Store()

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
    return {"status": "ok"}


# --- units ---------------------------------------------------------------------

def _fetch_units() -> list[dict]:
    """Units, cached. Always sourced live from NetNutrition's landing page —
    never a hardcoded list — so added/renamed/removed locations track upstream."""
    cached = store.cache_get("units")
    if cached is not None:
        return cached
    units = parsers.parse_units(client.get_landing_html())
    store.cache_set("units", units, ttl=DEFAULT_TTL_SECONDS)
    store.upsert_units(units)
    return units


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

class LogRequest(BaseModel):
    components: list[Component] = Field(..., min_length=1)
    label: Optional[str] = Field(None, description="e.g. 'Sashimi bowl, lunch'")
    timestamp: Optional[str] = Field(None, description="ISO 8601; defaults to now")


@app.post("/log")
def create_log_entry(req: LogRequest):
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
    return store.add_log_entry(timestamp, log_date, components, total, req.label)


@app.get("/log")
def read_log(date: Optional[str] = Query(None, description="YYYY-MM-DD; omit for all")):
    """Past entries, exactly as they were logged (never recomputed).

    `dayTotal` sums the stored entry totals for convenience.
    """
    entries = store.get_log_entries(date)
    day_total = sum_macros([e["totalNutrition"] for e in entries]) if entries else None
    return {"date": date, "entries": entries, "dayTotal": day_total}


@app.delete("/log/{entry_id}")
def delete_log_entry(entry_id: str):
    if not store.delete_log_entry(entry_id):
        raise HTTPException(status_code=404, detail=f"no log entry {entry_id}")
    return {"deleted": entry_id}


# --- cache control -------------------------------------------------------------

@app.post("/cache/clear")
def clear_cache(prefix: Optional[str] = Query(None, description="e.g. 'menu_items:'")):
    """Drop cached menus/labels to force a live refetch."""
    return {"cleared": store.cache_clear(prefix)}
