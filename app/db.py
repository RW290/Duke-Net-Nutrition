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

DEFAULT_TTL_SECONDS = 6 * 60 * 60

MENU_TTL_SECONDS = 90 * 60

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
    created_at      REAL NOT NULL,
    user_id         TEXT NOT NULL DEFAULT 'anonymous'
);

CREATE INDEX IF NOT EXISTS idx_log_entry_date ON log_entry(log_date);
"""


ANONYMOUS_USER = "anonymous"

_POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_log_entry_user_date ON log_entry(user_id, log_date);
"""


class Store:

    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.executescript(_POST_MIGRATION_INDEXES)
            self._conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(log_entry)")}
        if cols and "user_id" not in cols:
            self._conn.execute(
                "ALTER TABLE log_entry ADD COLUMN user_id TEXT NOT NULL "
                "DEFAULT 'anonymous'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


    def cache_get(self, key: str) -> Optional[Any]:
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
        with self._lock:
            if prefix is None:
                cur = self._conn.execute("DELETE FROM cache")
            else:
                cur = self._conn.execute(
                    "DELETE FROM cache WHERE key LIKE ?", (prefix + "%",))
            self._conn.commit()
            return cur.rowcount


    def get_known_units(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT id, name FROM dining_unit").fetchall()
        return {r["id"]: r["name"] for r in rows}

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
        with self._lock:
            cur = self._conn.execute("DELETE FROM menu_item WHERE seen_at < ?",
                                     (time.time() - max_age_seconds,))
            self._conn.commit()
            return cur.rowcount


    def add_log_entry(self, timestamp: str, log_date: str, components: list[dict],
                      total: dict, label: Optional[str] = None,
                      user_id: str = ANONYMOUS_USER) -> dict:
        entry_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO log_entry (id, timestamp, log_date, label, components_json, "
                "total_json, created_at, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry_id, timestamp, log_date, label, json.dumps(components),
                 json.dumps(total), time.time(), user_id),
            )
            self._conn.commit()
        return {"id": entry_id, "timestamp": timestamp, "date": log_date,
                "label": label, "components": components, "totalNutrition": total}

    def get_log_entries(self, log_date: Optional[str] = None,
                        user_id: str = ANONYMOUS_USER) -> list[dict]:
        sql = "SELECT * FROM log_entry WHERE user_id = ?"
        params: tuple = (user_id,)
        if log_date:
            sql += " AND log_date = ?"
            params = (user_id, log_date)
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

    def purge_old_log_entries(self, max_age_days: float) -> int:
        cutoff = time.time() - max_age_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM log_entry WHERE created_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def delete_log_entry(self, entry_id: str, user_id: str = ANONYMOUS_USER) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM log_entry WHERE id = ? AND user_id = ?", (entry_id, user_id))
            self._conn.commit()
            return cur.rowcount > 0
