from __future__ import annotations

import re

SPLIT_THRESHOLD = 10

_BUCKETS: list[tuple[str, list[str]]] = [
    ("Crepes", ["crepe"]),
    ("Pastries & Bakery", ["croissant", "muffin", "scone", "danish", "bagel",
                           "donut", "doughnut", "pastry", "pastries", "cookie",
                           "cake", "brownie", "biscuit"]),
    ("Breakfast", ["egg", "omelet", "omelette", "quiche", "scrambler",
                   "oatmeal", "pancake", "waffle", "breakfast", "parfait",
                   "granola"]),
    ("Drinks", ["coffee", "latte", "espresso", "cappuccino", "mocha", "tea",
               "juice", "soda", "smoothie", "lemonade", "water", "drink",
               "cold brew", "matcha", "chai", "refresher"]),
    ("Desserts", ["gelato", "ice cream", "pie", "pudding", "tiramisu"]),
    ("Salads & Bowls", ["salad", "bowl", "poke"]),
    ("Sandwiches & Wraps", ["sandwich", "wrap", "panini", "sub", "burger",
                           "blt", "quesadilla", "pita", "flatbread",
                           "shawarma", "focaccia"]),
    ("Sides & Snacks", ["chips", "fries", "crisp", "crisps", "side", "fruit cup",
                        "veggie", "vegetable", "hummus", "crudite"]),
]


def _bucket_for(name: str) -> str | None:
    lowered = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    for label, keywords in _BUCKETS:
        if any(keyword in lowered for keyword in keywords):
            return label
    return None


def split_flat_category(category: dict) -> list[dict]:
    items = category.get("items", [])
    if len(items) < SPLIT_THRESHOLD:
        return [category]

    grouped: dict[str, list[dict]] = {}
    leftover: list[dict] = []
    for item in items:
        label = _bucket_for(item.get("name", ""))
        if label:
            grouped.setdefault(label, []).append(item)
        else:
            leftover.append(item)

    if len(grouped) < 2:
        return [category]

    base_id = category.get("categoryId")
    header = category.get("header", "")
    result = []
    for label, bucket_items in grouped.items():
        result.append({
            **category,
            "categoryId": f"{base_id}:{label}",
            "header": f"{header} — {label}" if header else label,
            "items": bucket_items,
        })
    if leftover:
        result.append({
            **category,
            "categoryId": f"{base_id}:Other",
            "header": f"{header} — Other" if header else "Other",
            "items": leftover,
        })
    return result
