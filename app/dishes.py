from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Optional

from .subcategorize import split_flat_category

MIN_PREFIX_WORDS = 2

MAX_DISH_SECTIONS = 5

OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "dish_overrides.json")

_PAREN_RE = re.compile(r"\(([^)]*)\)")
_CHOOSE_RE = re.compile(
    r"\b(?:choose|select|pick)\b(?:\s+your)?(?:\s+up\s+to\s+[\w\d]+)?"
    r"(?:\s+(?:one|\d+))?", re.I)
_PICK_ONE_RE = re.compile(r"\bchoose\s+(?:your\s+)?(?:one|1)\b", re.I)
_UP_TO_RE = re.compile(r"\bup\s+to\b", re.I)
_PLURAL_NOUN_RE = re.compile(r"\btoppings\b|\bsauces\b|\bvegetables\b", re.I)


def normalize_header(header: str) -> list[str]:
    text = _PAREN_RE.sub(" ", header or "")
    text = text.replace("&", " and ")
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    return [t for t in text.lower().split() if t]


def _parenthetical(header: str) -> Optional[str]:
    m = _PAREN_RE.search(header or "")
    return m.group(1).strip() if m else None


def selection_hint(header: str) -> str:
    inner = _parenthetical(header) or header or ""
    if _PICK_ONE_RE.search(inner):
        return "pick_one"
    if _UP_TO_RE.search(inner) or _PLURAL_NOUN_RE.search(inner):
        return "pick_any"
    return "pick_any"


_GENERIC_SECTION_WORDS = {"toppings", "topping", "sauces", "sauce", "and", "or",
                          "etc", "choose", "your", "own"}


def _word_key(word: str) -> str:
    return re.sub(r"[^\w]", "", word or "").lower()


def section_role(header: str, prefix_words: int,
                 dish_tokens: Optional[list[str]] = None) -> str:
    bare = _CHOOSE_RE.sub(" ", _PAREN_RE.sub(" ", header or ""))
    words = bare.replace("&", " and ").split()
    if dish_tokens:
        dish_set = set(dish_tokens)
        without_dish = [w for w in words if _word_key(w) not in dish_set]
        if len(without_dish) == len(words):
            outside = " ".join(words).strip()
        else:
            outside = " ".join(
                w for w in without_dish
                if _word_key(w) not in _GENERIC_SECTION_WORDS).strip()
    else:
        outside = " ".join(words[prefix_words:]).strip()
    outside = outside.strip(" ,;:-–")
    if outside:
        return outside.title()

    inner = _parenthetical(header)
    if inner:
        role = _CHOOSE_RE.sub(" ", inner)
        role = re.sub(r"[:\-–]", " ", role)
        role = re.sub(r"\s+", " ", role).strip(" -–:")
        if role:
            return role.title()

    return "Main"


def load_overrides(path: str = OVERRIDES_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    return data.get("categoryToDish", data if isinstance(data, dict) else {})


def load_standalone_units(path: str = OVERRIDES_PATH) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        data = json.load(f)
    return {_venue_key(u) for u in data.get("standaloneUnits", [])}


def _venue_key(name: str) -> str:
    return " ".join(normalize_header(name or ""))


def group_categories(categories: list[dict],
                     overrides: Optional[dict] = None,
                     unit_name: Optional[str] = None,
                     standalone_units: Optional[set[str]] = None) -> dict:
    overrides = overrides if overrides is not None else load_overrides()
    standalone_units = (standalone_units if standalone_units is not None
                        else load_standalone_units())
    if unit_name and _venue_key(unit_name) in standalone_units:
        return {"dishes": [], "standalone": list(categories)}

    forced: dict[str, list[int]] = {}
    pinned_standalone: set[int] = set()
    remaining: list[int] = []
    for idx, cat in enumerate(categories):
        cid = str(cat.get("categoryId"))
        if cid in overrides:
            dish = overrides[cid]
            if dish:
                forced.setdefault(dish, []).append(idx)
            else:
                pinned_standalone.add(idx)
        else:
            remaining.append(idx)

    tokens = {idx: normalize_header(categories[idx].get("header", "")) for idx in remaining}

    counts: dict[tuple, list[int]] = {}
    for idx, toks in tokens.items():
        for n in range(MIN_PREFIX_WORDS, len(toks) + 1):
            counts.setdefault(tuple(toks[:n]), []).append(idx)

    candidates = sorted(
        ((prefix, members) for prefix, members in counts.items() if len(members) >= 2),
        key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True,
    )

    venue_tokens = tuple(normalize_header(unit_name)) if unit_name else ()

    assigned: dict[int, str] = {}
    groups: dict[str, list[int]] = {}
    swept: list[str] = []
    for prefix, members in candidates:
        free = [i for i in members if i not in assigned]
        if len(free) < 2:
            continue
        if (venue_tokens and prefix == venue_tokens
                and len(free) > MAX_DISH_SECTIONS):
            swept.append(" ".join(prefix))
            continue
        dish_name = _display_name(categories, free, len(prefix))
        for i in free:
            assigned[i] = dish_name
        groups.setdefault(dish_name, []).extend(free)

    dishes: list[dict] = []
    for dish_name, idxs in forced.items():
        extra: list[int] = list(groups.pop(dish_name, []))
        dish_toks = normalize_header(dish_name)
        for gname in [g for g in groups
                      if normalize_header(g)[:len(dish_toks)] == dish_toks]:
            extra.extend(groups.pop(gname))
        merged = sorted(set(idxs) | set(extra))
        dishes.append(_build_dish(dish_name, merged, categories,
                                  "override" if not extra else "mixed"))
    for dish_name, idxs in groups.items():
        dishes.append(_build_dish(dish_name, sorted(set(idxs)), categories, "matched"))

    grouped_idxs = {i for d in dishes for i in d["_idxs"]}
    standalone = [categories[i] for i in range(len(categories)) if i not in grouped_idxs]
    standalone = [split for cat in standalone for split in split_flat_category(cat)]

    dishes.sort(key=lambda d: min(d["_idxs"]))
    for d in dishes:
        d.pop("_idxs", None)

    result = {"dishes": dishes, "standalone": standalone}
    if swept:
        result["venueSweepsRejected"] = swept
    return result


def _display_name(categories: list[dict], idxs: list[int], prefix_words: int) -> str:
    header = min((categories[i].get("header", "") for i in idxs), key=len)
    words = _PAREN_RE.sub(" ", header).replace("&", " and ").split()
    return " ".join(words[:prefix_words]).strip().title()


def _build_dish(dish_name: str, idxs: list[int], categories: list[dict],
                source: str) -> dict:
    dish_tokens = normalize_header(dish_name)
    sections = []
    for i in idxs:
        cat = categories[i]
        header = cat.get("header", "")
        has_prefix = normalize_header(header)[:len(dish_tokens)] == dish_tokens
        sections.append({
            "categoryId": cat.get("categoryId"),
            "header": header,
            "role": section_role(header, len(dish_tokens),
                                 dish_tokens=None if has_prefix else dish_tokens),
            "selectionHint": selection_hint(header),
            "items": cat.get("items", []),
        })
    return {"dishName": dish_name, "source": source, "sections": sections,
            "_idxs": idxs}
