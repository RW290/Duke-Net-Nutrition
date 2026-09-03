"""SQLite persistence: response cache + food log.

Two concerns share one database file:

1. **Cache.** Menus don't change intraday, and every menu fetch otherwise costs
   a multi-request round trip to CBORD. Cached entries carry a TTL (default 6h).

   *Why SQLite rather than an in-memory dict:* the cache survives process
   restarts. This matters more than raw speed here — the app is deployed on a
   small host that sleeps/restarts often, and an in-memory cache would be cold
   after every wake, putting a slow CBORD chain in front of the first request
   each time. It also keeps one storage mechanism for both concerns instead of
   two. The cost is a disk round trip per lookup (microseconds locally,
   irrelevant next to a ~1s CBORD call) and, on hosts with ephemeral disks, the
   need for a mounted volume to make persistence real.

2. **Food log.** A LogEntry stores a LIST of components, each with its own
   detailOid and quantity, plus the total computed AT LOG TIME. Past entries
   must never silently change if CBORD's menu data changes later, so nothing is
   ever recomputed from live data on read — the stored JSON is returned as-is.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

DEFAULT_DB_PATH = os.environ.get(
    "DUKE_NUTRITION_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.sqlite3"),
)

# The unit list changes very rarely (a location is added or renamed).
DEFAULT_TTL_SECONDS = 6 * 60 * 60

# Menus get a much shorter TTL than their content would suggest. CBORD reissues
# every detailOid when the menu date rolls over — observed happening in the
# evening, not at midnight — and a cached menu that outlives the rollover hands
# out ids that are already dead, making every lookup fail. 90 minutes bounds
# that window; the self-healing invalidation in the API layer covers the rest.
MENU_TTL_SECONDS = 90 * 60

# CBORD reissues detailOids each day (the same item has a different id
# tomorrow), so an indexed item is only useful for matching while it's current.
FRESH_ITEM_SECONDS = 18 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dining_unit (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    seen_at     REAL NOT NULL
);

-- Menu items seen in a fetched menu. `context_json` records how to reach the
-- item again (menuOid or unitId), which the stateful nutrition call requires.
CREATE TABLE IF NOT EXISTS menu_item (
    detail_oid      TEXT NOT NULL,
    unit_id         TEXT,
    menu_oid        TEXT,
    name            TEXT NOT NULL,
    category_id     TEXT,
    category_header TEXT,
    serving_text    TEXT,
    date            TEXT,
    meal_period     TEXT,
    base_macros_json TEXT,
    seen_at         REAL NOT NULL,
    PRIMARY KEY (detail_oid, menu_oid, unit_id)
);

CREATE INDEX IF NOT EXISTS idx_menu_item_name ON menu_item(name);

-- components_json: [{detailOid|manualName, itemName, quantity, scaledNutrition}]
-- total_json:      the summed total, computed and frozen at log time.
CREATE TABLE IF NOT EXISTS log_entry (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    log_date        TEXT NOT NULL,
    label           TEXT,
    components_json TEXT NOT NULL,
    total_json      TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_log_entry_date ON log_entry(log_date);
"""


