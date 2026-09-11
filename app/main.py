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


REFRESH_WEEKDAY = 0
REFRESH_HOUR = 4
_CAMPUS_TZ = ZoneInfo("America/New_York")

LOG_RETENTION_DAYS = 30
_RETENTION_INTERVAL_SECONDS = 24 * 60 * 60


def _seconds_until_next_refresh(now: Optional[dt.datetime] = None) -> float:
    now = now or dt.datetime.now(_CAMPUS_TZ)
    target = now.replace(hour=REFRESH_HOUR, minute=0, second=0, microsecond=0)
    target += dt.timedelta(days=(REFRESH_WEEKDAY - now.weekday()) % 7)
    if target <= now:
        target += dt.timedelta(days=7)
    return (target - now).total_seconds()


def refresh_menu_data() -> dict:
    before = store.get_known_units()
    for prefix in ("units", "unit_menus:", "menu_unit:"):
        store.cache_clear(prefix)
    store.prune_menu_items(0)

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
            failed.append(f"{unit['name']}: {exc}")

    return {
        "unitsAudited": len(_fetch_units()) - len(failed),
        "venuesAutoCorrected": sorted(autohealed),
        "staleOverrideIds": sorted(cid for cid in overrides if cid not in seen_ids),
        "unreachable": failed,
    }


async def _weekly_refresh_loop() -> None:
    while True:
        await asyncio.sleep(_seconds_until_next_refresh())
        try:
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
            logger.exception("weekly refresh failed; retrying next Monday")


async def _log_retention_loop() -> None:
    while True:
        try:
            purged = await asyncio.to_thread(store.purge_old_log_entries, LOG_RETENTION_DAYS)
            if purged:
                logger.info("purged %d log entries older than %d days", purged, LOG_RETENTION_DAYS)
        except Exception:
            logger.exception("log retention purge failed")
        await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    refresh_task = asyncio.create_task(_weekly_refresh_loop())
    retention_task = asyncio.create_task(_log_retention_loop())
    logger.info("weekly refresh scheduled in %.1f hours",
                _seconds_until_next_refresh() / 3600)
    try:
        yield
    finally:
        refresh_task.cancel()
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task


app = FastAPI(
    lifespan=lifespan,
    title="Duke NetNutrition API",
    version="0.2.0",
    description="Personal backend for logging Duke dining macros. "
                "Backend only — the frontend is built separately.",
)
client = CbordClient()


def _open_store() -> tuple[Store, Optional[str]]:
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

_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_LABEL_TTL = 12 * 60 * 60


@app.get("/health")
def health():
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
    return {
        "status": "ok",
        "service": "Duke NetNutrition API",
        "docs": "/docs",
    }



def _fetch_units() -> list[dict]:
    cached = store.cache_get("units")
    if cached is not None:
        return cached
    units = parsers.parse_units(client.get_landing_html())
    _drop_caches_if_unit_ids_shifted(units)
    store.cache_set("units", units, ttl=DEFAULT_TTL_SECONDS)
    store.upsert_units(units)
    return units


def _drop_caches_if_unit_ids_shifted(units: list[dict]) -> None:
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
    for period in periods:
        if period.get("menuOid"):
            store.cache_set(f"menu_unit:{period['menuOid']}", unit_id,
                            ttl=DEFAULT_TTL_SECONDS * 4)


@app.get("/units/{unit_id}/menus")
def unit_menus(unit_id: str, refresh: bool = Query(False)):
    key = f"unit_menus:{unit_id}"
    if refresh:
        store.cache_clear(key)
    cached = store.cache_get(key)
    if cached is not None:
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



def _index_menu_items(menu: dict, unit_id: Optional[str], menu_oid: Optional[str],
                      date: Optional[str] = None, meal: Optional[str] = None) -> None:
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



