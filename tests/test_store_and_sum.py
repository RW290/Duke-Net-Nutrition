"""Tests for macro summation (custom bowls) and SQLite storage."""
import os
import tempfile

from app.db import Store
from app.scaling import scale_macros, sum_macros


def _macros(name, cal, protein, fat, carb):
    return {"name": name, "servingSizeText": None, "servingSizeGrams": None,
            "calories": cal, "proteinG": protein, "totalFatG": fat,
            "totalCarbG": carb, "qualifiers": {}}


def test_custom_bowl_components_scale_independently_then_sum():
    # 0.5x rice + 1x protein + 1.5x sauce, each scaled on its own.
    rice = scale_macros(_macros("Sushi Rice", 230, 4, 0, 51), 0.5)
    salmon = scale_macros(_macros("Salmon", 200, 22, 12, 0), 1.0)
    sauce = scale_macros(_macros("Spicy Mayo", 100, 0, 11, 1), 1.5)

    assert rice["calories"] == 115.0 and rice["totalCarbG"] == 25.5
    assert sauce["totalFatG"] == 16.5

    total = sum_macros([rice, salmon, sauce])
    assert total["calories"] == 465.0        # 115 + 200 + 150
    assert total["proteinG"] == 24.0         # 2 + 22 + 0
    assert total["totalFatG"] == 28.5        # 0 + 12 + 16.5
    assert total["totalCarbG"] == 27.0       # 25.5 + 0 + 1.5
    assert total["componentCount"] == 3
    assert total["incomplete"] == []


def test_sum_treats_na_honestly():
    # One component has NA protein: it contributes nothing but the total is
    # flagged incomplete rather than pretending the NA was a real 0.
    a = _macros("A", 100, 10, 5, 20)
    b = _macros("B", 50, None, 2, 10)
    total = sum_macros([a, b])
    assert total["calories"] == 150
    assert total["proteinG"] == 10          # only the known value
    assert "proteinG" in total["incomplete"]
    assert "caloriesG" not in total["incomplete"]

    # A field NA in EVERY component stays None, not 0.
    both_na = sum_macros([_macros("A", 10, None, 1, 1), _macros("B", 20, None, 2, 2)])
    assert both_na["proteinG"] is None
    assert both_na["calories"] == 30


def test_log_entry_roundtrip_preserves_components_and_frozen_total():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "t.sqlite3"))
        components = [
            {"detailOid": "1", "itemName": "Rice", "quantity": 0.5, "calories": 115,
             "proteinG": 2, "totalFatG": 0, "totalCarbG": 25.5},
            {"detailOid": "2", "itemName": "Salmon", "quantity": 1.0, "calories": 200,
             "proteinG": 22, "totalFatG": 12, "totalCarbG": 0},
        ]
        total = sum_macros(components)
        saved = store.add_log_entry("2026-08-18T12:30:00", "2026-08-18",
                                    components, total, label="Poke bowl")
        assert saved["id"]

        entries = store.get_log_entries("2026-08-18")
        assert len(entries) == 1
        entry = entries[0]
        # Full per-component breakdown survives, not just a collapsed total.
        assert len(entry["components"]) == 2
        assert entry["components"][0]["quantity"] == 0.5
        assert entry["components"][1]["itemName"] == "Salmon"
        assert entry["totalNutrition"]["calories"] == 315.0
        assert entry["label"] == "Poke bowl"

        # Filtering by another date returns nothing.
        assert store.get_log_entries("2026-08-19") == []
        assert store.delete_log_entry(saved["id"]) is True
        assert store.get_log_entries("2026-08-18") == []
        store.close()


def test_cache_ttl_and_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "c.sqlite3"))
        store.cache_set("k", {"v": 1}, ttl=60)
        assert store.cache_get("k") == {"v": 1}

        store.cache_set("expired", {"v": 2}, ttl=-1)   # already stale
        assert store.cache_get("expired") is None

        store.cache_set("menu_items:9", {"v": 3}, ttl=60)
        assert store.cache_clear("menu_items:") == 1
        assert store.cache_get("menu_items:9") is None
        assert store.cache_get("k") == {"v": 1}        # prefix clear is scoped
        store.close()


def test_stale_detail_oids_are_distinguishable_from_current_ones():
    # CBORD reissues detailOids daily, so an id seen only on an older menu must
    # be recognizable as stale rather than passed to a doomed nutrition lookup.
    import time as _time
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.sqlite3"))
        store.upsert_menu_items([
            {"detailOid": "today", "unitId": "7", "menuOid": None, "name": "Fresh Item"},
        ])
        # Backdate one row to simulate a sighting from two days ago.
        store._conn.execute(
            "INSERT INTO menu_item (detail_oid, unit_id, menu_oid, name, seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("stale", "7", None, "Old Item", _time.time() - 48 * 3600))
        store._conn.commit()

        assert store.find_item_context("today", max_age_seconds=18 * 3600) is not None
        assert store.find_item_context("stale", max_age_seconds=18 * 3600) is None
        assert store.find_item_context("stale") is not None   # still known, just old
        # An id never seen at all stays unknown (callers must not treat it as stale).
        assert store.find_item_context("never-seen") is None

        assert store.prune_menu_items() == 1
        store.close()


def test_menu_item_index_and_context_lookup():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "i.sqlite3"))
        store.upsert_menu_items([
            {"detailOid": "292785194", "unitId": "7", "menuOid": None,
             "name": "Edamame", "categoryId": "429",
             "categoryHeader": "Soups, Sides and Drinks", "servingSizeText": "5 oz"},
        ])
        ctx = store.find_item_context("292785194")
        assert ctx["unitId"] == "7" and ctx["name"] == "Edamame"
        assert store.find_item_context("nope") is None
        store.close()


def test_totals_surface_component_data_warnings():
    warned = {**_macros("Yogurt Chips", 22680, 189, 1134, 3213),
              "itemName": "Yogurt Chips",
              "dataWarning": {"type": "serving_size_mismatch", "ratio": 160.0}}
    clean = {**_macros("Sushi Rice", 230, 4, 0, 51), "itemName": "Sushi Rice"}
    total = sum_macros([warned, clean])
    assert total["calories"] == 22910
    assert "dataWarnings" in total
    assert total["dataWarnings"][0]["itemName"] == "Yogurt Chips"
    # A meal with no suspect component carries no warnings key at all.
    assert "dataWarnings" not in sum_macros([clean])


def test_scaling_preserves_the_data_warning():
    base = {**_macros("Yogurt Chips", 22680, 189, 1134, 3213),
            "dataWarning": {"type": "serving_size_mismatch", "ratio": 160.0}}
    scaled = scale_macros(base, 0.00625)
    assert scaled["dataWarning"]["ratio"] == 160.0
    assert scaled["calories"] == 141.75