class Store:
    """Thread-safe SQLite store. One connection guarded by a lock (FastAPI runs
    handlers on a threadpool; this app is single-user, so a lock is plenty)."""

    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- cache ------------------------------------------------------------------

    def cache_get(self, key: str) -> Optional[Any]:
        """Return the cached value, or None if absent or expired."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json, expires_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        return json.loads(row["value_json"])

    def cache_set(self, key: str, value: Any, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO cache (key, value_json, fetched_at, expires_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value_json = excluded.value_json, fetched_at = excluded.fetched_at, "
                "expires_at = excluded.expires_at",
                (key, json.dumps(value), now, now + ttl),
            )
            self._conn.commit()

    def cache_clear(self, prefix: Optional[str] = None) -> int:
        """Drop cached entries (all, or those whose key starts with `prefix`)."""
        with self._lock:
            if prefix is None:
                cur = self._conn.execute("DELETE FROM cache")
            else:
                cur = self._conn.execute(
                    "DELETE FROM cache WHERE key LIKE ?", (prefix + "%",))
            self._conn.commit()
            return cur.rowcount

    # -- menu item index (resolve an item's session context + id freshness) ----

    def upsert_units(self, units: list[dict]) -> None:
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO dining_unit (id, name, seen_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, seen_at = excluded.seen_at",
                [(u["id"], u["name"], now) for u in units],
            )
            self._conn.commit()

    def upsert_menu_items(self, items: list[dict]) -> None:
        """Index items from a parsed menu so later lookups know each item's
        context (menuOid/unitId) without re-walking the menu."""
        now = time.time()
        rows = [
            (
                it["detailOid"], it.get("unitId"), it.get("menuOid"), it["name"],
                it.get("categoryId"), it.get("categoryHeader"), it.get("servingSizeText"),
                it.get("date"), it.get("mealPeriod"),
                json.dumps(it["baseMacros"]) if it.get("baseMacros") else None,
                now,
            )
            for it in items
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO menu_item (detail_oid, unit_id, menu_oid, name, category_id, "
                "category_header, serving_text, date, meal_period, base_macros_json, seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(detail_oid, menu_oid, unit_id) DO UPDATE SET "
                "name = excluded.name, category_id = excluded.category_id, "
                "category_header = excluded.category_header, "
                "serving_text = excluded.serving_text, seen_at = excluded.seen_at",
                rows,
            )
            self._conn.commit()

    def find_item_context(self, detail_oid: str,
                          max_age_seconds: Optional[float] = None) -> Optional[dict]:
        """Most recently seen (menuOid, unitId) context for an item.

        With max_age_seconds, only considers sightings newer than that cutoff —
        used to tell a currently-valid detailOid from one CBORD has since reissued.
        """
        sql = ("SELECT menu_oid, unit_id, name FROM menu_item WHERE detail_oid = ?")
        params: tuple = (detail_oid,)
        if max_age_seconds:
            sql += " AND seen_at >= ?"
            params = (detail_oid, time.time() - max_age_seconds)
        sql += " ORDER BY seen_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return {"menuOid": row["menu_oid"], "unitId": row["unit_id"], "name": row["name"]}

    def prune_menu_items(self, max_age_seconds: float = FRESH_ITEM_SECONDS) -> int:
        """Drop menu_item rows older than the cutoff (stale detailOids)."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM menu_item WHERE seen_at < ?",
                                     (time.time() - max_age_seconds,))
            self._conn.commit()
            return cur.rowcount

    # -- food log ---------------------------------------------------------------

    def add_log_entry(self, timestamp: str, log_date: str, components: list[dict],
                      total: dict, label: Optional[str] = None) -> dict:
        """Persist one logged meal. `total` is stored verbatim as computed at
        log time and is never recomputed on read."""
        entry_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO log_entry (id, timestamp, log_date, label, components_json, "
                "total_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry_id, timestamp, log_date, label, json.dumps(components),
                 json.dumps(total), time.time()),
            )
            self._conn.commit()
        return {"id": entry_id, "timestamp": timestamp, "date": log_date,
                "label": label, "components": components, "totalNutrition": total}

    def get_log_entries(self, log_date: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM log_entry"
        params: tuple = ()
        if log_date:
            sql += " WHERE log_date = ?"
            params = (log_date,)
        sql += " ORDER BY timestamp ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "timestamp": r["timestamp"],
                "date": r["log_date"],
                "label": r["label"],
                "components": json.loads(r["components_json"]),
                "totalNutrition": json.loads(r["total_json"]),
            }
            for r in rows
        ]

    def delete_log_entry(self, entry_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM log_entry WHERE id = ?", (entry_id,))
            self._conn.commit()
            return cur.rowcount > 0