def _unit_name(unit_id: Optional[str]) -> Optional[str]:
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
    menu = menu_items(menu_oid, refresh=refresh)
    return {"menuOid": menu_oid, **_dishes_for(menu["categories"],
                                              unit_id=_resolve_menu_unit(menu_oid))}


@app.get("/units/{unit_id}/dishes")
def unit_dishes(unit_id: str, refresh: bool = Query(False)):
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
    return {"categoryToDish": dishes.load_overrides(), "path": dishes.OVERRIDES_PATH}



def _looks_stale(detail_oid: str) -> bool:
    if store.find_item_context(detail_oid, max_age_seconds=FRESH_ITEM_SECONDS):
        return False
    return store.find_item_context(detail_oid) is not None


def _invalidate_menu_caches(menu_oid: Optional[str], unit_id: Optional[str]) -> None:
    if menu_oid:
        store.cache_clear(f"menu_items:{menu_oid}")
    if unit_id:
        store.cache_clear(f"unit_menus:{unit_id}")
    logger.info("invalidated stale menu cache (menuOid=%s unitId=%s)", menu_oid, unit_id)


def _item_macros(detail_oid: str, menu_oid: Optional[str],
                 unit_id: Optional[str]) -> dict:
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
    if menu_oid and not unit_id:
        unit_id = store.cache_get(f"menu_unit:{menu_oid}")
    try:
        html = client.nutrition_label_html(detail_oid, menu_oid=menu_oid, unit_oid=unit_id)
    except CbordError as exc:
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
    return _item_macros(detail_oid, menu_oid, unit_id)


class ScaleRequest(BaseModel):
    quantity: float = Field(..., ge=0, description="any decimal, e.g. 0.5, 1.25")
    menuOid: Optional[str] = None
    unitId: Optional[str] = None


@app.post("/items/{detail_oid}/scale")
def item_scale(detail_oid: str, req: ScaleRequest):
    base = _item_macros(detail_oid, req.menuOid, req.unitId)
    return scale_macros(base, req.quantity)



class Component(BaseModel):
    detailOid: Optional[str] = None
    manualName: Optional[str] = None
    quantity: float = Field(1.0, ge=0)
    menuOid: Optional[str] = None
    unitId: Optional[str] = None
    calories: Optional[float] = None
    proteinG: Optional[float] = None
    totalFatG: Optional[float] = None
    totalCarbG: Optional[float] = None


class ComputeRequest(BaseModel):
    components: list[Component] = Field(..., min_length=1)


def _resolve_components(components: list[Component]) -> list[dict]:
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
        scaled["menuOid"] = comp.menuOid
        scaled["unitId"] = comp.unitId
        resolved.append(scaled)
    return resolved


@app.post("/meals/compute")
def compute_meal(req: ComputeRequest):
    components = _resolve_components(req.components)
    return {"components": components, "totalNutrition": sum_macros(components)}



_NETID_RE = re.compile(r"^[a-z][a-z0-9]{1,19}$")


def caller_id(x_duke_netid: Optional[str] = Header(
        None, alias="X-Duke-NetID",
        description="Duke NetID, e.g. 'rw290'. Omit to use the shared anonymous log.")
        ) -> str:
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
    components = _resolve_components(req.components)
    total = sum_macros(components)
    timestamp = req.timestamp or dt.datetime.now().isoformat(timespec="seconds")
    log_date = timestamp[:10]
    return store.add_log_entry(timestamp, log_date, components, total, req.label,
                               user_id=user_id)


@app.get("/log")
def read_log(date: Optional[str] = Query(None, description="YYYY-MM-DD; omit for all"),
             user_id: str = Depends(caller_id)):
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
    report = refresh_menu_data()
    if audit:
        report["dishAudit"] = audit_dish_grouping()
    return report



@app.post("/cache/clear")
def clear_cache(prefix: Optional[str] = Query(None, description="e.g. 'menu_items:'")):
    return {"cleared": store.cache_clear(prefix)}
